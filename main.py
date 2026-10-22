# main.py
# Telegram shop bot (aiogram 2.x), English only.
#
# Catalog:  Spotify (Individual / Platinum) and PlayStation Store top-ups (INR).
# Payment:  manual. The buyer pays by Visa card transfer or crypto, uploads a screenshot,
#           and presses "I've Paid" (the button only works after a screenshot is received).
#           The screenshot goes to the admins as a "New order" with a Confirm / Reject button.
# After the admin confirms:
#           Spotify     -> the bot asks for the account email and password, then notifies the admins.
#           PlayStation -> the buyer just waits; the order is already with the admins.
# The admin marks the order "Completed" and the buyer is notified.
#
# Environment variables (Variables on your host):
#   BOT_TOKEN       - token from @BotFather                    (required)
# Optional:
#   ADMIN_IDS       - extra admin Telegram IDs, comma separated
#   SUPPORT_URL     - support link (default https://t.me/Sainnapari)
#   SHOP_NAME       - shop name shown in the welcome message
#   CARD_DETAILS    - default bank-card payment details (can also be edited in the Admin Panel)
#   CRYPTO_DETAILS  - default crypto payment details   (can also be edited in the Admin Panel)
#   DB_PATH         - SQLite path (use a Volume, e.g. /data/subscriptions.db, so data survives redeploys)

import os
import re
import sys
import html
import sqlite3
import threading
from datetime import datetime

from flask import Flask
from aiogram import Bot, Dispatcher, types, executor
from aiogram.utils.exceptions import MessageNotModified  # noqa: F401  (kept for easy extension)

try:
    sys.stdout.reconfigure(line_buffering=True)  # make print() show up in host logs immediately
except Exception:
    pass

# ---------------------------------------------------------------- settings
# os.getenv() takes the NAME of a variable. The values live in Variables on the host.
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
DB_PATH = os.getenv("DB_PATH", "subscriptions.db")
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://t.me/Sainnapari")
SHOP_NAME = os.getenv("SHOP_NAME", "Subscription Store")

ADMIN_IDS = {8292954392, 5060101097}
for _x in os.getenv("ADMIN_IDS", "").split(","):
    _x = _x.strip()
    if _x.isdigit():
        ADMIN_IDS.add(int(_x))

DEFAULT_CARD = os.getenv(
    "CARD_DETAILS",
    "Card number: 0000 0000 0000 0000\nCardholder: DEMO HOLDER\n\n(Demo details - the real ones will be added soon.)",
)
DEFAULT_CRYPTO = os.getenv(
    "CRYPTO_DETAILS",
    "Network: USDT (TRC20)\nAddress: DEMO-ADDRESS-REPLACE-ME\n\n(Demo details - the real ones will be added soon.)",
)

if not BOT_TOKEN:
    print("Missing environment variable: BOT_TOKEN")
    sys.exit(1)

bot = Bot(token=BOT_TOKEN, parse_mode=types.ParseMode.HTML)
dp = Dispatcher(bot)

h = html.escape
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# ---------------------------------------------------------------- catalog
# Plan tuple: (id, name or None, price in cents, duration in months or None)
CATALOG = {
    "spotify": {
        "emoji": "🎧", "title": "Spotify", "note": "", "needs_credentials": True,
        "groups": [
            {"title": "Individual", "emoji": "👤", "plans": [
                ("sp_ind_1m", None, 300, 1),
                ("sp_ind_12m", None, 1650, 12),
            ]},
            {"title": "Platinum", "emoji": "💎", "plans": [
                ("sp_plat_1m", None, 700, 1),
                ("sp_plat_25m", None, 1400, 2.5),
                ("sp_plat_45m", None, 2000, 4.5),
            ]},
        ],
    },
    "ps": {
        "emoji": "🎮", "title": "PlayStation", "note": "Store top-up (INR)", "needs_credentials": False,
        "groups": [
            {"title": None, "emoji": "", "plans": [
                ("ps_1000", "1000 INR", 1700, None),
                ("ps_2000", "2000 INR", 3000, None),
                ("ps_3000", "3000 INR", 4500, None),
                ("ps_4000", "4000 INR", 6000, None),
            ]},
        ],
    },
}

PLANS = {}
for _skey, _svc in CATALOG.items():
    for _gi, _grp in enumerate(_svc["groups"]):
        for _pid, _name, _cents, _months in _grp["plans"]:
            PLANS[_pid] = {
                "svc": _skey, "gi": _gi, "group": _grp["title"],
                "name": _name, "cents": _cents, "months": _months,
            }


def money(cents):
    s = f"{cents / 100:.2f}".rstrip("0").rstrip(".")
    return "$" + s


def months_text(m):
    return "1 month" if m == 1 else f"{m:g} months"


def plan_label(p):
    return p["name"] or months_text(p["months"])


def plan_title(p):
    name = CATALOG[p["svc"]]["title"]
    if p["group"]:
        name += " " + p["group"]
    return name


def plan_full(p):
    return f"{plan_title(p)} - {plan_label(p)}"


def plan_btn_text(p):
    return f"{plan_label(p)} - {money(p['cents'])}"


def todo_text(p):
    if CATALOG[p["svc"]]["needs_credentials"]:
        return f"Activate {plan_title(p)} for {months_text(p['months'])} on the buyer's account."
    return f"Deliver the PlayStation Store top-up: {p['name']}."


def needs_credentials(p):
    return CATALOG[p["svc"]]["needs_credentials"]


# ---------------------------------------------------------------- buttons
B_CATALOG = "🛍 Catalog"
B_ORDERS = "📦 My Orders"
B_PAYMENT = "💳 Payment"
B_SUPPORT = "💬 Support"
B_SETTINGS = "⚙️ Settings"
B_ABOUT = "ℹ️ About"
B_MENU = "🏠 Main Menu"
B_BACK = "⬅️ Back"
B_ORDER = "✅ Place Order"
B_PAID = "✅ I've Paid"
B_CANCEL = "❌ Cancel Order"
B_CARD = "💳 Bank Card (Visa)"
B_CRYPTO = "🪙 Crypto"
B_CHANGE = "🔄 Change Method"
B_PROFILE = "👤 Profile"
B_RULES = "📜 Rules"
B_PRIVACY = "🔒 Privacy"
B_FAQ = "❓ FAQ"
B_ADMIN = "🛠 Admin Panel"
B_A_PENDING = "⏳ Pending Payments"
B_A_ORDERS = "📋 All Orders"
B_A_STATS = "📊 Stats"
B_A_CARD = "💳 Card Details"
B_A_CRYPTO = "🪙 Crypto Details"

BTN = {
    B_CATALOG: "catalog", B_ORDERS: "orders", B_PAYMENT: "payment", B_SUPPORT: "support",
    B_SETTINGS: "settings", B_ABOUT: "about", B_MENU: "menu", B_BACK: "back",
    B_ORDER: "order", B_PAID: "paid", B_CANCEL: "cancel", B_CARD: "card", B_CRYPTO: "crypto",
    B_CHANGE: "change", B_PROFILE: "profile", B_RULES: "rules", B_PRIVACY: "privacy", B_FAQ: "faq",
    B_ADMIN: "admin", B_A_PENDING: "a_pending", B_A_ORDERS: "a_orders", B_A_STATS: "a_stats",
    B_A_CARD: "a_card", B_A_CRYPTO: "a_crypto",
}
SVC_BTN = {f"{svc['emoji']} {svc['title']}": skey for skey, svc in CATALOG.items()}

STATUS_TEXT = {
    "new": "⏳ Awaiting payment",
    "claimed": "🕐 Payment under review",
    "wait_email": "✉️ Waiting for your email",
    "wait_password": "🔑 Waiting for your password",
    "submitted": "🛠 In progress",
    "done": "✅ Completed",
    "cancelled": "🚫 Cancelled",
}

# per-user navigation state (in memory; after a restart the user simply lands on the main menu)
ctx = {}


def kb(*rows):
    m = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for row in rows:
        m.row(*[types.KeyboardButton(t) for t in row])
    return m


def ikb(*buttons):
    m = types.InlineKeyboardMarkup()
    m.row(*[types.InlineKeyboardButton(text=t, callback_data=d) for t, d in buttons])
    return m


def main_kb(uid):
    rows = [[B_CATALOG, B_ORDERS], [B_PAYMENT, B_SUPPORT], [B_SETTINGS, B_ABOUT]]
    if uid in ADMIN_IDS:
        rows.append([B_ADMIN])
    return kb(*rows)


# ---------------------------------------------------------------- database
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
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(orders)")}
    for col in ("method", "proof_file_id", "proof_type", "claimed_at", "confirmed_at", "done_at"):
        if col not in existing:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {col} TEXT")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            created_at TEXT
        )
    """)
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    conn.close()


def ensure_user(user):
    conn = db()
    row = conn.execute("SELECT 1 FROM users WHERE user_id = ?", (user.id,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO users(user_id, username, first_name, created_at) VALUES(?,?,?,?)",
            (user.id, user.username or "", user.first_name or "", now_iso()),
        )
    else:
        conn.execute(
            "UPDATE users SET username = ?, first_name = ? WHERE user_id = ?",
            (user.username or "", user.first_name or "", user.id),
        )
    conn.commit()
    conn.close()


def get_user_row(uid):
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE user_id = ?", (uid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_setting(key, default=""):
    conn = db()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row and row["value"] else default


def set_setting(key, value):
    conn = db()
    conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?,?)", (key, value))
    conn.commit()
    conn.close()


def create_order(user, pid, cents):
    conn = db()
    # an unpaid order is replaced when the buyer starts a new one
    conn.execute("UPDATE orders SET status = 'cancelled' WHERE user_id = ? AND status = 'new'", (user.id,))
    cur = conn.execute(
        "INSERT INTO orders(user_id, username, plan_id, price_cents, status, created_at) VALUES(?,?,?,?,?,?)",
        (user.id, user.username or "", pid, cents, "new", now_iso()),
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
    sets = ", ".join(f"{k} = ?" for k in keys)
    conn = db()
    conn.execute(f"UPDATE orders SET {sets} WHERE id = ?", [fields[k] for k in keys] + [oid])
    conn.commit()
    conn.close()


def transition(oid, from_statuses, to_status, **extra):
    """Atomic status change. Returns True only for the caller that actually changed it."""
    marks = ",".join("?" * len(from_statuses))
    sets = ", ".join(["status = ?"] + [f"{k} = ?" for k in extra])
    conn = db()
    cur = conn.execute(
        f"UPDATE orders SET {sets} WHERE id = ? AND status IN ({marks})",
        [to_status, *extra.values(), oid, *from_statuses],
    )
    ok = cur.rowcount > 0
    conn.commit()
    conn.close()
    return ok


def pick_order(uid, statuses, newest=True):
    marks = ",".join("?" * len(statuses))
    direction = "DESC" if newest else "ASC"
    conn = db()
    row = conn.execute(
        f"SELECT * FROM orders WHERE user_id = ? AND status IN ({marks}) ORDER BY id {direction} LIMIT 1",
        [uid, *statuses],
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def user_orders(uid, limit=10):
    conn = db()
    rows = conn.execute(
        "SELECT * FROM orders WHERE user_id = ? AND status NOT IN ('new','cancelled') ORDER BY id DESC LIMIT ?",
        (uid, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def orders_by_status(statuses, limit=20):
    marks = ",".join("?" * len(statuses))
    conn = db()
    rows = conn.execute(
        f"SELECT * FROM orders WHERE status IN ({marks}) ORDER BY id ASC LIMIT ?", [*statuses, limit]
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def all_orders(limit=20):
    conn = db()
    rows = conn.execute("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def order_plan(order):
    return PLANS.get(order["plan_id"])


def order_name(order):
    p = order_plan(order)
    return plan_full(p) if p else f"Plan {order['plan_id']}"


def method_name(method):
    return {"card": "Bank card (Visa)", "crypto": "Crypto"}.get(method or "", "-")


def who_html(uid, username=None, full_name=None):
    label = h(full_name) if full_name else str(uid)
    s = f'<a href="tg://user?id={uid}">{label}</a>'
    if username:
        s += f" (@{h(username)})"
    return s + f" · ID <code>{uid}</code>"


# ---------------------------------------------------------------- texts
def welcome_text():
    return (
        f"👋 <b>Welcome to {h(SHOP_NAME)}!</b>\n\n"
        "Spotify plans and PlayStation Store top-ups.\n\n"
        "Choose a section from the menu below 👇\n\n"
        f'Need help? Contact <a href="{SUPPORT_URL}">support</a>.'
    )


MENU_TEXT = "🏠 <b>Main Menu</b>\n\nChoose a section 👇"

PAYMENT_INFO = (
    "💳 <b>How payment works</b>\n\n"
    "1️⃣ Pick a plan in the catalog and press «✅ Place Order».\n"
    "2️⃣ Choose a payment method: <b>bank card transfer (Visa)</b> or <b>crypto</b>.\n"
    "3️⃣ Pay using the details shown and send a <b>screenshot</b> of the payment here.\n"
    "4️⃣ Press «✅ I've Paid» - it works only after the screenshot is received.\n"
    "5️⃣ The seller checks the payment and confirms it.\n"
    "6️⃣ Spotify: send your account email and password. PlayStation: just wait for your order."
)


def about_text():
    return (
        "ℹ️ <b>About</b>\n\n"
        f"{h(SHOP_NAME)} sells Spotify plans (Individual and Platinum) and PlayStation Store top-ups.\n\n"
        "• Pay by Visa card transfer or crypto\n"
        "• Every payment is verified manually by the seller\n"
        "• Track your orders in «📦 My Orders»\n\n"
        f'Questions? Contact <a href="{SUPPORT_URL}">support</a>.'
    )


def support_text():
    return (
        "💬 <b>Support</b>\n\n"
        f'Write to us: <a href="{SUPPORT_URL}">open the support chat</a>.\n'
        "Please include your order number if your question is about an order."
    )


FAQ_TEXT = (
    "❓ <b>FAQ</b>\n\n"
    "<b>Which payment methods do you accept?</b>\nBank card transfer (Visa) and crypto.\n\n"
    "<b>Why do I need to send a screenshot?</b>\nThe seller verifies every payment manually, so the order is submitted only together with proof of payment.\n\n"
    "<b>What do you need for Spotify?</b>\nAfter your payment is confirmed, the email and password of the Spotify account the plan should be activated on.\n\n"
    "<b>What do I do after paying for a PlayStation top-up?</b>\nNothing - your order goes to the seller, and you will be notified here when it is completed.\n\n"
    "<b>How long does it take?</b>\nOrders are processed manually by the seller. You will get a message here as soon as yours is completed.\n\n"
    f'<b>Something went wrong?</b>\nContact <a href="{SUPPORT_URL}">support</a> and mention your order number.'
)

RULES_TEXT = (
    "📜 <b>Rules</b>\n\n"
    "• An order is processed only after the seller has confirmed your payment.\n"
    "• Send the exact amount and a clear screenshot of the payment.\n"
    "• For Spotify we need your account email and password to activate the plan. "
    "After activation we recommend changing your password.\n"
    f'• Questions about an order - contact <a href="{SUPPORT_URL}">support</a> and give the order number.'
)

PRIVACY_TEXT = (
    "🔒 <b>Privacy</b>\n\n"
    "• The bot stores your Telegram ID and username, your orders and the email you send for Spotify.\n"
    "• Payment screenshots and account details are forwarded to the seller to complete your order.\n"
    "• Your password is not saved in the bot's database. The message with it is deleted from the chat "
    "and sent to the seller once.\n"
    "• After your plan is activated we recommend changing your password."
)

WAIT_NOTE = "Your order has been sent to the seller. Please wait - you will get a message here when it is completed. ⏳"


# ---------------------------------------------------------------- helpers
async def send(uid, text, markup=None):
    return await bot.send_message(uid, text, reply_markup=markup, disable_web_page_preview=True)


async def to_admins(text, markup=None, ids=None):
    sent = 0
    for aid in (ids if ids is not None else ADMIN_IDS):
        try:
            await bot.send_message(aid, text, reply_markup=markup, disable_web_page_preview=True)
            sent += 1
        except Exception as e:
            # an admin must press /start in the bot once, otherwise Telegram won't let the bot write first
            print(f"Could not message admin {aid}: {e}")
    return sent


# ---------------------------------------------------------------- screens
async def remind_awaiting(uid):
    order = pick_order(uid, ("wait_email", "wait_password"), newest=False)
    if order:
        what = "email" if order["status"] == "wait_email" else "password"
        await send(uid, f"📌 Order <b>#{order['id']}</b> is waiting for your Spotify account <b>{what}</b> - just send it as a message.")


async def screen_main(uid, text=None):
    ctx[uid] = {"screen": "main"}
    await send(uid, text or MENU_TEXT, main_kb(uid))
    await remind_awaiting(uid)


async def screen_catalog(uid):
    ctx[uid] = {"screen": "catalog"}
    rows = [[t] for t in SVC_BTN]
    rows.append([B_MENU])
    await send(uid, "🛍 <b>Catalog</b>\n\nChoose a service 👇", kb(*rows))


async def screen_groups(uid, skey):
    svc = CATALOG[skey]
    ctx[uid] = {"screen": "groups", "svc": skey}
    rows = [[f"{g['emoji']} {g['title']}"] for g in svc["groups"]]
    rows.append([B_BACK, B_MENU])
    await send(uid, f"{svc['emoji']} <b>{h(svc['title'])}</b>\n\nChoose your plan type 👇", kb(*rows))


async def screen_plans(uid, skey, gi):
    svc = CATALOG[skey]
    grp = svc["groups"][gi]
    ctx[uid] = {"screen": "plans", "svc": skey, "gi": gi}
    title = f"{svc['emoji']} <b>{h(svc['title'])}</b>"
    if grp["title"]:
        title += f" - <b>{h(grp['title'])}</b>"
    if svc["note"]:
        title += f"\n<i>{h(svc['note'])}</i>"
    rows = [[plan_btn_text(PLANS[pid])] for pid, _n, _c, _m in grp["plans"]]
    rows.append([B_BACK, B_MENU])
    await send(uid, title + "\n\nChoose a plan 👇", kb(*rows))


async def screen_service(uid, skey):
    if len(CATALOG[skey]["groups"]) > 1:
        await screen_groups(uid, skey)
    else:
        await screen_plans(uid, skey, 0)


async def screen_plan(uid, pid):
    p = PLANS[pid]
    ctx[uid] = {"screen": "plan", "svc": p["svc"], "gi": p["gi"], "pid": pid}
    lines = [f"🧾 <b>{h(plan_title(p))}</b>", ""]
    if p["months"]:
        lines.append(f"Duration: <b>{months_text(p['months'])}</b>")
    else:
        lines.append(f"Top-up: <b>{h(p['name'])}</b>")
    lines.append(f"Price: <b>{money(p['cents'])}</b>")
    lines.append("")
    if needs_credentials(p):
        lines.append("After your payment is confirmed we will ask for your Spotify account email and password to activate the plan.")
    else:
        lines.append("After your payment is confirmed your order goes to the seller - nothing else is needed from you.")
    lines.append("")
    lines.append("Press «✅ Place Order» to continue.")
    await send(uid, "\n".join(lines), kb([B_ORDER], [B_BACK, B_MENU]))


async def screen_methods(uid, order):
    ctx[uid] = {"screen": "methods", "oid": order["id"]}
    update_order(order["id"], method=None, proof_file_id=None, proof_type=None)
    text = (
        f"🧾 <b>Order #{order['id']}</b>\n"
        f"{h(order_name(order))}\n"
        f"Total: <b>{money(order['price_cents'])}</b>\n\n"
        "Choose a payment method 👇\n\n"
        "💳 Bank card transfer (Visa)\n🪙 Crypto"
    )
    await send(uid, text, kb([B_CARD, B_CRYPTO], [B_CANCEL], [B_MENU]))


async def screen_pay(uid, order, method):
    update_order(order["id"], method=method)
    ctx[uid] = {"screen": "pay", "oid": order["id"]}
    if method == "card":
        title = "💳 <b>Pay by bank card (Visa)</b>"
        details = h(get_setting("card_details", DEFAULT_CARD))
        how = f"Transfer <b>{money(order['price_cents'])}</b> to the card above and keep the receipt."
    else:
        title = "🪙 <b>Pay with crypto</b>"
        details = h(get_setting("crypto_details", DEFAULT_CRYPTO))
        how = (
            f"Send the equivalent of <b>{money(order['price_cents'])}</b> to the address above "
            "using the network shown. Network fees are paid by you."
        )
    text = (
        f"{title}\n\n"
        f"Order <b>#{order['id']}</b> - {h(order_name(order))}\n"
        f"Amount: <b>{money(order['price_cents'])}</b>\n\n"
        f"{details}\n\n"
        f"1️⃣ {how}\n"
        "2️⃣ Send a <b>screenshot</b> of the payment here in the chat 📎\n"
        "3️⃣ Press «✅ I've Paid» - the button works only after the screenshot is received."
    )
    await send(uid, text, kb([B_PAID], [B_CHANGE, B_CANCEL], [B_MENU]))


async def screen_settings(uid, text=None):
    ctx[uid] = {"screen": "settings"}
    await send(uid, text or "⚙️ <b>Settings</b>\n\nYour profile, rules, privacy and answers to common questions 👇",
               kb([B_PROFILE, B_RULES], [B_PRIVACY, B_FAQ], [B_BACK, B_MENU]))


async def screen_admin(uid, text=None):
    ctx[uid] = {"screen": "admin"}
    await send(uid, text or "🛠 <b>Admin Panel</b>",
               kb([B_A_PENDING, B_A_ORDERS], [B_A_STATS], [B_A_CARD, B_A_CRYPTO], [B_BACK, B_MENU]))


async def show_orders(uid):
    rows = user_orders(uid)
    if not rows:
        text = "📦 <b>My Orders</b>\n\nNo orders yet. Take a look at the catalog 🛍"
    else:
        lines = ["📦 <b>My Orders</b>\n"]
        for r in rows:
            lines.append(f"#{r['id']} · {h(order_name(r))} · {money(r['price_cents'])}\n   {STATUS_TEXT.get(r['status'], r['status'])}")
        text = "\n".join(lines)
    await send(uid, text, main_kb(uid))
    ctx[uid] = {"screen": "main"}
    await remind_awaiting(uid)


async def show_profile(uid):
    row = get_user_row(uid) or {}
    conn = db()
    n = conn.execute("SELECT COUNT(*) AS n FROM orders WHERE user_id = ? AND status NOT IN ('new','cancelled')", (uid,)).fetchone()["n"]
    conn.close()
    username = f"@{h(row['username'])}" if row.get("username") else "-"
    since = (row.get("created_at") or "")[:10] or "-"
    text = (
        "👤 <b>Profile</b>\n\n"
        f"ID: <code>{uid}</code>\n"
        f"Username: {username}\n"
        f"Orders: <b>{n}</b>\n"
        f"Member since: {since}"
    )
    await screen_settings(uid, text)


# ---------------------------------------------------------------- order flow
def admin_caption(order, extra=""):
    p = order_plan(order)
    todo = todo_text(p) if p else "Check the plan manually."
    data_line = (
        "🔐 Account data: will be requested after you confirm the payment."
        if p and needs_credentials(p) else "🔐 Account data: not needed."
    )
    return (
        f"🆕 <b>New order #{order['id']}</b>\n\n"
        f"🎯 <b>To do:</b> {h(todo)}\n"
        f"📦 Plan: {h(order_name(order))}\n"
        f"💰 Amount paid: <b>{money(order['price_cents'])}</b> via {method_name(order.get('method'))}\n"
        f"👤 Buyer: {who_html(order['user_id'], order.get('username'))}\n"
        f"{data_line}\n\n"
        "📸 Check the screenshot and the amount, then confirm or reject." + extra
    )


async def send_proof(aid, order, caption, markup):
    fid, ftype = order.get("proof_file_id"), order.get("proof_type")
    if fid and ftype == "document":
        return await bot.send_document(aid, fid, caption=caption, reply_markup=markup)
    if fid:
        return await bot.send_photo(aid, fid, caption=caption, reply_markup=markup)
    return await bot.send_message(aid, caption, reply_markup=markup)


async def send_claim(order, ids=None):
    markup = ikb(("✅ Confirm payment", f"ap:{order['id']}"), ("❌ Reject", f"rj:{order['id']}"))
    caption = admin_caption(order)
    sent = 0
    for aid in (ids if ids is not None else ADMIN_IDS):
        try:
            await send_proof(aid, order, caption, markup)
            sent += 1
        except Exception as e:
            print(f"Could not send order {order['id']} to admin {aid}: {e}")
    return sent


async def handle_paid(user):
    uid = user.id
    order = pick_order(uid, ("new",))
    if not order:
        if pick_order(uid, ("claimed",)):
            await send(uid, "Your payment is already being checked by the seller. Please wait for the confirmation 🙏")
        else:
            await send(uid, "You have no order waiting for payment. Choose a plan in the catalog 🛍", main_kb(uid))
        return
    if not order.get("method"):
        await screen_methods(uid, order)
        return
    if not order.get("proof_file_id"):
        await send(
            uid,
            "⚠️ <b>Screenshot required.</b>\n\nSend a screenshot of your payment here in the chat first - "
            "«✅ I've Paid» works only after we receive it. 📎",
        )
        return
    if not transition(order["id"], ("new",), "claimed", claimed_at=now_iso()):
        return
    order = get_order(order["id"])
    if await send_claim(order) == 0:
        transition(order["id"], ("claimed",), "new")
        await send(uid, f'⚠️ We could not reach the seller. Please try again in a minute or contact <a href="{SUPPORT_URL}">support</a>.')
        return
    ctx[uid] = {"screen": "main"}
    await send(
        uid,
        f"🕐 <b>Payment submitted</b> (order #{order['id']}).\n\nThe seller will check it and confirm. "
        "You will get a message here as soon as it is confirmed.",
        main_kb(uid),
    )


async def handle_cancel(uid):
    order = pick_order(uid, ("new",))
    if order:
        transition(order["id"], ("new",), "cancelled")
        await screen_main(uid, f"🚫 Order #{order['id']} was cancelled.\n\n" + MENU_TEXT)
        return
    claimed = pick_order(uid, ("claimed",))
    if claimed:
        await send(uid, f'Order #{claimed["id"]} is already with the seller and can no longer be cancelled here. Please contact <a href="{SUPPORT_URL}">support</a>.')
        return
    await screen_main(uid)


# ---------------------------------------------------------------- handlers
@dp.message_handler(commands=["start", "menu", "help"])
async def cmd_start(message: types.Message):
    ensure_user(message.from_user)
    await screen_main(message.from_user.id, welcome_text() if message.get_command() == "/start" else None)


@dp.message_handler(commands=["backup"])
async def cmd_backup(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    try:
        await message.answer_document(types.InputFile(DB_PATH), caption="Database backup")
    except Exception as e:
        await message.answer(f"Backup failed: {h(str(e))}")


@dp.message_handler(content_types=[types.ContentType.PHOTO, types.ContentType.DOCUMENT])
async def on_proof(message: types.Message):
    user = message.from_user
    uid = user.id
    ensure_user(user)
    if message.photo:
        file_id, ftype = message.photo[-1].file_id, "photo"
    else:
        mime = (message.document.mime_type or "")
        if not (mime.startswith("image/") or mime == "application/pdf"):
            await send(uid, "Please send the payment proof as a screenshot (image) or a PDF.")
            return
        file_id, ftype = message.document.file_id, "document"

    order = pick_order(uid, ("new", "claimed"))
    if not order:
        await send(uid, "There is no order waiting for payment right now. Choose a plan in the catalog first 🛍", main_kb(uid))
        return

    if order["status"] == "claimed":
        # extra proof for an order that is already under review -> forward to admins
        fake = dict(order, proof_file_id=file_id, proof_type=ftype)
        cap = f"📎 <b>Additional proof for order #{order['id']}</b>\nBuyer: {who_html(uid, user.username)}"
        for aid in ADMIN_IDS:
            try:
                await send_proof(aid, fake, cap, None)
            except Exception as e:
                print(f"Could not forward proof to admin {aid}: {e}")
        await send(uid, f"📎 Extra screenshot forwarded to the seller (order #{order['id']}).")
        return

    if not order.get("method"):
        await send(uid, "Please choose a payment method first 👇")
        await screen_methods(uid, order)
        return

    update_order(order["id"], proof_file_id=file_id, proof_type=ftype)
    ctx[uid] = {"screen": "pay", "oid": order["id"]}
    await send(
        uid,
        f"✅ <b>Screenshot received</b> for order #{order['id']}.\n\nNow press «✅ I've Paid» to send it to the seller.",
        kb([B_PAID], [B_CHANGE, B_CANCEL], [B_MENU]),
    )


async def handle_button(user, key, c):
    uid = user.id
    screen = c.get("screen")

    if key == "menu":
        await screen_main(uid)
    elif key == "back":
        if screen == "plan":
            await screen_plans(uid, c["svc"], c["gi"])
        elif screen == "plans":
            if len(CATALOG[c["svc"]]["groups"]) > 1:
                await screen_groups(uid, c["svc"])
            else:
                await screen_catalog(uid)
        elif screen == "groups":
            await screen_catalog(uid)
        elif screen == "settings":
            await screen_main(uid)
        elif screen in ("set_card", "set_crypto"):
            await screen_admin(uid)
        else:
            await screen_main(uid)
    elif key == "catalog":
        await screen_catalog(uid)
    elif key == "orders":
        await show_orders(uid)
    elif key == "payment":
        await send(uid, PAYMENT_INFO, main_kb(uid))
        ctx[uid] = {"screen": "main"}
    elif key == "support":
        await send(uid, support_text(), main_kb(uid))
        ctx[uid] = {"screen": "main"}
    elif key == "about":
        await send(uid, about_text(), main_kb(uid))
        ctx[uid] = {"screen": "main"}
    elif key == "settings":
        await screen_settings(uid)
    elif key == "profile":
        await show_profile(uid)
    elif key == "rules":
        await screen_settings(uid, RULES_TEXT)
    elif key == "privacy":
        await screen_settings(uid, PRIVACY_TEXT)
    elif key == "faq":
        await screen_settings(uid, FAQ_TEXT)
    elif key == "order":
        p = PLANS.get(c.get("pid"))
        if screen != "plan" or not p:
            await screen_catalog(uid)
            return
        oid = create_order(user, c["pid"], p["cents"])
        await screen_methods(uid, get_order(oid))
    elif key in ("card", "crypto"):
        order = pick_order(uid, ("new",))
        if not order:
            await screen_catalog(uid)
            return
        await screen_pay(uid, order, key)
    elif key == "change":
        order = pick_order(uid, ("new",))
        if not order:
            await screen_catalog(uid)
            return
        await screen_methods(uid, order)
    elif key == "paid":
        await handle_paid(user)
    elif key == "cancel":
        await handle_cancel(uid)
    elif key.startswith("a_") or key == "admin":
        if uid not in ADMIN_IDS:
            await screen_main(uid)
            return
        await handle_admin_button(uid, key)


async def handle_admin_button(uid, key):
    if key == "admin":
        await screen_admin(uid)
    elif key == "a_pending":
        rows = orders_by_status(("claimed",))
        if not rows:
            await send(uid, "✅ No payments are waiting for review.")
            return
        for o in rows:
            await send_claim(o, ids=[uid])
    elif key == "a_orders":
        rows = all_orders()
        if not rows:
            await send(uid, "No orders yet.")
            return
        lines = ["📋 <b>Latest orders</b>\n"]
        for r in rows:
            who = f"@{h(r['username'])}" if r.get("username") else f"ID {r['user_id']}"
            lines.append(f"#{r['id']} · {h(order_name(r))} · {money(r['price_cents'])} · {who}\n   {STATUS_TEXT.get(r['status'], r['status'])}")
        await send(uid, "\n".join(lines)[:4000])
    elif key == "a_stats":
        conn = db()
        counts = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM orders GROUP BY status")}
        rev = conn.execute(
            "SELECT COALESCE(SUM(price_cents),0) AS s FROM orders WHERE status IN ('wait_email','wait_password','submitted','done')"
        ).fetchone()["s"]
        conn.close()
        lines = ["📊 <b>Stats</b>\n"]
        for st, label in STATUS_TEXT.items():
            lines.append(f"{label}: <b>{counts.get(st, 0)}</b>")
        lines.append(f"\n💰 Confirmed payments: <b>{money(rev)}</b>")
        await send(uid, "\n".join(lines))
    elif key in ("a_card", "a_crypto"):
        kind = "card" if key == "a_card" else "crypto"
        current = get_setting(f"{kind}_details", DEFAULT_CARD if kind == "card" else DEFAULT_CRYPTO)
        ctx[uid] = {"screen": f"set_{kind}"}
        await send(
            uid,
            f"{'💳' if kind == 'card' else '🪙'} <b>Current {kind} details:</b>\n\n{h(current)}\n\n"
            "Send the new details as one message. Send <code>-</code> to restore the default. "
            "Press «⬅️ Back» to keep them as they are.",
            kb([B_BACK, B_MENU]),
        )


@dp.message_handler(lambda m: m.text and not m.text.startswith("/"))
async def on_text(message: types.Message):
    user = message.from_user
    uid = user.id
    ensure_user(user)
    text = message.text.strip()
    c = ctx.get(uid) or {"screen": "main"}

    key = BTN.get(text)
    if key:
        await handle_button(user, key, c)
        return

    if text in SVC_BTN:
        await screen_service(uid, SVC_BTN[text])
        return

    screen = c.get("screen")
    if screen == "groups":
        for gi, g in enumerate(CATALOG[c["svc"]]["groups"]):
            if text == f"{g['emoji']} {g['title']}":
                await screen_plans(uid, c["svc"], gi)
                return
    if screen == "plans":
        grp = CATALOG[c["svc"]]["groups"][c["gi"]]
        for pid, _n, _c, _m in grp["plans"]:
            if text == plan_btn_text(PLANS[pid]):
                await screen_plan(uid, pid)
                return

    if screen in ("set_card", "set_crypto") and uid in ADMIN_IDS:
        kind = "card" if screen == "set_card" else "crypto"
        if text == "-":
            set_setting(f"{kind}_details", "")
            await screen_admin(uid, f"✅ {kind.capitalize()} details restored to the default.")
        else:
            set_setting(f"{kind}_details", text)
            await screen_admin(uid, f"✅ {kind.capitalize()} details updated.")
        return

    # Spotify account data
    order = pick_order(uid, ("wait_email", "wait_password"), newest=False)
    if order:
        if order["status"] == "wait_email":
            if not EMAIL_RE.match(text):
                await send(uid, "That doesn't look like an email address. Please send it like <code>name@mail.com</code>")
                return
            update_order(order["id"], email=text, status="wait_password")
            await send(
                uid,
                "Thanks! Now send the <b>password</b> of the account 🔑\n\n"
                "🔒 The bot doesn't save your password and your message with it is deleted from the chat - "
                "it is sent to the seller only. We recommend changing it after activation.",
                kb([B_MENU]),
            )
            return
        # wait_password
        try:
            await message.delete()
        except Exception:
            pass
        update_order(order["id"], status="submitted")
        order = get_order(order["id"])
        p = order_plan(order)
        caption = (
            f"🔐 <b>Account data - order #{order['id']}</b>\n\n"
            f"🎯 <b>To do:</b> {h(todo_text(p) if p else 'Check the plan manually.')}\n"
            f"👤 Buyer: {who_html(uid, user.username, user.full_name)}\n"
            f"📧 Email: <code>{h(order['email'] or '')}</code>\n"
            f"🔑 Password: <code>{h(text)}</code>"
        )
        sent = await to_admins(caption, ikb(("✅ Mark completed", f"dn:{order['id']}")))
        if sent:
            await send(
                uid,
                f"🎉 <b>Order #{order['id']} accepted!</b>\n\n{WAIT_NOTE}",
                main_kb(uid),
            )
        else:
            await send(uid, f'⚠️ Order #{order["id"]} is paid, but we could not reach the seller. Please contact <a href="{SUPPORT_URL}">support</a> and mention the order number.')
        ctx[uid] = {"screen": "main"}
        return

    await send(uid, "Please use the menu buttons below 👇", main_kb(uid))
    ctx[uid] = {"screen": "main"}


# ---------------------------------------------------------------- admin callbacks
async def edit_admin_msg(call, suffix, markup=None):
    m = call.message
    try:
        body = m.html_text or ""
        if m.photo or m.document or m.caption is not None:
            await m.edit_caption(caption=body + suffix, reply_markup=markup)
        else:
            await m.edit_text(body + suffix, reply_markup=markup)
    except Exception as e:
        print("Could not edit admin message:", e)


@dp.callback_query_handler(lambda c: c.data and c.data[:3] in ("ap:", "rj:", "dn:"))
async def cb_admin(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("Admins only", show_alert=True)
        return
    action, _, raw = call.data.partition(":")
    try:
        oid = int(raw)
    except ValueError:
        await call.answer()
        return
    order = get_order(oid)
    if not order:
        await call.answer("Order not found", show_alert=True)
        return
    buyer = order["user_id"]
    p = order_plan(order)
    admin_name = h(call.from_user.full_name)

    if action == "ap":
        creds = bool(p and needs_credentials(p))
        new_status = "wait_email" if creds else "submitted"
        if not transition(oid, ("claimed",), new_status, confirmed_at=now_iso(), paid_at=now_iso()):
            await call.answer("Already processed", show_alert=True)
            return
        await call.answer("Payment confirmed ✅")
        if creds:
            await edit_admin_msg(call, f"\n\n✅ <b>Payment confirmed</b> by {admin_name}. Waiting for the buyer's Spotify account data.")
            ctx[buyer] = {"screen": "await"}
            await send(
                buyer,
                f"✅ <b>Payment confirmed!</b> (order #{oid})\n\n"
                f"To activate <b>{h(plan_title(p))}</b>, send the <b>email</b> of your Spotify account as a message 👇",
                kb([B_MENU]),
            )
        else:
            await edit_admin_msg(call, f"\n\n✅ <b>Payment confirmed</b> by {admin_name}. Deliver the order, then mark it completed.",
                                 ikb(("✅ Mark completed", f"dn:{oid}")))
            ctx[buyer] = {"screen": "main"}
            await send(buyer, f"✅ <b>Payment confirmed!</b> (order #{oid})\n\n{WAIT_NOTE}", main_kb(buyer))

    elif action == "rj":
        if not transition(oid, ("claimed",), "new", proof_file_id=None, proof_type=None):
            await call.answer("Already processed", show_alert=True)
            return
        await call.answer("Rejected")
        await edit_admin_msg(call, f"\n\n❌ <b>Rejected</b> by {admin_name}.")
        ctx[buyer] = {"screen": "pay", "oid": oid}
        await send(
            buyer,
            f"❌ <b>We could not find your payment for order #{oid}.</b>\n\n"
            "Please check the transfer and send a new, clear screenshot, then press «✅ I've Paid» again. "
            f'Or contact <a href="{SUPPORT_URL}">support</a>.',
            kb([B_PAID], [B_CHANGE, B_CANCEL], [B_MENU]),
        )

    elif action == "dn":
        if not transition(oid, ("submitted",), "done", done_at=now_iso()):
            await call.answer("Already completed or not ready", show_alert=True)
            return
        await call.answer("Marked completed ✅")
        await edit_admin_msg(call, f"\n\n✅ <b>COMPLETED</b> by {admin_name}.")
        name = h(order_name(order))
        try:
            await send(
                buyer,
                f"🎉 <b>Order #{oid} is completed!</b>\n\n{name}\n\n"
                f'Enjoy! If anything looks wrong, contact <a href="{SUPPORT_URL}">support</a> with your order number.',
                main_kb(buyer),
            )
        except Exception as e:
            print("Could not notify the buyer:", e)


# ---------------------------------------------------------------- web (health check for the host)
app = Flask(__name__)


@app.route("/")
def index():
    return "ok", 200


def run_flask():
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))


# ---------------------------------------------------------------- start
if __name__ == "__main__":
    init_db()
    threading.Thread(target=run_flask, daemon=True).start()
    executor.start_polling(dp, skip_updates=True)
