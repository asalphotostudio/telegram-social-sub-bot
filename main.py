# main.py
# Telegram-бот (aiogram 2.x) для продажи подписок через Stripe Checkout.
# Вебхуки НЕ обязательны: после оплаты пользователь нажимает кнопку "Я оплатил",
# бот сам спрашивает у Stripe, прошла ли оплата, и активирует подписку.
#
# Обязательные переменные окружения:
#   BOT_TOKEN   - токен от @BotFather
#   STRIPE_KEY  - секретный ключ Stripe (для демо используйте sk_test_...)
# Необязательные:
#   BASE_URL               - публичный адрес сервиса (для страниц success/cancel)
#   STRIPE_WEBHOOK_SECRET  - если захотите включить вебхук (whsec_...)
#   ADMIN_IDS              - доп. админы через запятую
#   CONTENT_TEXT           - текст для подписчиков по /content
#   DB_PATH                - путь к SQLite (по умолчанию subscriptions.db)

import os
import sys
import json
import sqlite3
import threading
import urllib.request
from datetime import datetime, timedelta

import stripe
from flask import Flask, request, abort
from aiogram import Bot, Dispatcher, types, executor

# ---------- Настройки ----------
sys.stdout.reconfigure(line_buffering=True)  # чтобы print сразу попадал в логи

# В getenv пишем ТОЛЬКО ИМЯ переменной. Сами значения лежат в Variables на хостинге.
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
STRIPE_KEY = (os.getenv("STRIPE_KEY") or "").strip()
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")  # необязательно
BASE_URL = (os.getenv("BASE_URL") or "https://t.me").rstrip("/")
DB_PATH = os.getenv("DB_PATH", "subscriptions.db")
CONTENT_TEXT = os.getenv("CONTENT_TEXT", "Закрытый контент: ссылки на аккаунты/файлы ...")

# Админ по умолчанию + любые из переменной ADMIN_IDS
ADMIN_IDS = {8292954392}
for _x in os.getenv("ADMIN_IDS", "").split(","):
    _x = _x.strip()
    if _x.isdigit():
        ADMIN_IDS.add(int(_x))

missing = [n for n, v in [("BOT_TOKEN", BOT_TOKEN), ("STRIPE_KEY", STRIPE_KEY)] if not v]
if missing:
    print("Не заданы переменные окружения: " + ", ".join(missing))
    sys.exit(1)

stripe.api_key = STRIPE_KEY

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# ---------- Тарифы ----------
PLANS = {
    "trial7": {"days": 7, "amount_cents": 200, "label": "7 дней (пробный) - $2"},
    "month1": {"days": 30, "amount_cents": 1450, "label": "1 месяц - $14.5"},
    "year12": {"days": 365, "amount_cents": 14600, "label": "12 месяцев - $146"},
}


def as_dict(obj):
    """Новые версии stripe возвращают не dict, а StripeObject - приводим к обычному dict."""
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return dict(obj)


# ---------- База данных ----------
def db():
    return sqlite3.connect(DB_PATH)


def init_db():
    conn = db()
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
            session_id TEXT UNIQUE,
            plan_id TEXT,
            amount INTEGER,
            currency TEXT,
            created_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pending (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            session_id TEXT,
            plan_id TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def set_subscription(user_id, username, chat_id, days):
    """Продлевает подписку (если активна - добавляет дни к текущему сроку)."""
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT subscription_end FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    now = datetime.utcnow()

    if row and row[0]:
        try:
            current_end = datetime.fromisoformat(row[0])
        except Exception:
            current_end = now
        base = current_end if current_end > now else now
        new_end = base + timedelta(days=days)
        if username:
            cur.execute(
                "UPDATE users SET subscription_end = ?, username = ?, chat_id = ? WHERE user_id = ?",
                (new_end.isoformat(), username, chat_id, user_id),
            )
        else:
            cur.execute(
                "UPDATE users SET subscription_end = ?, chat_id = ? WHERE user_id = ?",
                (new_end.isoformat(), chat_id, user_id),
            )
    else:
        new_end = now + timedelta(days=days)
        cur.execute(
            "INSERT OR REPLACE INTO users(user_id, username, chat_id, subscription_end) VALUES(?,?,?,?)",
            (user_id, username or "", chat_id, new_end.isoformat()),
        )
    conn.commit()
    conn.close()
    return new_end


def get_subscription_end(user_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT subscription_end FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    if not row or not row[0]:
        return None
    try:
        return datetime.fromisoformat(row[0])
    except Exception:
        return None


def has_active_subscription(user_id):
    end = get_subscription_end(user_id)
    return bool(end and end > datetime.utcnow())


def add_pending(user_id, username, session_id, plan_id):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO pending(user_id, username, session_id, plan_id, created_at) VALUES(?,?,?,?,?)",
        (user_id, username or "", session_id, plan_id, datetime.utcnow().isoformat()),
    )
    pid = cur.lastrowid
    conn.commit()
    conn.close()
    return pid


def get_pending(pid):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT user_id, username, session_id, plan_id FROM pending WHERE id = ?", (pid,))
    row = cur.fetchone()
    conn.close()
    return row


def activate_purchase(user_id, username, session_id, plan_id, amount, currency):
    """
    Засчитывает оплату ровно один раз (по session_id).
    Возвращает новую дату окончания или None, если эта оплата уже была засчитана.
    """
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO purchases(user_id, session_id, plan_id, amount, currency, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (user_id, session_id, plan_id, amount or 0, currency or "", datetime.utcnow().isoformat()),
    )
    inserted = cur.rowcount > 0
    conn.commit()
    conn.close()
    if not inserted:
        return None
    return set_subscription(user_id, username, user_id, PLANS[plan_id]["days"])


# ---------- Telegram: обработчики ----------
@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message):
    await message.answer(
        "Привет! Я бот для продажи подписок.\n\n"
        "/buy - купить подписку\n"
        "/status - проверить подписку\n"
        "/content - закрытый контент (только для подписчиков)\n"
    )


@dp.message_handler(commands=["buy"])
async def cmd_buy(message: types.Message):
    kb = types.InlineKeyboardMarkup()
    for pid, p in PLANS.items():
        kb.add(types.InlineKeyboardButton(text=p["label"], callback_data=f"buy:{pid}"))
    await message.answer("Выберите тариф:", reply_markup=kb)


@dp.callback_query_handler(lambda c: c.data and c.data.startswith("buy:"))
async def process_buy(call: types.CallbackQuery):
    await call.answer()
    plan_id = call.data.split(":", 1)[1]
    plan = PLANS.get(plan_id)
    if not plan:
        await call.message.answer("Тариф не найден.")
        return

    user = call.from_user
    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            client_reference_id=str(user.id),
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": f"Подписка: {plan['label']}"},
                    "unit_amount": plan["amount_cents"],
                },
                "quantity": 1,
            }],
            metadata={
                "plan_id": plan_id,
                "user_id": str(user.id),
                "username": user.username or "",
            },
            success_url=BASE_URL + "/success.html",
            cancel_url=BASE_URL + "/cancel.html",
        )
    except Exception as e:
        print("Stripe error:", e, flush=True)
        await call.message.answer("Ошибка при создании платежа: " + str(e)[:300])
        return

    pending_id = add_pending(user.id, user.username, session.id, plan_id)
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"check:{pending_id}"))
    await call.message.answer(
        f"1) Оплатите по ссылке:\n{session.url}\n\n"
        f"2) Вернитесь сюда и нажмите «Я оплатил» - подписка включится сразу.",
        reply_markup=kb,
    )


@dp.callback_query_handler(lambda c: c.data and c.data.startswith("check:"))
async def process_check(call: types.CallbackQuery):
    await call.answer()
    try:
        pid = int(call.data.split(":", 1)[1])
    except ValueError:
        return
    row = get_pending(pid)
    if not row or row[0] != call.from_user.id:
        await call.message.answer("Платёж не найден. Начните заново: /buy")
        return
    user_id, username, session_id, plan_id = row

    try:
        session = as_dict(stripe.checkout.Session.retrieve(session_id))
    except Exception as e:
        print("Stripe retrieve error:", e, flush=True)
        await call.message.answer("Не удалось проверить оплату: " + str(e)[:300])
        return

    if session.get("payment_status") != "paid":
        await call.message.answer(
            "Оплата пока не поступила. Оплатите по ссылке и нажмите кнопку «Я оплатил» ещё раз."
        )
        return

    new_end = activate_purchase(
        user_id, username, session_id, plan_id,
        session.get("amount_total"), session.get("currency"),
    )
    if new_end is None:
        await call.message.answer("Эта оплата уже засчитана. Проверить подписку: /status")
    else:
        await call.message.answer(
            f"Спасибо за оплату! Подписка активна до {new_end.strftime('%Y-%m-%d %H:%M')} UTC.\n"
            f"Закрытый контент: /content"
        )


@dp.message_handler(commands=["status"])
async def cmd_status(message: types.Message):
    end = get_subscription_end(message.from_user.id)
    if end and end > datetime.utcnow():
        await message.answer(f"Ваша подписка активна до {end.strftime('%Y-%m-%d %H:%M')} UTC")
    else:
        await message.answer("У вас нет активной подписки. Купить: /buy")


@dp.message_handler(commands=["content"])
async def cmd_content(message: types.Message):
    if has_active_subscription(message.from_user.id):
        await message.answer(CONTENT_TEXT)
    else:
        await message.answer("Доступ только для подписчиков. Купите подписку: /buy")


# ---------- Админ-команды ----------
@dp.message_handler(commands=["users"])
async def cmd_users(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Только для админов.")
        return
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT user_id, username, subscription_end FROM users")
    rows = cur.fetchall()
    conn.close()
    if not rows:
        await message.answer("Пользователей нет.")
        return
    text = "Пользователи:\n"
    for r in rows:
        text += f"{r[0]} | @{r[1] or '-'} | {r[2] or 'нет подписки'}\n"
    await message.answer(text[:4000])


@dp.message_handler(commands=["grant"])
async def cmd_grant(message: types.Message):
    """Админ: /grant <user_id> <дней> - выдать подписку без оплаты (удобно для демо)."""
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Только для админов.")
        return
    parts = message.text.split()
    if len(parts) != 3 or not parts[1].lstrip("-").isdigit() or not parts[2].isdigit():
        await message.answer("Использование: /grant <user_id> <дней>")
        return
    uid, days = int(parts[1]), int(parts[2])
    end = set_subscription(uid, "", uid, days)
    await message.answer(f"Выдано. Подписка пользователя {uid} до {end.strftime('%Y-%m-%d %H:%M')} UTC")


# ---------- Flask: страницы и (необязательный) вебхук ----------
app = Flask(__name__)


def notify_user(chat_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print("Не удалось отправить уведомление:", e)


@app.route("/")
def index():
    return "ok", 200


@app.route("/success.html")
def success_page():
    return "<h2>Оплата прошла!</h2><p>Вернитесь в Telegram и нажмите «Я оплатил».</p>"


@app.route("/cancel.html")
def cancel_page():
    return "<h2>Оплата отменена.</h2><p>Вернитесь в Telegram и попробуйте снова: /buy</p>"


@app.route("/stripe-webhook", methods=["POST"])
def stripe_webhook():
    if not STRIPE_WEBHOOK_SECRET:
        return abort(404)  # вебхук не настроен - он и не нужен

    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        print("Ошибка вебхука:", e)
        return abort(400)

    if event["type"] == "checkout.session.completed":
        s = as_dict(event["data"]["object"])
        meta = s.get("metadata") or {}
        plan_id = meta.get("plan_id")
        client_ref = s.get("client_reference_id")
        if s.get("payment_status") == "paid" and client_ref and plan_id in PLANS:
            try:
                user_id = int(client_ref)
                new_end = activate_purchase(
                    user_id, meta.get("username") or "", s.get("id"), plan_id,
                    s.get("amount_total"), s.get("currency"),
                )
                if new_end:
                    notify_user(
                        user_id,
                        f"Спасибо за оплату! Подписка активна до {new_end.strftime('%Y-%m-%d %H:%M')} UTC.\n"
                        f"Закрытый контент: /content",
                    )
            except Exception as e:
                print("Ошибка обработки платежа:", e)
                return abort(500)
    return "", 200


# ---------- Запуск ----------
def run_flask():
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))


if __name__ == "__main__":
    init_db()
    threading.Thread(target=run_flask, daemon=True).start()
    executor.start_polling(dp, skip_updates=True)
