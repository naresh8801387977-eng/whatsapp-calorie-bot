# app.py
import os
import sqlite3
import requests
from datetime import datetime, date
from flask import Flask, request, Response
from twilio.twiml.messaging_response import MessagingResponse

DB = "calorie_bot.db"
NUTRITIONIX_APP_ID = os.environ.get("NUTRITIONIX_APP_ID")
NUTRITIONIX_APP_KEY = os.environ.get("NUTRITIONIX_APP_KEY")

app = Flask(__name__)

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
    # seed with a few foods
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

# Nutritionix natural language query
def nutritionix_query(natural_query):
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
        if "foods" in data and len(data["foods"]) > 0:
            total_kcal = sum(f.get("nf_calories", 0) for f in data["foods"])
            parsed = ", ".join(f.get("food_name", "") for f in data["foods"])
            return {"kcal": total_kcal, "parsed_name": parsed}
    except Exception as e:
        print("Nutritionix error:", e)
    return None

def parse_quantity_token(token):
    # accept decimal like 0.5, and fractions like 1/2
    token = token.strip()
    if "/" in token:
        try:
            num, den = token.split("/")
            return float(num) / float(den)
        except:
            return None
    try:
        return float(token)
    except:
        return None

def handle_incoming(wa_from, body):
    uid, target = get_or_create_user(wa_from)
    if not body:
        return ("Commands:\n- add <food> <qty>  (eg: add apple 1 or add oats 1/2)\n- today\n- settarget <kcal>")
    parts = body.strip().lower().split()
    if parts[0] in ("add", "log"):
        if len(parts) < 2:
            return "Usage: add <food> <qty> (eg: add apple 1)"
        # if only two tokens (add banana) assume qty = 1
        if len(parts) == 2:
            qty = 1.0
            food_name = parts[1]
        else:
            qty_token = parts[-1]
            qty = parse_quantity_token(qty_token)
            if qty is None:
                return "Couldn't parse quantity. Use a number or fraction (eg: 0.5 or 1/2)."
            food_name = " ".join(parts[1:-1]) if len(parts) > 2 else parts[1]

        # Try Nutritionix first
        nx = None
        if NUTRITIONIX_APP_ID and NUTRITIONIX_APP_KEY:
            try:
                nx = nutritionix_query(f"{qty} {food_name}")
            except:
                nx = None

        if nx:
            kcal = nx["kcal"]
            # attempt to record: find or create local food row
            matches = find_food_local(food_name)
            if matches:
                fid = matches[0][0]
            else:
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

        # fallback to local DB
        matches = find_food_local(food_name)
        if not matches:
            return f"No local data for '{food_name}'. Try: add <food> <qty> with a common food name or enable Nutritionix."
        fid, fname, unit, kcal_per_unit = matches[0]
        if "100g" in unit:
            kcal = (kcal_per_unit * qty) / 100.0
        else:
            kcal = kcal_per_unit * qty
        log_food_local(uid, fid, qty, kcal)
        total = today_total(uid)
        return f"Logged {qty} x {fname} = {int(kcal)} kcal. Today: {int(total)}/{get_or_create_user(wa_from)[1]} kcal."

    if parts[0] in ("today", "total"):
        total = today_total(uid)
        rows = get_today_logs(uid)
        lines = [f"{q} x {name} = {int(k)} kcal" for q, name, unit, k, ts in rows]
        s = f"Today: {int(total)}/{get_or_create_user(wa_from)[1]} kcal\n" + ("\n".join(lines) if lines else "No logs today.")
        return s

    if parts[0] in ("settarget", "target"):
        if len(parts) >= 2 and parts[1].isdigit():
            conn = sqlite3.connect(DB)
            c = conn.cursor()
            c.execute("UPDATE users SET daily_target = ? WHERE wa_number = ?", (int(parts[1]), wa_from))
            conn.commit()
            conn.close()
            return f"Daily target set to {parts[1]} kcal."
        else:
            return "Usage: settarget <kcal>"

    return ("Commands:\n- add <food> <qty>\n- today\n- settarget <kcal>")

@app.route("/webhook", methods=["POST"])
def webhook():
    init_db()
    wa_from = request.form.get('From')  # 'whatsapp:+123456'
    body = request.form.get('Body', '').strip()
    reply_text = handle_incoming(wa_from, body)
    resp = MessagingResponse()
    resp.message(reply_text)
    return Response(str(resp), mimetype="application/xml")

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
