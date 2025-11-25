from flask import Flask, request, Response
from twilio.twiml.messaging_response import MessagingResponse
import sqlite3
from datetime import datetime, date
import os

app = Flask(__name__)
DB = "calorie_bot.db"

def init_db():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        wa_number TEXT UNIQUE,
        daily_target INTEGER DEFAULT 2000
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS food_items (
        id INTEGER PRIMARY KEY,
        name TEXT UNIQUE,
        unit TEXT,
        kcal_per_unit REAL
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY,
        user_id INTEGER,
        food_item_id INTEGER,
        quantity REAL,
        kcal REAL,
        timestamp TEXT
    )""")
    conn.commit()

    seed_data = [
        ("apple", "piece", 95),
        ("banana", "piece", 105),
        ("brown rice", "100g", 111),
        ("egg", "piece", 78),
        ("oats", "100g", 389),
    ]
    for name, unit, kcal in seed_data:
        try:
            c.execute("INSERT INTO food_items (name, unit, kcal_per_unit) VALUES (?, ?, ?)",
                      (name, unit, kcal))
        except:
            pass
    conn.commit()
    conn.close()

def get_or_create_user(num):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT id, daily_target FROM users WHERE wa_number = ?", (num,))
    row = c.fetchone()
    if row:
        uid, target = row
    else:
        c.execute("INSERT INTO users (wa_number) VALUES (?)", (num,))
        conn.commit()
        uid = c.lastrowid
        target = 2000
    conn.close()
    return uid, target

def find_food(name):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT id, name, unit, kcal_per_unit FROM food_items WHERE name LIKE ?", (f"%{name}%",))
    rows = c.fetchall()
    conn.close()
    return rows

def log_food(uid, fid, qty, kcal):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("INSERT INTO logs (user_id, food_item_id, quantity, kcal, timestamp) VALUES (?, ?, ?, ?, ?)",
              (uid, fid, qty, kcal, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()

def today_total(uid):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT SUM(kcal) FROM logs WHERE user_id = ? AND date(timestamp)=?",
              (uid, date.today().isoformat()))
    total = c.fetchone()[0]
    conn.close()
    return total or 0

@app.route("/webhook", methods=["POST"])
def webhook():
    wa_from = request.form.get("From")
    body = request.form.get("Body","").lower()

    init_db()
    uid, target = get_or_create_user(wa_from)

    resp = MessagingResponse()

    parts = body.split()

    if parts[0] == "add":
        food = " ".join(parts[1:-1])
        qty = float(parts[-1])

        matches = find_food(food)
        if not matches:
            resp.message("Food not found.")
            return Response(str(resp), mimetype="application/xml")

        fid, name, unit, kcal_unit = matches[0]

        if unit == "100g":
            total_kcal = (kcal_unit * qty) / 100
        else:
            total_kcal = kcal_unit * qty

        log_food(uid, fid, qty, total_kcal)
        today = today_total(uid)

        resp.message(f"Added {qty} x {name} = {int(total_kcal)} kcal\nToday: {int(today)}/{target}")
        return Response(str(resp), mimetype="application/xml")

    if parts[0] == "today":
        today = today_total(uid)
        resp.message(f"Today total = {int(today)} / {target} kcal")
        return Response(str(resp), mimetype="application/xml")

    resp.message("Commands:\nadd <food> <qty>\ntoday")
    return Response(str(resp), mimetype="application/xml")

if __name__ == "__main__":
    init_db()
    app.run(debug=True)
