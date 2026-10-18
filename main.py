# main.py
# Магазин подписок в Telegram (aiogram 2.x) + оплата через Stripe Checkout.
#
# Как это работает:
#   1. Покупатель выбирает сервис и тариф в меню с кнопками.
#   2. Нажимает "Оплатить" (кнопка-ссылка), платит картой.
#   3. Возвращается в бота, нажимает "Я оплатил" - бот проверяет оплату у Stripe.
#   4. Бот просит email и пароль от аккаунта для оформления подписки.
#   5. Админу (продавцу) приходит сообщение "Новый заказ!" со всеми данными и сроком.
#   6. Продавец оформляет подписку и нажимает "Заказ выполнен" - покупателю уходит уведомление.
#
# Переменные окружения (Variables на хостинге):
#   BOT_TOKEN   - токен от @BotFather            (обязательно)
#   STRIPE_KEY  - секретный ключ Stripe sk_...   (обязательно)
# Необязательные:
#   ADMIN_IDS   - доп. админы через запятую
#   SUPPORT_URL - ссылка на поддержку (по умолчанию https://t.me/Sainnapari)
#   BASE_URL    - адрес сервиса для страниц success/cancel
#   DB_PATH     - путь к SQLite (по умолчанию subscriptions.db)

import os
import re
import sys
import html
import sqlite3
import threading
from datetime import datetime

import stripe
from flask import Flask
from aiogram import Bot, Dispatcher, types, executor
from aiogram.utils.exceptions import MessageNotModified

sys.stdout.reconfigure(line_buffering=True)  # чтобы print сразу попадал в логи

# ---------- Настройки ----------
# В getenv пишем ТОЛЬКО ИМЯ переменной. Сами значения лежат в Variables на хостинге.
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
STRIPE_KEY = (os.getenv("STRIPE_KEY") or "").strip()
BASE_URL = (os.getenv("BASE_URL") or "https://t.me").rstrip("/")
DB_PATH = os.getenv("DB_PATH", "subscriptions.db")
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://t.me/Sainnapari")

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

bot = Bot(token=BOT_TOKEN, parse_mode=types.ParseMode.HTML)
dp = Dispatcher(bot)

h = html.escape
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# ---------- Каталог ----------
# Тариф: (id, название на кнопке, цена в центах, срок для админа "оформить на ...")
CATALOG = {
    "adobe": {
        "emoji": "📱", "title": "Adobe Creative Cloud Pro", "note": "Individual plan",
        "groups": [
            {"title": None, "plans": [
                ("adobe_7d", "7 дней (пробный)", 200, "7 дней (пробный)"),
                ("adobe_1m", "1 месяц", 1450, "1 месяц"),
                ("adobe_12m", "12 месяцев", 14600, "12 месяцев"),
            ]},
        ],
    },
    "canva": {
        "emoji": "⚪", "title": "Canva Pro", "note": "",
        "groups": [
            {"title": None, "plans": [
                ("canva_30t", "30 дней (пробный)", 200, "30 дней (пробный)"),
                ("canva_1d", "1 день", 120, "1 день"),
                ("canva_1w", "1 неделя", 250, "1 неделя"),
                ("canva_1m", "1 месяц", 618, "1 месяц"),
                ("canva_12m", "12 месяцев", 4600, "12 месяцев"),
            ]},
        ],
    },
    "netflix": {
        "emoji": "🎥", "title": "Netflix", "note": "",
        "groups": [
            {"title": None, "plans": [
                # Срок для Netflix в прайсе не указан - поставлен 1 месяц, поправьте при необходимости
                ("nf_premium", "Premium 4K", 1000, "1 месяц"),
                ("nf_mobile", "Mobile", 300, "1 месяц"),
                ("nf_basic", "Basic", 400, "1 месяц"),
                ("nf_standard", "Standard", 700, "1 месяц"),
            ]},
        ],
    },
    "crunchy": {
        "emoji": "⛩", "title": "Crunchyroll", "note": "",
        "groups": [
            {"title": "Fan", "plans": [
                ("cr_fan_1m", "1 месяц", 150, "1 месяц"),
                ("cr_fan_12m", "12 месяцев", 540, "12 месяцев"),
            ]},
            {"title": "Mega Fan", "plans": [
                ("cr_mega_1m", "1 месяц", 180, "1 месяц"),
                ("cr_mega_12m", "12 месяцев", 700, "12 месяцев"),
            ]},
            {"title": "Ultimate Fan", "plans": [
                ("cr_ult_1m", "1 месяц", 400, "1 месяц"),
                ("cr_ult_12m", "12 месяцев", 1270, "12 месяцев"),
            ]},
        ],
    },
}

PLANS = {}
for _skey, _svc in CATALOG.items():
    for _gi, _grp in enumerate(_svc["groups"]):
        for _pid, _label, _cents, _period in _grp["plans"]:
            PLANS[_pid] = {
                "svc": _skey, "gi": _gi, "group": _grp["title"],
                "label": _label, "cents": _cents, "period": _period,
            }


def money(cents):
    s = f"{cents / 100:.2f}".rstrip("0").rstrip(".")
    return "$" + s


def plan_title(p):
    name = CATALOG[p["svc"]]["title"]
    if p["group"]:
        name += " " + p["group"]
    return name


def back_target(p):
    """Куда ведёт кнопка 'Назад' с экрана тарифа."""
    svc = CATALOG[p["svc"]]
    if len(svc["groups"]) > 1:
        return f"grp:{p['svc']}:{p['gi']}"
    return f"svc:{p['svc']}"


STATUS_TEXT = {
    "new": "⏳ ждёт оплаты",
    "wait_email": "✉️ ждём ваш email",
    "wait_password": "🔑 ждём пароль",
    "submitted": "🛠 в работе",
    "done": "✅ выполнен",
}


def as_dict(obj):
    """Новые версии stripe возвращают не dict, а StripeObject - приводим к обычному dict."""
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return dict(obj)


# ---------- База данных ----------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def now_iso():
    return datetime.utcnow().isoformat()


def init_db():
    conn = db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            plan_id TEXT,
            price_cents INTEGER,
            session_id TEXT,
            status TEXT,
            email TEXT,
            created_at TEXT,
            paid_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def create_order(user, plan_id, cents):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO orders(user_id, username, plan_id, price_cents, status, created_at) VALUES(?,?,?,?,?,?)",
        (user.id, user.username or "", plan_id, cents, "new", now_iso()),
    )
    oid = cur.lastrowid
    conn.commit()
    conn.close()
    return oid


def get_order(oid):
    conn = db()
    row = conn.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_order(oid, **fields):
    keys = list(fields.keys())
    sql = "UPDATE orders SET " + ", ".join(f"{k} = ?" for k in keys) + " WHERE id = ?"
    conn = db()
    conn.execute(sql, [fields[k] for k in keys] + [oid])
    conn.commit()
    conn.close()


def claim_paid(oid):
    """Атомарно переводит заказ из 'new' в 'wait_email'. True - если это сделали мы (защита от двойного нажатия)."""
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE orders SET status = 'wait_email', paid_at = ? WHERE id = ? AND status = 'new'",
        (now_iso(), oid),
    )
    ok = cur.rowcount > 0
    conn.commit()
    conn.close()
    return ok


def get_awaiting_order(user_id):
    conn = db()
    row = conn.execute(
        "SELECT * FROM orders WHERE user_id = ? AND status IN ('wait_email','wait_password') "
        "ORDER BY id DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def user_orders(user_id, limit=10):
    conn = db()
    rows = conn.execute(
        "SELECT * FROM orders WHERE user_id = ? AND status != 'new' ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def all_orders(limit=20):
    conn = db()
    rows = conn.execute("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------- Клавиатуры и тексты ----------
def btn(text, data=None, url=None):
    if url:
        return types.InlineKeyboardButton(text=text, url=url)
    return types.InlineKeyboardButton(text=text, callback_data=data)


def main_menu():
    k = types.InlineKeyboardMarkup(row_width=1)
    k.add(btn("🛍 Каталог", "cat"))
    k.add(btn("📦 Мои заказы", "orders"))
    k.add(btn("💬 Поддержка", url=SUPPORT_URL))
    return k


def welcome_text():
    return (
        "👋 <b>Добро пожаловать!</b>\n\n"
        "Здесь можно оформить подписки на <b>Adobe</b>, <b>Canva</b>, <b>Netflix</b> и <b>Crunchyroll</b>.\n\n"
        f"Остались вопросы? Напишите в <a href=\"{SUPPORT_URL}\">поддержку</a>.\n\n"
        "Выберите действие 👇"
    )


async def show(call, text, markup):
    """Меняет текущее сообщение (чтобы меню не плодило новые сообщения)."""
    try:
        await call.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    except MessageNotModified:
        pass
    except Exception:
        await call.message.answer(text, reply_markup=markup, disable_web_page_preview=True)


# ---------- Меню ----------
@dp.message_handler(commands=["start", "menu"])
async def cmd_start(message: types.Message):
    await message.answer(welcome_text(), reply_markup=main_menu(), disable_web_page_preview=True)


@dp.callback_query_handler(lambda c: c.data == "menu")
async def cb_menu(call: types.CallbackQuery):
    await call.answer()
    await show(call, welcome_text(), main_menu())


@dp.callback_query_handler(lambda c: c.data == "cat")
async def cb_catalog(call: types.CallbackQuery):
    await call.answer()
    k = types.InlineKeyboardMarkup(row_width=1)
    for skey, svc in CATALOG.items():
        k.add(btn(f"{svc['emoji']} {svc['title']}", f"svc:{skey}"))
    k.add(btn("⬅️ Назад", "menu"))
    await show(call, "🛍 <b>Каталог</b>\n\nВыберите сервис:", k)


def plans_screen(skey, gi):
    svc = CATALOG[skey]
    grp = svc["groups"][gi]
    title = f"{svc['emoji']} <b>{h(svc['title'])}</b>"
    if grp["title"]:
        title += f" — <b>{h(grp['title'])}</b>"
    if svc["note"]:
        title += f"\n<i>{h(svc['note'])}</i>"
    k = types.InlineKeyboardMarkup(row_width=1)
    for pid, label, cents, _period in grp["plans"]:
        k.add(btn(f"{label} — {money(cents)}", f"plan:{pid}"))
    k.add(btn("⬅️ Назад", "cat" if len(svc["groups"]) == 1 else f"svc:{skey}"))
    return title + "\n\nВыберите тариф 👇", k


@dp.callback_query_handler(lambda c: c.data and c.data.startswith("svc:"))
async def cb_service(call: types.CallbackQuery):
    await call.answer()
    skey = call.data.split(":", 1)[1]
    svc = CATALOG.get(skey)
    if not svc:
        return
    if len(svc["groups"]) == 1:
        text, k = plans_screen(skey, 0)
        await show(call, text, k)
        return
    k = types.InlineKeyboardMarkup(row_width=1)
    for gi, grp in enumerate(svc["groups"]):
        k.add(btn(grp["title"], f"grp:{skey}:{gi}"))
    k.add(btn("⬅️ Назад", "cat"))
    await show(call, f"{svc['emoji']} <b>{h(svc['title'])}</b>\n\nВыберите уровень подписки:", k)


@dp.callback_query_handler(lambda c: c.data and c.data.startswith("grp:"))
async def cb_group(call: types.CallbackQuery):
    await call.answer()
    try:
        _, skey, gi = call.data.split(":")
        text, k = plans_screen(skey, int(gi))
    except Exception:
        return
    await show(call, text, k)


@dp.callback_query_handler(lambda c: c.data and c.data.startswith("plan:"))
async def cb_plan(call: types.CallbackQuery):
    await call.answer()
    pid = call.data.split(":", 1)[1]
    p = PLANS.get(pid)
    if not p:
        return
    text = (
        f"🧾 <b>{h(plan_title(p))}</b>\n\n"
        f"Тариф: <b>{h(p['label'])}</b>\n"
        f"Срок: <b>{h(p['period'])}</b>\n"
        f"Цена: <b>{money(p['cents'])}</b>\n\n"
        "После оплаты бот попросит <b>email</b> и <b>пароль</b> от вашего аккаунта, "
        "чтобы оформить подписку."
    )
    k = types.InlineKeyboardMarkup(row_width=1)
    k.add(btn(f"💳 Оплатить {money(p['cents'])}", f"pay:{pid}"))
    k.add(btn("⬅️ Назад", back_target(p)))
    await show(call, text, k)


# ---------- Оплата ----------
@dp.callback_query_handler(lambda c: c.data and c.data.startswith("pay:"))
async def cb_pay(call: types.CallbackQuery):
    await call.answer()
    pid = call.data.split(":", 1)[1]
    p = PLANS.get(pid)
    if not p:
        return
    user = call.from_user
    oid = create_order(user, pid, p["cents"])
    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            client_reference_id=str(user.id),
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": f"{plan_title(p)} - {p['label']}"},
                    "unit_amount": p["cents"],
                },
                "quantity": 1,
            }],
            metadata={"order_id": str(oid), "plan_id": pid, "user_id": str(user.id)},
            success_url=BASE_URL + "/success.html",
            cancel_url=BASE_URL + "/cancel.html",
        )
    except Exception as e:
        print("Stripe error:", e, flush=True)
        await call.message.answer(
            "⚠️ Не удалось создать платёж. Попробуйте ещё раз позже или напишите в "
            f"<a href=\"{SUPPORT_URL}\">поддержку</a>."
        )
        return

    update_order(oid, session_id=session.id)
    text = (
        f"🧾 <b>Заказ #{oid}</b>\n"
        f"{h(plan_title(p))} — {h(p['label'])}\n"
        f"К оплате: <b>{money(p['cents'])}</b>\n\n"
        "1️⃣ Нажмите «Оплатить» и оплатите картой\n"
        "2️⃣ Вернитесь сюда и нажмите «Я оплатил»"
    )
    k = types.InlineKeyboardMarkup(row_width=1)
    k.add(btn(f"💳 Оплатить {money(p['cents'])}", url=session.url))
    k.add(btn("✅ Я оплатил", f"chk:{oid}"))
    k.add(btn("⬅️ Назад", f"plan:{pid}"))
    await show(call, text, k)


def ask_email_text(order):
    p = PLANS[order["plan_id"]]
    return (
        "✅ <b>Оплата получена, спасибо!</b>\n\n"
        f"Для оформления подписки <b>{h(plan_title(p))}</b> отправьте "
        "<b>email</b> от вашего аккаунта одним сообщением 👇"
    )


ASK_PASSWORD_TEXT = (
    "Отлично! Теперь отправьте <b>пароль</b> от аккаунта 🔑\n\n"
    "🔒 Пароль не сохраняется в базе бота, а ваше сообщение с ним будет удалено из чата. "
    "Данные уходят только продавцу для оформления заказа. "
    "После оформления рекомендуем сменить пароль."
)


@dp.callback_query_handler(lambda c: c.data and c.data.startswith("chk:"))
async def cb_check(call: types.CallbackQuery):
    try:
        oid = int(call.data.split(":", 1)[1])
    except ValueError:
        await call.answer()
        return
    order = get_order(oid)
    if not order or order["user_id"] != call.from_user.id:
        await call.answer("Заказ не найден. Начните заново: /start", show_alert=True)
        return

    status = order["status"]
    if status == "wait_email":
        await call.answer()
        await call.message.answer(ask_email_text(order))
        return
    if status == "wait_password":
        await call.answer()
        await call.message.answer(ASK_PASSWORD_TEXT)
        return
    if status in ("submitted", "done"):
        await call.answer("Этот заказ уже принят в работу ✅", show_alert=True)
        return

    # status == "new": проверяем оплату у Stripe
    try:
        session = as_dict(stripe.checkout.Session.retrieve(order["session_id"]))
    except Exception as e:
        print("Stripe retrieve error:", e, flush=True)
        await call.answer("Не удалось проверить оплату. Попробуйте через минуту.", show_alert=True)
        return

    if session.get("payment_status") != "paid":
        await call.answer(
            "Оплата пока не поступила. Оплатите по кнопке «Оплатить» и нажмите «Я оплатил» ещё раз.",
            show_alert=True,
        )
        return

    await call.answer()
    if claim_paid(oid):
        order = get_order(oid)
        await call.message.answer(ask_email_text(order))


# ---------- Получение email и пароля ----------
@dp.message_handler(lambda m: m.text and not m.text.startswith("/"))
async def on_text(message: types.Message):
    order = get_awaiting_order(message.from_user.id)
    if not order:
        await message.answer("Выберите действие в меню 👇", reply_markup=main_menu())
        return

    if order["status"] == "wait_email":
        email = message.text.strip()
        if not EMAIL_RE.match(email):
            await message.answer("Это не похоже на email. Отправьте адрес в формате <code>name@mail.com</code>")
            return
        update_order(order["id"], email=email, status="wait_password")
        await message.answer(ASK_PASSWORD_TEXT)
        return

    if order["status"] == "wait_password":
        password = message.text.strip()
        try:
            await message.delete()  # убираем пароль из чата
        except Exception:
            pass
        update_order(order["id"], status="submitted")
        order = get_order(order["id"])
        sent = await notify_admins(order, message.from_user, order["email"], password)
        if sent:
            await message.answer(
                f"🎉 <b>Заказ #{order['id']} принят!</b>\n\n"
                "Продавец оформит подписку, и вы получите уведомление здесь. "
                f"Если нужна помощь — <a href=\"{SUPPORT_URL}\">поддержка</a>.",
                reply_markup=main_menu(), disable_web_page_preview=True,
            )
        else:
            await message.answer(
                f"⚠️ Заказ #{order['id']} оплачен, но не удалось уведомить продавца. "
                f"Напишите, пожалуйста, в <a href=\"{SUPPORT_URL}\">поддержку</a> и назовите номер заказа.",
                disable_web_page_preview=True,
            )


async def notify_admins(order, user, email, password):
    p = PLANS[order["plan_id"]]
    who = f"<a href=\"tg://user?id={user.id}\">{h(user.full_name)}</a>"
    if user.username:
        who += f" (@{h(user.username)})"
    text = (
        f"🆕 <b>Новый заказ #{order['id']}!</b>\n\n"
        f"📦 Сервис: <b>{h(plan_title(p))}</b>\n"
        f"🏷 Тариф: {h(p['label'])}\n"
        f"⏱ Оформить на: <b>{h(p['period'])}</b>\n"
        f"💰 Оплачено: {money(order['price_cents'])}\n\n"
        f"👤 Покупатель: {who}\n"
        f"📧 Email: <code>{h(email)}</code>\n"
        f"🔑 Пароль: <code>{h(password)}</code>"
    )
    k = types.InlineKeyboardMarkup()
    k.add(btn("✅ Заказ выполнен", f"done:{order['id']}"))
    sent = 0
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, text, reply_markup=k)
            sent += 1
        except Exception as e:
            # Админ должен хотя бы раз нажать /start у бота, иначе Telegram не даст написать ему первым
            print(f"Не удалось отправить админу {aid}:", e, flush=True)
    return sent


@dp.callback_query_handler(lambda c: c.data and c.data.startswith("done:"))
async def cb_done(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("Только для админов", show_alert=True)
        return
    try:
        oid = int(call.data.split(":", 1)[1])
    except ValueError:
        await call.answer()
        return
    order = get_order(oid)
    if not order:
        await call.answer("Заказ не найден", show_alert=True)
        return
    if order["status"] == "done":
        await call.answer("Уже отмечен выполненным")
        return
    update_order(oid, status="done")
    await call.answer("Отмечено выполненным ✅")
    try:
        await call.message.edit_text(
            call.message.html_text + "\n\n✅ <b>ВЫПОЛНЕНО</b>", reply_markup=None
        )
    except Exception:
        pass
    p = PLANS.get(order["plan_id"])
    name = plan_title(p) if p else "подписка"
    try:
        await bot.send_message(
            order["user_id"],
            f"🎉 <b>Заказ #{oid} выполнен!</b>\n\n"
            f"Подписка <b>{h(name)}</b> оформлена. Приятного использования!\n"
            f"Вопросы — <a href=\"{SUPPORT_URL}\">поддержка</a>.",
            disable_web_page_preview=True,
        )
    except Exception as e:
        print("Не удалось уведомить покупателя:", e, flush=True)


# ---------- Мои заказы ----------
@dp.callback_query_handler(lambda c: c.data == "orders")
async def cb_orders(call: types.CallbackQuery):
    await call.answer()
    rows = user_orders(call.from_user.id)
    if not rows:
        text = "📦 <b>Мои заказы</b>\n\nПока заказов нет. Загляните в каталог 🛍"
    else:
        lines = ["📦 <b>Мои заказы</b>\n"]
        for r in rows:
            p = PLANS.get(r["plan_id"])
            name = f"{plan_title(p)} — {p['label']}" if p else r["plan_id"]
            lines.append(f"#{r['id']} · {h(name)}\n   {STATUS_TEXT.get(r['status'], r['status'])}")
        text = "\n".join(lines)
    k = types.InlineKeyboardMarkup(row_width=1)
    k.add(btn("🛍 Каталог", "cat"))
    k.add(btn("⬅️ Назад", "menu"))
    await show(call, text, k)


# ---------- Админ-команды ----------
@dp.message_handler(commands=["orders"])
async def cmd_admin_orders(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    rows = all_orders()
    if not rows:
        await message.answer("Заказов пока нет.")
        return
    lines = ["📋 <b>Последние заказы</b>\n"]
    for r in rows:
        p = PLANS.get(r["plan_id"])
        name = f"{plan_title(p)} — {p['label']}" if p else r["plan_id"]
        who = f"@{h(r['username'])}" if r["username"] else f"id {r['user_id']}"
        lines.append(f"#{r['id']} · {h(name)} · {money(r['price_cents'])} · {who}\n   {STATUS_TEXT.get(r['status'], r['status'])}")
    await message.answer("\n".join(lines)[:4000])


# ---------- Flask: страницы после оплаты ----------
app = Flask(__name__)


@app.route("/")
def index():
    return "ok", 200


@app.route("/success.html")
def success_page():
    return "<h2>Оплата прошла!</h2><p>Вернитесь в Telegram и нажмите «Я оплатил».</p>"


@app.route("/cancel.html")
def cancel_page():
    return "<h2>Оплата отменена.</h2><p>Вернитесь в Telegram и попробуйте снова.</p>"


def run_flask():
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))


# ---------- Запуск ----------
if __name__ == "__main__":
    init_db()
    threading.Thread(target=run_flask, daemon=True).start()
    executor.start_polling(dp, skip_updates=True)
