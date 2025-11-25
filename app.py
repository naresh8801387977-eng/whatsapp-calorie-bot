# app.py
import os
import sqlite3
import requests
from datetime import datetime, date
from flask import Flask, request, Response
from twilio.twiml.messaging_response import MessagingResponse
from io import BytesIO
from PIL import Image

# Optional Google Vision (image labeling)
from google.cloud import vision

# ---------- Config ----------
DB = "calorie_bot.db"
NUTRITIONIX_APP_ID = os.environ.get("NUTRITIONIX_APP_ID")
NUTRITIONIX_APP_KEY = os.environ.get("NUTRITIONIX_APP_KEY")
# Google Vision expects GOOGLE_APPLICATION_CREDENTIALS to point to service account JSON (set in Render env)
USE_GOOGLE_VISION = os.environ.get("USE_GOOGLE_VISION", "1") == "1"

# ---------- Init ----------
app = Flask(__name__)

# ---------- DB helpers ----------
def init_db():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        wa_number TEXT UNIQUE,
        daily_target INTEGER DEFAULT 2000
    )""")
    c.execute("""
    CREATE TABLE IF NOT EXISTS food_items (
        id INTEGER PRIMARY KEY,
        name TEXT UNIQUE,
        unit TEXT,
        kcal_per_unit REAL
    )""")
    c.execute("""
    CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY,
        user_id INTEGER,
        food_item_id INTEGER,
        quantity REAL,
        kcal REAL,
        timestamp TEXT
    )""")
    conn.commit()
    # seed
    seed_foods = [
        ("apple", "piece", 95),
        ("banana", "piece", 105),
        ("brown rice", "100g", 111),
        ("egg", "piece", 78),
        ("oats", "100g", 389)
    ]
    for name, unit, kcal in seed_foods:
        try:
            c.execute("INSERT INTO food_items (name, unit, kcal_per_unit) VALUES (?, ?, ?)",
                      (name, unit, kcal))
        except sqlite3.IntegrityError:
            pass
    conn.commit()
    conn.close()

def get_or_create_user(wa_number):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT id, daily_target FROM users WHERE wa_number = ?", (wa_number,))
    row = c.fetchone()
    if row:
        uid, target = row
    else:
        c.execute("INSERT INTO users (wa_number) VALUES (?)", (wa_number,))
        conn.commit()
        uid = c.lastrowid
        target = 2000
    conn.close()
    return uid, target

def find_food_local(name):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT id, name, unit, kcal_per_unit FROM food_items WHERE LOWER(name) LIKE ?", (f"%{name.lower()}%",))
    rows = c.fetchall()
    conn.close()
    return rows

def log_food_local(user_id, food_id, quantity, kcal):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    ts = datetime.utcnow().isoformat()
    c.execute("INSERT INTO logs (user_id, food_item_id, quantity, kcal, timestamp) VALUES (?, ?, ?, ?, ?)",
              (user_id, food_id, quantity, kcal, ts))
    conn.commit()
    conn.close()

def today_total(user_id):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    start = date.today().isoformat()
    c.execute("SELECT SUM(kcal) FROM logs WHERE user_id = ? AND date(timestamp)=?", (user_id, start))
    s = c.fetchone()[0] or 0
    conn.close()
    return s

def get_today_logs(user_id):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    start = date.today().isoformat()
    c.execute("""
      SELECT l.quantity, f.name, f.unit, l.kcal, l.timestamp
      FROM logs l JOIN food_items f ON l.food_item_id = f.id
      WHERE l.user_id = ? AND date(l.timestamp) = ?
      ORDER BY l.id DESC
    """, (user_id, start))
    rows = c.fetchall()
    conn.close()
    return rows

# ---------- Image helpers ----------
def download_image(url):
    resp = requests.get(url, timeout=20)
    resp.raise_for_status()
    return resp.content

def google_label_image(image_bytes):
    client = vision.ImageAnnotatorClient()
    image = vision.Image(content=image_bytes)
    response = client.label_detection(image=image, max_results=5)
    labels = []
    for label in response.label_annotations:
        labels.append((label.description, label.score))
    return labels  # list of (label, score)

# ---------- Nutrition lookup (Nutritionix Natural Language API) ----------
def nutritionix_query(natural_query):
    """
    Query Nutritionix Natural Language endpoint.
    Requires NUTRITIONIX_APP_ID and NUTRITIONIX_APP_KEY set in env.
    Returns calories (float) and a friendly parsed name, or None on failure.
    """
    if not NUTRITIONIX_APP_ID or not NUTRITIONIX_APP_KEY:
        return None
    url = "https://trackapi.nutritionix.com/v2/natural/nutrients"
    headers = {
        "x-app-id": NUTRITIONIX_APP_ID,
        "x-app-key": NUTRITIONIX_APP_KEY,
        "Content-Type": "application/json"
    }
    payload = {"query": natural_query}
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
        if "foods" in data and len(data["foods"])>0:
            total_kcal = sum(f.get("nf_calories",0) for f in data["foods"])
            parsed = ", ".join(f.get("food_name","") for f in data["foods"])
            return {"kcal": total_kcal, "parsed_name": parsed}
    except Exception as e:
        print("Nutritionix error:", e)
    return None

# ---------- Main message handler ----------
def handle_incoming(wa_from, body, num_media, media_urls):
    uid, target = get_or_create_user(wa_from)
    resp_text = ""

    # If image(s) present - try to analyse
    if int(num_media or 0) > 0 and media_urls:
        # use first image
        img_url = media_urls[0]
        try:
            img_bytes = download_image(img_url)
        except Exception as e:
            return "I couldn't download your image. Please try again."

        # label with Google Vision (if enabled)
        label = None
        if USE_GOOGLE_VISION:
            try:
                labels = google_label_image(img_bytes)
                if labels:
                    # pick top label that looks like food (simple heuristic)
                    label = labels[0][0]
            except Exception as e:
                print("Vision error:", e)
                label = None

        # fallback: ask user to enter what it is
        if not label:
            return "I couldn't identify the food in the image. Please reply with the food name (eg: banana 1) or send another photo."

        # Now try nutrition lookup using the label
        # default assume 1 serving
        query = f"1 serving {label}"
        nx = nutritionix_query(query)
        if nx:
            kcal = nx["kcal"]
            parsed = nx["parsed_name"] or label
            # Create a quick reply and ask to confirm
            resp_text = (f"I detected *{parsed}* from the photo — estimated *{int(kcal)} kcal* for {query}.\n"
                         f"Reply `yes` to log this, or reply with `add <food> <qty>` to correct (eg: add banana 2).")
            # Save the detection temporarily by storing inferred values in a simple session? (we will rely on user reply)
            # For simplicity we'll prompt and wait for user confirmation.
            return resp_text
        else:
            # fallback to local DB lookup by label
            local = find_food_local(label)
            if local:
                fid, fname, unit, kcal_per_unit = local[0]
                # assume 1 unit
                if "100g" in unit:
                    est_kcal = kcal_per_unit  # user must confirm grams
                else:
                    est_kcal = kcal_per_unit
                resp_text = (f"I think it's *{fname}* — estimated *{int(est_kcal)} kcal* per {unit}.\n"
                             f"Reply `yes` to log 1 x {fname}, or reply `add {fname} <qty>` to change.")
                return resp_text
            else:
                return (f"I think the image contains *{label}*, but I couldn't fetch its calories. "
                        "Reply `add <food> <qty>` (eg `add banana 1`) to log it manually.")

    # Otherwise parse text commands
    parts = (body or "").strip().lower().split()
    if not parts:
        return ("Commands:\n- add <food> <qty>  (eg: add apple 1)\n- today\n- settarget <kcal>")

    if parts[0] in ("add","log"):
        if len(parts) < 3:
            return "Usage: add <food> <qty> (eg: add apple 1)"
        # last token quantity
        try:
            qty = float(parts[-1])
            food_name = " ".join(parts[1:-1])
        except:
            token = parts[-1]
            num = ''.join(ch for ch in token if (ch.isdigit() or ch=='.'))
            if not num:
                return "Couldn't parse quantity. Use a number: add apple 1"
            qty = float(num)
            food_name = " ".join(parts[1:-1])

        # Try Nutritionix for precise kcal
        nx = None
        if NUTRITIONIX_APP_ID and NUTRITIONIX_APP_KEY:
            try:
                # natural query like "2 apple"
                nx = nutritionix_query(f"{qty} {food_name}")
            except:
                nx = None

        if nx:
            kcal = nx["kcal"]
            # log to local DB if the item exists or create a generic entry (we will fallback to not creating a food item)
            # Try to find local id:
            matches = find_food_local(food_name)
            if matches:
                fid = matches[0][0]
                log_food_local(uid, fid, qty, kcal)
            else:
                # create a temp local row for this food
                conn = sqlite3.connect(DB)
                c = conn.cursor()
                try:
                    c.execute("INSERT INTO food_items (name, unit, kcal_per_unit) VALUES (?, ?, ?)",
                              (food_name, "serving", float(kcal)/qty if qty else float(kcal)))
                    conn.commit()
                    fid = c.lastrowid
                except sqlite3.IntegrityError:
                    c.execute("SELECT id FROM food_items WHERE name = ?", (food_name,))
                    fid = c.fetchone()[0]
                conn.close()
                log_food_local(uid, fid, qty, kcal)

            total = today_total(uid)
            return f"Logged {qty} x {food_name} = {int(kcal)} kcal. Today: {int(total)}/{get_or_create_user(wa_from)[1]} kcal."

        # Fallback: local DB or ask user
        matches = find_food_local(food_name)
        if not matches:
            return f"No local data for '{food_name}'. Try: add <food> <qty> with a common food name or try the web version."
        fid, fname, unit, kcal_per_unit = matches[0]
        if "100g" in unit:
            kcal = (kcal_per_unit * qty) / 100.0
        else:
            kcal = kcal_per_unit * qty
        log_food_local(uid, fid, qty, kcal)
        total = today_total(uid)
        return f"Logged {qty} x {fname} = {int(kcal)} kcal. Today: {int(total)}/{get_or_create_user(wa_from)[1]} kcal."

    if parts[0] in ("today","total"):
        total = today_total(uid)
        rows = get_today_logs(uid)
        lines = [f"{q} x {name} = {int(k)} kcal" for q, name, unit, k, ts in rows]
        s = f"Today: {int(total)}/{get_or_create_user(wa_from)[1]} kcal\n" + ("\n".join(lines) if lines else "No logs today.")
        return s

    if parts[0] in ("settarget","target"):
        if len(parts)>=2 and parts[1].isdigit():
            conn = sqlite3.connect(DB)
            c = conn.cursor()
            c.execute("UPDATE users SET daily_target = ? WHERE wa_number = ?", (int(parts[1]), wa_from))
            conn.commit()
            conn.close()
            return f"Daily target set to {parts[1]} kcal."
        else:
            return "Usage: settarget <kcal>"

    return ("Commands:\n- add <food> <qty>\n- today\n- settarget <kcal>\nSend a food photo and I'll try to detect and estimate calories.")

# ---------- Webhook route ----------
@app.route("/webhook", methods=["POST"])
def webhook():
    init_db()
    wa_from = request.form.get('From')  # 'whatsapp:+123456'
    body = request.form.get('Body', '').strip()
    num_media = request.form.get('NumMedia', '0')
    media_urls = []
    try:
        nm = int(num_media or 0)
    except:
        nm = 0
    for i in range(nm):
        url = request.form.get(f"MediaUrl{i}")
        if url:
            media_urls.append(url)

    reply_text = handle_incoming(wa_from, body, nm, media_urls)
    resp = MessagingResponse()
    resp.message(reply_text)
    return Response(str(resp), mimetype="application/xml")


if __name__ == "__main__":
    # required for local debugging
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
