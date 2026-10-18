# main.py
# Telegram bot (aiogram) + Flask webhook for Stripe
# Do NOT put real secrets in the repo. Set them via environment variables or GitHub Secrets.

import os
import threading
import sqlite3
from datetime import datetime, timedelta
from flask import Flask, request, abort
import stripe
from aiogram import Bot, Dispatcher, types, executor

BOT_TOKEN = os.getenv("8923459305:AAGakZB337V9LYMvLxshSEc-guUy4GLkEOY")
STRIPE_KEY = os.getenv("STRIPE_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
BASE_URL = os.getenv("BASE_URL")  # https://your-domain.com

if not all([BOT_TOKEN, STRIPE_KEY, STRIPE_WEBHOOK_SECRET, BASE_URL]):
    print("One of required env vars is missing: BOT_TOKEN, STRIPE_KEY, STRIPE_WEBHOOK_SECRET, BASE_URL")
    # Don't exit here so the Flask part can still run for webhook tests, but bot will not start without BOT_TOKEN

stripe.api_key = STRIPE_KEY

bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
if bot:
    dp = Dispatcher(bot)
else:
    dp = Dispatcher(Bot(token=""))  # dummy dispatcher until token provided

DB_PATH = os.getenv("DB_PATH", "subscriptions.db")

# --- Pricing / plans ---
PLANS = {
    "trial7": {"days": 7, "amount_cents": 200, "label": "7-day trial - $2"},
    "month1": {"days": 30, "amount_cents": 1450, "label": "1 month - $14.5"},
    "year12": {"days": 365, "amount_cents": 14600, "label": "12 months - $146"},
}

# --- DB helpers ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        chat_id INTEGER,
        subscription_end TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS purchases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        session_id TEXT,
        plan_id TEXT,
        amount INTEGER,
        currency TEXT,
        created_at TEXT
    )
    """)
    conn.commit()
    conn.close()


def set_subscription(user_id: int, username: str, chat_id: int, days: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT subscription_end FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    now = datetime.utcnow()
    if row and row[0]:
        try:
            current_end = datetime.fromisoformat(row[0])
        except Exception:
            current_end = now
        if current_end > now:
            new_end = current_end + timedelta(days=days)
        else:
            new_end = now + timedelta(days=days)
        cur.execute("UPDATE users SET subscription_end = ?, username = ?, chat_id = ? WHERE user_id = ?",
                    (new_end.isoformat(), username, chat_id, user_id))
    else:
        new_end = now + timedelta(days=days)
        cur.execute("INSERT OR REPLACE INTO users(user_id, username, chat_id, subscription_end) VALUES(?,?,?,?)",
                    (user_id, username, chat_id, new_end.isoformat()))
    conn.commit()
    conn.close()
    return new_end


def get_subscription_end(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT subscription_end FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    if not row or not row[0]:
        return None
    return datetime.fromisoformat(row[0])


def record_purchase(user_id:int, session_id:str, plan_id:str, amount:int, currency:str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT INTO purchases(user_id, session_id, plan_id, amount, currency, created_at) VALUES(?,?,?,?,?,?)",
                (user_id, session_id, plan_id, amount, currency, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()


# --- Bot handlers ---
ADMIN_IDS = set(map(int, os.getenv("ADMIN_IDS","").split(","))) if os.getenv("ADMIN_IDS") else set()

if bot:
    @dp.message_handler(commands=["start"])
    async def cmd_start(message: types.Message):
        text = (
            "Привет! Я бот для продажи подписок на соцсети.\n\n"
            "/buy — купить подписку\n"
            "/status — проверить подписку\n"
            "/content — закрытый контент (только для подписчиков)\n"
        )
        await message.answer(text)

    @dp.message_handler(commands=["buy"])
    async def cmd_buy(message: types.Message):
        kb = types.InlineKeyboardMarkup()
        for pid, p in PLANS.items():
            kb.add(types.InlineKeyboardButton(text=p["label"], callback_data=f"buy:{pid}"))
        await message.answer("Выберите тариф:", reply_markup=kb)

    @dp.callback_query_handler(lambda c: c.data and c.data.startswith("buy:"))
    async def process_buy(call: types.CallbackQuery):
        plan_id = call.data.split(":",1)[1]
        plan = PLANS.get(plan_id)
        if not plan:
            await call.message.answer("План не найден")
            return
        user = call.from_user
        try:
            session = stripe.checkout.Session.create(
                payment_method_types=["card"],
                mode="payment",
                client_reference_id=str(user.id),
                line_items=[{
                    "price_data": {
                        "currency": "usd",
                        "product_data": {"name": f"Подписка: {plan['label']}"},
                        "unit_amount": plan['amount_cents'],
                    },
                    "quantity": 1,
                }],
                metadata={"plan_id": plan_id, "user_id": str(user.id)},
                success_url=BASE_URL + "/success.html",
                cancel_url=BASE_URL + "/cancel.html",
            )
            await call.message.answer(f"Перейдите по ссылке для оплаты:\n{session.url}")
        except Exception as e:
            await call.message.answer("Ошибка при создании платежа: " + str(e))

    @dp.message_handler(commands=["status"])
    async def cmd_status(message: types.Message):
        user_id = message.from_user.id
        end = get_subscription_end(user_id)
        now = datetime.utcnow()
        if end and end > now:
            await message.answer(f"Ваша подписка активна до {end.isoformat()} UTC")
        else:
            await message.answer("У вас нет активной подписки.")

    @dp.message_handler(commands=["content"])
    async def cmd_content(message: types.Message):
        user_id = message.from_user.id
        end = get_subscription_end(user_id)
        now = datetime.utcnow()
        if end and end > now:
            await message.answer("Закрытый контент: ссылка на аккаунты/файлы ...")
        else:
            await message.answer("Доступ только для подписчиков. Купите подписку: /buy")

    @dp.message_handler(commands=["users"])
    async def cmd_users(message: types.Message):
        if message.from_user.id not in ADMIN_IDS:
            await message.answer("Только админы.")
            return
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT user_id, username, subscription_end FROM users")
        rows = cur.fetchall()
        conn.close()
        if not rows:
            await message.answer("Нет пользователей.")
            return
        text = "Пользователи:\n"
        for r in rows:
            text += f"{r[0]} | @{r[1] or '—'} | {r[2] or 'no sub'}\n"
        await message.answer(text)

# --- Flask webhook for Stripe ---
app = Flask(__name__)

@app.route("/stripe-webhook", methods=["POST"])
def stripe_webhook():
    payload = request.get_data()
    sig_header = request.headers.get("stripe-signature", None)
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        print("Webhook error:", e)
        return abort(400)

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        client_ref = session.get("client_reference_id")
        plan_id = session.get("metadata", {}).get("plan_id")
        if client_ref and plan_id and plan_id in PLANS:
            try:
                user_id = int(client_ref)
                days = PLANS[plan_id]["days"]
                new_end = set_subscription(user_id, "", user_id, days=days)
                record_purchase(user_id, session.get("id"), plan_id, session.get("amount_total",0), session.get("currency",""))
                # notify user
                import asyncio
                if bot:
                    asyncio.get_event_loop().create_task(bot.send_message(chat_id=user_id, text=f"Спасибо! Подписка активирована до {new_end.isoformat()} UTC"))
            except Exception as e:
                print("Error processing session:", e)

    return "", 200

# Run Flask in thread and aiogram polling

def run_flask():
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))

if __name__ == "__main__":
    init_db()
    t = threading.Thread(target=run_flask)
    t.start()
    if bot:
        executor.start_polling(dp, skip_updates=True)
    else:
        print("BOT_TOKEN not set; bot polling not started. Flask webhook still running.")
