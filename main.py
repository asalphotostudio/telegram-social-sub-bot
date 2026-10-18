# main.py
# Магазин подписок в Telegram (aiogram 2.x) + оплата через Stripe Checkout.
#
# Интерфейс: постоянное нижнее меню (кнопки под полем ввода), 3 языка (RU / EN / UZ),
# настройки пользователя, каталог с inline-кнопками, оформление заказа и уведомление продавцу.
#
# Как это работает:
#   1. Покупатель выбирает сервис и тариф, платит картой на странице Stripe.
#   2. Возвращается в бота, нажимает "Я оплатил" - бот проверяет оплату у Stripe.
#   3. Бот просит email и пароль от аккаунта для оформления подписки.
#   4. Админу (продавцу) приходит "Новый заказ!" с данными и сроком.
#   5. Продавец оформляет подписку и нажимает "Заказ выполнен" - покупателю уходит уведомление.
#
# Переменные окружения (Variables на хостинге):
#   BOT_TOKEN   - токен от @BotFather            (обязательно)
#   STRIPE_KEY  - секретный ключ Stripe sk_...   (обязательно)
# Необязательные:
#   ADMIN_IDS   - доп. админы через запятую
#   SUPPORT_URL - ссылка на поддержку (по умолчанию https://t.me/Sainnapari)
#   BASE_URL    - адрес сервиса для страниц success/cancel
#   DB_PATH     - путь к SQLite (на Railway: /data/subscriptions.db при подключённом Volume)

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
LINE = "━━━━━━━━━━━━━━"

# ---------- Языки ----------
LANGS = ["ru", "en", "uz"]
LANG_NAMES = {"ru": "🇷🇺 Русский", "en": "🇬🇧 English", "uz": "🇺🇿 Oʻzbekcha"}

PERIOD_LABELS = {
    "7d_t": {"ru": "7 дней (пробный)", "en": "7 days (trial)", "uz": "7 kun (sinov)"},
    "30d_t": {"ru": "30 дней (пробный)", "en": "30 days (trial)", "uz": "30 kun (sinov)"},
    "1d": {"ru": "1 день", "en": "1 day", "uz": "1 kun"},
    "1w": {"ru": "1 неделя", "en": "1 week", "uz": "1 hafta"},
    "1m": {"ru": "1 месяц", "en": "1 month", "uz": "1 oy"},
    "12m": {"ru": "12 месяцев", "en": "12 months", "uz": "12 oy"},
}

T = {
    "ru": {
        "m_catalog": "🛍 Каталог",
        "m_orders": "📦 Мои заказы",
        "m_how": "📖 Как заказать",
        "m_faq": "❓ FAQ",
        "m_settings": "⚙️ Настройки",
        "m_support": "💬 Поддержка",
        "placeholder": "Выберите раздел меню",
        "welcome": (
            "<b>Добро пожаловать!</b>\n" + LINE + "\n"
            "Подписки на <b>Adobe</b>, <b>Canva</b>, <b>Netflix</b> и <b>Crunchyroll</b>.\n\n"
            "▫️ Оплата картой на защищённой странице Stripe\n"
            "▫️ Оформление подписки на ваш аккаунт\n"
            "▫️ Статус заказа — в разделе «Мои заказы»\n\n"
            "Вопросы? <a href=\"{support}\">Служба поддержки</a>\n\n"
            "Выберите раздел в меню ниже 👇"
        ),
        "catalog": "🛍 <b>Каталог</b>\n" + LINE + "\nВыберите сервис:",
        "choose_level": "{emoji} <b>{title}</b>\n" + LINE + "\nВыберите уровень подписки:",
        "choose_plan": "\n" + LINE + "\nВыберите тариф:",
        "plan_card": (
            "🧾 <b>{title}</b>\n" + LINE + "\n"
            "▫️ Тариф: <b>{label}</b>\n"
            "▫️ Срок: <b>{period}</b>\n"
            "▫️ Цена: <b>{price}</b>\n" + LINE + "\n"
            "После оплаты бот запросит <b>email</b> и <b>пароль</b> от вашего аккаунта для оформления подписки."
        ),
        "pay_btn": "💳 Оплатить {price}",
        "paid_btn": "✅ Я оплатил",
        "back": "⬅️ Назад",
        "order_card": (
            "🧾 <b>Заказ #{oid}</b>\n" + LINE + "\n"
            "{title} — {label}\n"
            "К оплате: <b>{price}</b>\n\n"
            "<b>1.</b> Нажмите «Оплатить» и оплатите картой\n"
            "<b>2.</b> Вернитесь сюда и нажмите «Я оплатил»"
        ),
        "pay_error": "⚠️ Не удалось создать платёж. Попробуйте позже или обратитесь в <a href=\"{support}\">поддержку</a>.",
        "pay_not_yet": "Оплата пока не поступила. Оплатите по кнопке «Оплатить» и нажмите «Я оплатил» ещё раз.",
        "pay_check_fail": "Не удалось проверить оплату. Попробуйте через минуту.",
        "order_not_found": "Заказ не найден. Начните заново из каталога.",
        "already_in_work": "Этот заказ уже принят в работу ✅",
        "ask_email": (
            "✅ <b>Оплата получена, спасибо!</b>\n\n"
            "Для оформления подписки <b>{title}</b> отправьте <b>email</b> от вашего аккаунта одним сообщением 👇"
        ),
        "bad_email": "Это не похоже на email. Отправьте адрес в формате <code>name@mail.com</code>",
        "ask_password": (
            "Отлично! Теперь отправьте <b>пароль</b> от аккаунта 🔑\n\n"
            "🔒 Пароль не сохраняется в базе бота, а ваше сообщение с ним удаляется из чата. "
            "Данные передаются только продавцу для оформления заказа. "
            "После оформления рекомендуем сменить пароль."
        ),
        "order_accepted": (
            "🎉 <b>Заказ #{oid} принят!</b>\n" + LINE + "\n"
            "Продавец оформит подписку, и вы получите уведомление здесь. "
            "Статус заказа — в разделе «Мои заказы»."
        ),
        "admin_fail": (
            "⚠️ Заказ #{oid} оплачен, но не удалось уведомить продавца. "
            "Обратитесь в <a href=\"{support}\">поддержку</a> и назовите номер заказа."
        ),
        "order_done": (
            "🎉 <b>Заказ #{oid} выполнен!</b>\n" + LINE + "\n"
            "Подписка <b>{title}</b> оформлена. Приятного использования!\n"
            "Вопросы — <a href=\"{support}\">поддержка</a>."
        ),
        "orders_title": "📦 <b>Мои заказы</b>\n" + LINE,
        "no_orders": "Пока заказов нет. Откройте «🛍 Каталог», чтобы оформить первый.",
        "st_new": "⏳ ожидает оплаты",
        "st_wait_email": "✉️ ожидаем ваш email",
        "st_wait_password": "🔑 ожидаем пароль",
        "st_submitted": "🛠 в работе",
        "st_done": "✅ выполнен",
        "how": (
            "📖 <b>Как заказать</b>\n" + LINE + "\n"
            "<b>1.</b> Откройте «🛍 Каталог» и выберите сервис\n"
            "<b>2.</b> Выберите тариф и нажмите «Оплатить»\n"
            "<b>3.</b> Оплатите картой на странице Stripe\n"
            "<b>4.</b> Вернитесь в бота и нажмите «Я оплатил»\n"
            "<b>5.</b> Отправьте email и пароль от аккаунта\n"
            "<b>6.</b> Продавец оформит подписку, вы получите уведомление\n\n"
            "Следить за заказами можно в разделе «📦 Мои заказы»."
        ),
        "faq": (
            "❓ <b>Частые вопросы</b>\n" + LINE + "\n"
            "<b>Безопасна ли оплата?</b>\n"
            "Оплата проходит на странице Stripe. Бот не видит и не хранит данные вашей карты.\n\n"
            "<b>Зачем нужны email и пароль?</b>\n"
            "Для оформления подписки на ваш аккаунт. Пароль не сохраняется в базе бота и передаётся только продавцу. "
            "После оформления рекомендуем сменить пароль.\n\n"
            "<b>Как узнать статус заказа?</b>\n"
            "В разделе «📦 Мои заказы». Когда подписка будет оформлена, бот пришлёт уведомление.\n\n"
            "<b>Что делать, если возник вопрос или проблема?</b>\n"
            "Напишите в <a href=\"{support}\">поддержку</a> и укажите номер заказа. "
            "Условия возврата также уточняйте у поддержки."
        ),
        "settings": (
            "⚙️ <b>Настройки</b>\n" + LINE + "\n"
            "👤 ID: <code>{uid}</code>\n"
            "🌐 Язык: {lang}\n"
            "🔔 Уведомления о заказах: {notif}\n"
            "📦 Заказов: {n}"
        ),
        "s_lang": "🌐 Язык: {lang}",
        "s_notif": "🔔 Уведомления: {state}",
        "on": "вкл ✅",
        "off": "выкл ⛔",
        "choose_language": "🌐 <b>Выберите язык</b>\n" + LINE,
        "lang_changed": "✅ Язык изменён: {lang}",
        "support": (
            "💬 <b>Поддержка</b>\n" + LINE + "\n"
            "По вопросам оплаты и оформления заказа напишите нам. Если есть номер заказа — укажите его."
        ),
        "support_btn": "Написать в поддержку",
        "unknown": "Выберите раздел в меню ниже 👇",
        "notif_off_note": "Покупатель отключил уведомления, сообщение не отправлено.",
    },
    "en": {
        "m_catalog": "🛍 Catalog",
        "m_orders": "📦 My orders",
        "m_how": "📖 How to order",
        "m_faq": "❓ FAQ",
        "m_settings": "⚙️ Settings",
        "m_support": "💬 Support",
        "placeholder": "Choose a menu section",
        "welcome": (
            "<b>Welcome!</b>\n" + LINE + "\n"
            "Subscriptions for <b>Adobe</b>, <b>Canva</b>, <b>Netflix</b> and <b>Crunchyroll</b>.\n\n"
            "▫️ Card payment on a secure Stripe page\n"
            "▫️ Subscription activated on your account\n"
            "▫️ Order status in the “My orders” section\n\n"
            "Questions? <a href=\"{support}\">Support team</a>\n\n"
            "Choose a section in the menu below 👇"
        ),
        "catalog": "🛍 <b>Catalog</b>\n" + LINE + "\nChoose a service:",
        "choose_level": "{emoji} <b>{title}</b>\n" + LINE + "\nChoose a subscription level:",
        "choose_plan": "\n" + LINE + "\nChoose a plan:",
        "plan_card": (
            "🧾 <b>{title}</b>\n" + LINE + "\n"
            "▫️ Plan: <b>{label}</b>\n"
            "▫️ Duration: <b>{period}</b>\n"
            "▫️ Price: <b>{price}</b>\n" + LINE + "\n"
            "After payment the bot will ask for the <b>email</b> and <b>password</b> of your account to activate the subscription."
        ),
        "pay_btn": "💳 Pay {price}",
        "paid_btn": "✅ I have paid",
        "back": "⬅️ Back",
        "order_card": (
            "🧾 <b>Order #{oid}</b>\n" + LINE + "\n"
            "{title} — {label}\n"
            "Amount due: <b>{price}</b>\n\n"
            "<b>1.</b> Tap “Pay” and pay by card\n"
            "<b>2.</b> Come back here and tap “I have paid”"
        ),
        "pay_error": "⚠️ Could not create the payment. Please try again later or contact <a href=\"{support}\">support</a>.",
        "pay_not_yet": "Payment has not arrived yet. Pay using the “Pay” button and tap “I have paid” again.",
        "pay_check_fail": "Could not check the payment. Please try again in a minute.",
        "order_not_found": "Order not found. Please start again from the catalog.",
        "already_in_work": "This order is already being processed ✅",
        "ask_email": (
            "✅ <b>Payment received, thank you!</b>\n\n"
            "To activate <b>{title}</b>, send the <b>email</b> of your account in one message 👇"
        ),
        "bad_email": "That doesn't look like an email. Send it in the format <code>name@mail.com</code>",
        "ask_password": (
            "Great! Now send the account <b>password</b> 🔑\n\n"
            "🔒 The password is not stored in the bot's database and your message with it is deleted from the chat. "
            "The data goes only to the seller to process your order. "
            "We recommend changing the password afterwards."
        ),
        "order_accepted": (
            "🎉 <b>Order #{oid} accepted!</b>\n" + LINE + "\n"
            "The seller will activate your subscription and you will be notified here. "
            "Track the status in the “My orders” section."
        ),
        "admin_fail": (
            "⚠️ Order #{oid} is paid, but the seller could not be notified. "
            "Please contact <a href=\"{support}\">support</a> and give the order number."
        ),
        "order_done": (
            "🎉 <b>Order #{oid} completed!</b>\n" + LINE + "\n"
            "Your <b>{title}</b> subscription is activated. Enjoy!\n"
            "Questions — <a href=\"{support}\">support</a>."
        ),
        "orders_title": "📦 <b>My orders</b>\n" + LINE,
        "no_orders": "No orders yet. Open “🛍 Catalog” to place your first one.",
        "st_new": "⏳ awaiting payment",
        "st_wait_email": "✉️ waiting for your email",
        "st_wait_password": "🔑 waiting for your password",
        "st_submitted": "🛠 in progress",
        "st_done": "✅ completed",
        "how": (
            "📖 <b>How to order</b>\n" + LINE + "\n"
            "<b>1.</b> Open “🛍 Catalog” and choose a service\n"
            "<b>2.</b> Choose a plan and tap “Pay”\n"
            "<b>3.</b> Pay by card on the Stripe page\n"
            "<b>4.</b> Return to the bot and tap “I have paid”\n"
            "<b>5.</b> Send the account email and password\n"
            "<b>6.</b> The seller activates the subscription and you get a notification\n\n"
            "Track your orders in the “📦 My orders” section."
        ),
        "faq": (
            "❓ <b>FAQ</b>\n" + LINE + "\n"
            "<b>Is the payment secure?</b>\n"
            "Payment is made on a Stripe page. The bot never sees or stores your card details.\n\n"
            "<b>Why are email and password needed?</b>\n"
            "To activate the subscription on your account. The password is not stored in the bot's database and goes only to the seller. "
            "We recommend changing it afterwards.\n\n"
            "<b>How do I check my order status?</b>\n"
            "In the “📦 My orders” section. The bot will notify you when the subscription is activated.\n\n"
            "<b>What if I have a question or a problem?</b>\n"
            "Write to <a href=\"{support}\">support</a> and give your order number. "
            "Please also ask support about refund terms."
        ),
        "settings": (
            "⚙️ <b>Settings</b>\n" + LINE + "\n"
            "👤 ID: <code>{uid}</code>\n"
            "🌐 Language: {lang}\n"
            "🔔 Order notifications: {notif}\n"
            "📦 Orders: {n}"
        ),
        "s_lang": "🌐 Language: {lang}",
        "s_notif": "🔔 Notifications: {state}",
        "on": "on ✅",
        "off": "off ⛔",
        "choose_language": "🌐 <b>Choose your language</b>\n" + LINE,
        "lang_changed": "✅ Language changed: {lang}",
        "support": (
            "💬 <b>Support</b>\n" + LINE + "\n"
            "Write to us about payment or order questions. Include the order number if you have one."
        ),
        "support_btn": "Contact support",
        "unknown": "Choose a section in the menu below 👇",
        "notif_off_note": "The buyer turned notifications off, the message was not sent.",
    },
    "uz": {
        "m_catalog": "🛍 Katalog",
        "m_orders": "📦 Buyurtmalarim",
        "m_how": "📖 Qanday buyurtma",
        "m_faq": "❓ FAQ",
        "m_settings": "⚙️ Sozlamalar",
        "m_support": "💬 Yordam",
        "placeholder": "Menyudan bo‘limni tanlang",
        "welcome": (
            "<b>Xush kelibsiz!</b>\n" + LINE + "\n"
            "<b>Adobe</b>, <b>Canva</b>, <b>Netflix</b> va <b>Crunchyroll</b> obunalari.\n\n"
            "▫️ Stripe xavfsiz sahifasida karta orqali to‘lov\n"
            "▫️ Obuna sizning akkauntingizga rasmiylashtiriladi\n"
            "▫️ Buyurtma holati — «Buyurtmalarim» bo‘limida\n\n"
            "Savollar? <a href=\"{support}\">Yordam xizmati</a>\n\n"
            "Quyidagi menyudan bo‘limni tanlang 👇"
        ),
        "catalog": "🛍 <b>Katalog</b>\n" + LINE + "\nXizmatni tanlang:",
        "choose_level": "{emoji} <b>{title}</b>\n" + LINE + "\nObuna darajasini tanlang:",
        "choose_plan": "\n" + LINE + "\nTarifni tanlang:",
        "plan_card": (
            "🧾 <b>{title}</b>\n" + LINE + "\n"
            "▫️ Tarif: <b>{label}</b>\n"
            "▫️ Muddat: <b>{period}</b>\n"
            "▫️ Narx: <b>{price}</b>\n" + LINE + "\n"
            "To‘lovdan so‘ng bot obunani rasmiylashtirish uchun akkauntingizning <b>email</b> va <b>parolini</b> so‘raydi."
        ),
        "pay_btn": "💳 To‘lash {price}",
        "paid_btn": "✅ To‘lov qildim",
        "back": "⬅️ Orqaga",
        "order_card": (
            "🧾 <b>Buyurtma #{oid}</b>\n" + LINE + "\n"
            "{title} — {label}\n"
            "To‘lov summasi: <b>{price}</b>\n\n"
            "<b>1.</b> «To‘lash» tugmasini bosib, karta bilan to‘lang\n"
            "<b>2.</b> Shu yerga qaytib, «To‘lov qildim» tugmasini bosing"
        ),
        "pay_error": "⚠️ To‘lovni yaratib bo‘lmadi. Keyinroq urinib ko‘ring yoki <a href=\"{support}\">yordamga</a> murojaat qiling.",
        "pay_not_yet": "To‘lov hali kelmadi. «To‘lash» tugmasi orqali to‘lang va «To‘lov qildim» tugmasini yana bosing.",
        "pay_check_fail": "To‘lovni tekshirib bo‘lmadi. Bir daqiqadan so‘ng urinib ko‘ring.",
        "order_not_found": "Buyurtma topilmadi. Katalogdan qaytadan boshlang.",
        "already_in_work": "Bu buyurtma allaqachon ishga olingan ✅",
        "ask_email": (
            "✅ <b>To‘lov qabul qilindi, rahmat!</b>\n\n"
            "<b>{title}</b> obunasini rasmiylashtirish uchun akkauntingiz <b>emailini</b> bitta xabarda yuboring 👇"
        ),
        "bad_email": "Bu email'ga o‘xshamaydi. Manzilni <code>name@mail.com</code> formatida yuboring",
        "ask_password": (
            "Ajoyib! Endi akkaunt <b>parolini</b> yuboring 🔑\n\n"
            "🔒 Parol bot bazasida saqlanmaydi, u yozilgan xabar esa chatdan o‘chiriladi. "
            "Ma’lumotlar faqat buyurtmani rasmiylashtirish uchun sotuvchiga yuboriladi. "
            "Rasmiylashtirilgandan so‘ng parolni almashtirishni tavsiya qilamiz."
        ),
        "order_accepted": (
            "🎉 <b>Buyurtma #{oid} qabul qilindi!</b>\n" + LINE + "\n"
            "Sotuvchi obunani rasmiylashtiradi va siz shu yerda xabar olasiz. "
            "Holatni «Buyurtmalarim» bo‘limida kuzating."
        ),
        "admin_fail": (
            "⚠️ Buyurtma #{oid} to‘langan, lekin sotuvchiga xabar yuborib bo‘lmadi. "
            "<a href=\"{support}\">Yordamga</a> murojaat qilib, buyurtma raqamini ayting."
        ),
        "order_done": (
            "🎉 <b>Buyurtma #{oid} bajarildi!</b>\n" + LINE + "\n"
            "<b>{title}</b> obunasi rasmiylashtirildi. Foydalanishdan zavqlaning!\n"
            "Savollar — <a href=\"{support}\">yordam</a>."
        ),
        "orders_title": "📦 <b>Buyurtmalarim</b>\n" + LINE,
        "no_orders": "Hozircha buyurtmalar yo‘q. Birinchi buyurtma uchun «🛍 Katalog»ni oching.",
        "st_new": "⏳ to‘lov kutilmoqda",
        "st_wait_email": "✉️ emailingiz kutilmoqda",
        "st_wait_password": "🔑 parolingiz kutilmoqda",
        "st_submitted": "🛠 jarayonda",
        "st_done": "✅ bajarildi",
        "how": (
            "📖 <b>Qanday buyurtma qilinadi</b>\n" + LINE + "\n"
            "<b>1.</b> «🛍 Katalog»ni oching va xizmatni tanlang\n"
            "<b>2.</b> Tarifni tanlab, «To‘lash»ni bosing\n"
            "<b>3.</b> Stripe sahifasida karta bilan to‘lang\n"
            "<b>4.</b> Botga qaytib, «To‘lov qildim»ni bosing\n"
            "<b>5.</b> Akkaunt emaili va parolini yuboring\n"
            "<b>6.</b> Sotuvchi obunani rasmiylashtiradi, siz xabar olasiz\n\n"
            "Buyurtmalarni «📦 Buyurtmalarim» bo‘limida kuzatib boring."
        ),
        "faq": (
            "❓ <b>Ko‘p beriladigan savollar</b>\n" + LINE + "\n"
            "<b>To‘lov xavfsizmi?</b>\n"
            "To‘lov Stripe sahifasida amalga oshadi. Bot karta ma’lumotlaringizni ko‘rmaydi va saqlamaydi.\n\n"
            "<b>Email va parol nima uchun kerak?</b>\n"
            "Obunani akkauntingizga rasmiylashtirish uchun. Parol bot bazasida saqlanmaydi va faqat sotuvchiga yuboriladi. "
            "Rasmiylashtirilgandan so‘ng uni almashtirishni tavsiya qilamiz.\n\n"
            "<b>Buyurtma holatini qanday bilaman?</b>\n"
            "«📦 Buyurtmalarim» bo‘limida. Obuna rasmiylashtirilganda bot xabar yuboradi.\n\n"
            "<b>Savol yoki muammo bo‘lsa-chi?</b>\n"
            "<a href=\"{support}\">Yordamga</a> yozing va buyurtma raqamini ko‘rsating. "
            "Pulni qaytarish shartlarini ham yordamdan so‘rang."
        ),
        "settings": (
            "⚙️ <b>Sozlamalar</b>\n" + LINE + "\n"
            "👤 ID: <code>{uid}</code>\n"
            "🌐 Til: {lang}\n"
            "🔔 Buyurtma bildirishnomalari: {notif}\n"
            "📦 Buyurtmalar: {n}"
        ),
        "s_lang": "🌐 Til: {lang}",
        "s_notif": "🔔 Bildirishnomalar: {state}",
        "on": "yoq ✅",
        "off": "o‘chiq ⛔",
        "choose_language": "🌐 <b>Tilni tanlang</b>\n" + LINE,
        "lang_changed": "✅ Til o‘zgartirildi: {lang}",
        "support": (
            "💬 <b>Yordam</b>\n" + LINE + "\n"
            "To‘lov va buyurtma bo‘yicha savollar uchun bizga yozing. Buyurtma raqami bo‘lsa, ko‘rsating."
        ),
        "support_btn": "Yordamga yozish",
        "unknown": "Quyidagi menyudan bo‘limni tanlang 👇",
        "notif_off_note": "Xaridor bildirishnomalarni o‘chirgan, xabar yuborilmadi.",
    },
}

MENU_KEYS = ["m_catalog", "m_orders", "m_how", "m_faq", "m_settings", "m_support"]
MENU_ACTIONS = {}  # текст кнопки (на любом языке) -> ключ действия
for _lg in LANGS:
    for _k in MENU_KEYS:
        MENU_ACTIONS[T[_lg][_k]] = _k


def tr(lang, key, **kw):
    s = T.get(lang, T["ru"]).get(key) or T["ru"][key]
    return s.format(**kw) if kw else s


def detect_lang(code):
    code = (code or "").lower()
    if code.startswith("uz"):
        return "uz"
    if code.startswith(("ru", "be", "uk", "kk", "ky", "tg")):
        return "ru"
    return "en"


# ---------- Каталог ----------
# Тариф: (id, метка (код периода или название тарифа), цена в центах, код срока)
CATALOG = {
    "adobe": {
        "emoji": "📱", "title": "Adobe Creative Cloud Pro", "note": "Individual plan",
        "groups": [
            {"title": None, "plans": [
                ("adobe_7d", "7d_t", 200, "7d_t"),
                ("adobe_1m", "1m", 1450, "1m"),
                ("adobe_12m", "12m", 14600, "12m"),
            ]},
        ],
    },
    "canva": {
        "emoji": "⚪", "title": "Canva Pro", "note": "",
        "groups": [
            {"title": None, "plans": [
                ("canva_30t", "30d_t", 200, "30d_t"),
                ("canva_1d", "1d", 120, "1d"),
                ("canva_1w", "1w", 250, "1w"),
                ("canva_1m", "1m", 618, "1m"),
                ("canva_12m", "12m", 4600, "12m"),
            ]},
        ],
    },
    "netflix": {
        "emoji": "🎥", "title": "Netflix", "note": "",
        "groups": [
            {"title": None, "plans": [
                # Срок для Netflix в прайсе не указан - поставлен 1 месяц, поправьте при необходимости
                ("nf_premium", "Premium 4K", 1000, "1m"),
                ("nf_mobile", "Mobile", 300, "1m"),
                ("nf_basic", "Basic", 400, "1m"),
                ("nf_standard", "Standard", 700, "1m"),
            ]},
        ],
    },
    "crunchy": {
        "emoji": "⛩", "title": "Crunchyroll", "note": "",
        "groups": [
            {"title": "Fan", "plans": [
                ("cr_fan_1m", "1m", 150, "1m"),
                ("cr_fan_12m", "12m", 540, "12m"),
            ]},
            {"title": "Mega Fan", "plans": [
                ("cr_mega_1m", "1m", 180, "1m"),
                ("cr_mega_12m", "12m", 700, "12m"),
            ]},
            {"title": "Ultimate Fan", "plans": [
                ("cr_ult_1m", "1m", 400, "1m"),
                ("cr_ult_12m", "12m", 1270, "12m"),
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
                "label_code": _label, "cents": _cents, "period_code": _period,
            }


def money(cents):
    s = f"{cents / 100:.2f}".rstrip("0").rstrip(".")
    return "$" + s


def plan_title(p):
    name = CATALOG[p["svc"]]["title"]
    if p["group"]:
        name += " " + p["group"]
    return name


def plabel(p, lang):
    return PERIOD_LABELS.get(p["label_code"], {}).get(lang, p["label_code"])


def pperiod(p, lang):
    return PERIOD_LABELS[p["period_code"]][lang]


def back_target(p):
    """Куда ведёт кнопка 'Назад' с экрана тарифа."""
    svc = CATALOG[p["svc"]]
    if len(svc["groups"]) > 1:
        return f"grp:{p['svc']}:{p['gi']}"
    return f"svc:{p['svc']}"


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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bot_users (
            user_id INTEGER PRIMARY KEY,
            lang TEXT,
            notify INTEGER DEFAULT 1,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def get_bot_user(user_id):
    conn = db()
    row = conn.execute("SELECT * FROM bot_users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def set_user_lang(user_id, lang):
    conn = db()
    conn.execute(
        "INSERT INTO bot_users(user_id, lang, notify, created_at) VALUES(?,?,1,?) "
        "ON CONFLICT(user_id) DO UPDATE SET lang = excluded.lang",
        (user_id, lang, now_iso()),
    )
    conn.commit()
    conn.close()


def toggle_notify(user_id, default_lang):
    u = get_bot_user(user_id)
    if not u:
        set_user_lang(user_id, default_lang)
        u = get_bot_user(user_id)
    new = 0 if u["notify"] else 1
    conn = db()
    conn.execute("UPDATE bot_users SET notify = ? WHERE user_id = ?", (new, user_id))
    conn.commit()
    conn.close()
    return new


def lang_of(user):
    """Язык пользователя: сохранённый или определённый по языку Telegram."""
    u = get_bot_user(user.id)
    if u and u["lang"] in LANGS:
        return u["lang"]
    return detect_lang(user.language_code)


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


# ---------- Клавиатуры ----------
def btn(text, data=None, url=None):
    if url:
        return types.InlineKeyboardButton(text=text, url=url)
    return types.InlineKeyboardButton(text=text, callback_data=data)


def menu_kb(lang):
    """Постоянное нижнее меню (под полем ввода)."""
    k = types.ReplyKeyboardMarkup(
        resize_keyboard=True, row_width=2, input_field_placeholder=tr(lang, "placeholder")
    )
    k.row(types.KeyboardButton(tr(lang, "m_catalog")), types.KeyboardButton(tr(lang, "m_orders")))
    k.row(types.KeyboardButton(tr(lang, "m_how")), types.KeyboardButton(tr(lang, "m_faq")))
    k.row(types.KeyboardButton(tr(lang, "m_settings")), types.KeyboardButton(tr(lang, "m_support")))
    return k


def language_kb(onboarding):
    k = types.InlineKeyboardMarkup(row_width=1)
    flag = 1 if onboarding else 0
    for lg in LANGS:
        k.add(btn(LANG_NAMES[lg], f"lang:{lg}:{flag}"))
    return k


async def show(call, text, markup):
    """Меняет текущее сообщение (чтобы меню не плодило новые сообщения)."""
    try:
        await call.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    except MessageNotModified:
        pass
    except Exception:
        await call.message.answer(text, reply_markup=markup, disable_web_page_preview=True)


def catalog_kb(lang):
    k = types.InlineKeyboardMarkup(row_width=1)
    for skey, svc in CATALOG.items():
        k.add(btn(f"{svc['emoji']} {svc['title']}", f"svc:{skey}"))
    return k


def welcome_text(lang):
    return tr(lang, "welcome", support=SUPPORT_URL)


async def send_welcome(message, lang):
    await message.answer(welcome_text(lang), reply_markup=menu_kb(lang), disable_web_page_preview=True)


# ---------- /start и язык ----------
@dp.message_handler(commands=["start", "menu"])
async def cmd_start(message: types.Message):
    user = message.from_user
    if not get_bot_user(user.id):
        # первый запуск - предлагаем выбрать язык
        await message.answer(
            "🌐 <b>Выберите язык · Choose language · Tilni tanlang</b>\n" + LINE,
            reply_markup=language_kb(onboarding=True),
        )
        return
    await send_welcome(message, lang_of(user))


@dp.message_handler(commands=["lang", "language"])
async def cmd_lang(message: types.Message):
    lang = lang_of(message.from_user)
    await message.answer(tr(lang, "choose_language"), reply_markup=language_kb(onboarding=False))


@dp.callback_query_handler(lambda c: c.data and c.data.startswith("lang:"))
async def cb_set_lang(call: types.CallbackQuery):
    await call.answer()
    try:
        _, lg, flag = call.data.split(":")
    except ValueError:
        return
    if lg not in LANGS:
        return
    set_user_lang(call.from_user.id, lg)
    try:
        await call.message.delete()
    except Exception:
        pass
    if flag == "1":
        await send_welcome(call.message, lg)
    else:
        await call.message.answer(
            tr(lg, "lang_changed", lang=LANG_NAMES[lg]), reply_markup=menu_kb(lg)
        )
        await send_settings(call.message, call.from_user, lg)


# ---------- Нижнее меню ----------
@dp.message_handler(lambda m: m.text in MENU_ACTIONS)
async def on_menu(message: types.Message):
    user = message.from_user
    lang = lang_of(user)
    action = MENU_ACTIONS[message.text]

    if action == "m_catalog":
        await message.answer(tr(lang, "catalog"), reply_markup=catalog_kb(lang))

    elif action == "m_orders":
        await message.answer(orders_text(user.id, lang), disable_web_page_preview=True)

    elif action == "m_how":
        await message.answer(tr(lang, "how"))

    elif action == "m_faq":
        await message.answer(tr(lang, "faq", support=SUPPORT_URL), disable_web_page_preview=True)

    elif action == "m_settings":
        await send_settings(message, user, lang)

    elif action == "m_support":
        k = types.InlineKeyboardMarkup()
        k.add(btn("💬 " + tr(lang, "support_btn"), url=SUPPORT_URL))
        await message.answer(tr(lang, "support"), reply_markup=k)


# ---------- Настройки ----------
def settings_content(user, lang):
    u = get_bot_user(user.id)
    notify_on = (u["notify"] if u else 1) == 1
    state = tr(lang, "on") if notify_on else tr(lang, "off")
    n = len(user_orders(user.id, 1000))
    text = tr(lang, "settings", uid=user.id, lang=LANG_NAMES[lang], notif=state, n=n)
    k = types.InlineKeyboardMarkup(row_width=1)
    k.add(btn(tr(lang, "s_lang", lang=LANG_NAMES[lang]), "setlang"))
    k.add(btn(tr(lang, "s_notif", state=state), "notif"))
    return text, k


async def send_settings(message, user, lang):
    text, k = settings_content(user, lang)
    await message.answer(text, reply_markup=k)


@dp.callback_query_handler(lambda c: c.data == "setlang")
async def cb_open_lang(call: types.CallbackQuery):
    await call.answer()
    lang = lang_of(call.from_user)
    await show(call, tr(lang, "choose_language"), language_kb(onboarding=False))


@dp.callback_query_handler(lambda c: c.data == "notif")
async def cb_notif(call: types.CallbackQuery):
    lang = lang_of(call.from_user)
    toggle_notify(call.from_user.id, lang)
    await call.answer("✅")
    text, k = settings_content(call.from_user, lang)
    await show(call, text, k)


# ---------- Каталог ----------
@dp.callback_query_handler(lambda c: c.data == "cat")
async def cb_catalog(call: types.CallbackQuery):
    await call.answer()
    lang = lang_of(call.from_user)
    await show(call, tr(lang, "catalog"), catalog_kb(lang))


def plans_screen(skey, gi, lang):
    svc = CATALOG[skey]
    grp = svc["groups"][gi]
    title = f"{svc['emoji']} <b>{h(svc['title'])}</b>"
    if grp["title"]:
        title += f" — <b>{h(grp['title'])}</b>"
    if svc["note"]:
        title += f"\n<i>{h(svc['note'])}</i>"
    k = types.InlineKeyboardMarkup(row_width=1)
    for pid, label_code, cents, _period in grp["plans"]:
        label = PERIOD_LABELS.get(label_code, {}).get(lang, label_code)
        k.add(btn(f"{label} — {money(cents)}", f"plan:{pid}"))
    k.add(btn(tr(lang, "back"), "cat" if len(svc["groups"]) == 1 else f"svc:{skey}"))
    return title + tr(lang, "choose_plan"), k


@dp.callback_query_handler(lambda c: c.data and c.data.startswith("svc:"))
async def cb_service(call: types.CallbackQuery):
    await call.answer()
    lang = lang_of(call.from_user)
    skey = call.data.split(":", 1)[1]
    svc = CATALOG.get(skey)
    if not svc:
        return
    if len(svc["groups"]) == 1:
        text, k = plans_screen(skey, 0, lang)
        await show(call, text, k)
        return
    k = types.InlineKeyboardMarkup(row_width=1)
    for gi, grp in enumerate(svc["groups"]):
        k.add(btn(grp["title"], f"grp:{skey}:{gi}"))
    k.add(btn(tr(lang, "back"), "cat"))
    await show(call, tr(lang, "choose_level", emoji=svc["emoji"], title=h(svc["title"])), k)


@dp.callback_query_handler(lambda c: c.data and c.data.startswith("grp:"))
async def cb_group(call: types.CallbackQuery):
    await call.answer()
    lang = lang_of(call.from_user)
    try:
        _, skey, gi = call.data.split(":")
        text, k = plans_screen(skey, int(gi), lang)
    except Exception:
        return
    await show(call, text, k)


@dp.callback_query_handler(lambda c: c.data and c.data.startswith("plan:"))
async def cb_plan(call: types.CallbackQuery):
    await call.answer()
    lang = lang_of(call.from_user)
    pid = call.data.split(":", 1)[1]
    p = PLANS.get(pid)
    if not p:
        return
    text = tr(
        lang, "plan_card",
        title=h(plan_title(p)), label=h(plabel(p, lang)),
        period=h(pperiod(p, lang)), price=money(p["cents"]),
    )
    k = types.InlineKeyboardMarkup(row_width=1)
    k.add(btn(tr(lang, "pay_btn", price=money(p["cents"])), f"pay:{pid}"))
    k.add(btn(tr(lang, "back"), back_target(p)))
    await show(call, text, k)


# ---------- Оплата ----------
@dp.callback_query_handler(lambda c: c.data and c.data.startswith("pay:"))
async def cb_pay(call: types.CallbackQuery):
    await call.answer()
    lang = lang_of(call.from_user)
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
                    "product_data": {"name": f"{plan_title(p)} - {plabel(p, 'en')}"},
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
        await call.message.answer(tr(lang, "pay_error", support=SUPPORT_URL), disable_web_page_preview=True)
        return

    update_order(oid, session_id=session.id)
    text = tr(
        lang, "order_card",
        oid=oid, title=h(plan_title(p)), label=h(plabel(p, lang)), price=money(p["cents"]),
    )
    k = types.InlineKeyboardMarkup(row_width=1)
    k.add(btn(tr(lang, "pay_btn", price=money(p["cents"])), url=session.url))
    k.add(btn(tr(lang, "paid_btn"), f"chk:{oid}"))
    k.add(btn(tr(lang, "back"), f"plan:{pid}"))
    await show(call, text, k)


def ask_email_text(order, lang):
    p = PLANS[order["plan_id"]]
    return tr(lang, "ask_email", title=h(plan_title(p)))


@dp.callback_query_handler(lambda c: c.data and c.data.startswith("chk:"))
async def cb_check(call: types.CallbackQuery):
    lang = lang_of(call.from_user)
    try:
        oid = int(call.data.split(":", 1)[1])
    except ValueError:
        await call.answer()
        return
    order = get_order(oid)
    if not order or order["user_id"] != call.from_user.id:
        await call.answer(tr(lang, "order_not_found"), show_alert=True)
        return

    status = order["status"]
    if status == "wait_email":
        await call.answer()
        await call.message.answer(ask_email_text(order, lang))
        return
    if status == "wait_password":
        await call.answer()
        await call.message.answer(tr(lang, "ask_password"))
        return
    if status in ("submitted", "done"):
        await call.answer(tr(lang, "already_in_work"), show_alert=True)
        return

    # status == "new": проверяем оплату у Stripe
    try:
        session = as_dict(stripe.checkout.Session.retrieve(order["session_id"]))
    except Exception as e:
        print("Stripe retrieve error:", e, flush=True)
        await call.answer(tr(lang, "pay_check_fail"), show_alert=True)
        return

    if session.get("payment_status") != "paid":
        await call.answer(tr(lang, "pay_not_yet"), show_alert=True)
        return

    await call.answer()
    if claim_paid(oid):
        order = get_order(oid)
        await call.message.answer(ask_email_text(order, lang))


# ---------- Получение email и пароля ----------
@dp.message_handler(lambda m: m.text and not m.text.startswith("/"))
async def on_text(message: types.Message):
    user = message.from_user
    lang = lang_of(user)
    order = get_awaiting_order(user.id)
    if not order:
        await message.answer(tr(lang, "unknown"), reply_markup=menu_kb(lang))
        return

    if order["status"] == "wait_email":
        email = message.text.strip()
        if not EMAIL_RE.match(email):
            await message.answer(tr(lang, "bad_email"))
            return
        update_order(order["id"], email=email, status="wait_password")
        await message.answer(tr(lang, "ask_password"))
        return

    if order["status"] == "wait_password":
        password = message.text.strip()
        try:
            await message.delete()  # убираем пароль из чата
        except Exception:
            pass
        update_order(order["id"], status="submitted")
        order = get_order(order["id"])
        sent = await notify_admins(order, user, order["email"], password)
        if sent:
            await message.answer(
                tr(lang, "order_accepted", oid=order["id"]),
                reply_markup=menu_kb(lang), disable_web_page_preview=True,
            )
        else:
            await message.answer(
                tr(lang, "admin_fail", oid=order["id"], support=SUPPORT_URL),
                disable_web_page_preview=True,
            )


async def notify_admins(order, user, email, password):
    # Сообщение для продавца всегда на русском
    p = PLANS[order["plan_id"]]
    who = f"<a href=\"tg://user?id={user.id}\">{h(user.full_name)}</a>"
    if user.username:
        who += f" (@{h(user.username)})"
    text = (
        f"🆕 <b>Новый заказ #{order['id']}!</b>\n\n"
        f"📦 Сервис: <b>{h(plan_title(p))}</b>\n"
        f"🏷 Тариф: {h(plabel(p, 'ru'))}\n"
        f"⏱ Оформить на: <b>{h(pperiod(p, 'ru'))}</b>\n"
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

    buyer = get_bot_user(order["user_id"])
    buyer_lang = buyer["lang"] if buyer and buyer["lang"] in LANGS else "ru"
    notify_on = (buyer["notify"] if buyer else 1) == 1

    note = "" if notify_on else "\n" + tr("ru", "notif_off_note")
    await call.answer("Отмечено выполненным ✅")
    try:
        await call.message.edit_text(
            call.message.html_text + "\n\n✅ <b>ВЫПОЛНЕНО</b>" + note, reply_markup=None
        )
    except Exception:
        pass

    if not notify_on:
        return
    p = PLANS.get(order["plan_id"])
    name = plan_title(p) if p else "-"
    try:
        await bot.send_message(
            order["user_id"],
            tr(buyer_lang, "order_done", oid=oid, title=h(name), support=SUPPORT_URL),
            disable_web_page_preview=True,
        )
    except Exception as e:
        print("Не удалось уведомить покупателя:", e, flush=True)


# ---------- Мои заказы ----------
def orders_text(user_id, lang):
    rows = user_orders(user_id)
    if not rows:
        return tr(lang, "orders_title") + "\n" + tr(lang, "no_orders")
    lines = [tr(lang, "orders_title")]
    for r in rows:
        p = PLANS.get(r["plan_id"])
        name = f"{plan_title(p)} — {plabel(p, lang)}" if p else r["plan_id"]
        status = tr(lang, "st_" + r["status"]) if ("st_" + r["status"]) in T["ru"] else r["status"]
        lines.append(f"\n<b>#{r['id']}</b> · {h(name)}\n{status}")
    return "\n".join(lines)


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
        name = f"{plan_title(p)} — {plabel(p, 'ru')}" if p else r["plan_id"]
        who = f"@{h(r['username'])}" if r["username"] else f"id {r['user_id']}"
        st = tr("ru", "st_" + r["status"]) if ("st_" + r["status"]) in T["ru"] else r["status"]
        lines.append(f"#{r['id']} · {h(name)} · {money(r['price_cents'])} · {who}\n   {st}")
    await message.answer("\n".join(lines)[:4000])


# ---------- Flask: страницы после оплаты ----------
app = Flask(__name__)


@app.route("/")
def index():
    return "ok", 200


@app.route("/success.html")
def success_page():
    return "<h2>Оплата прошла! / Payment successful</h2><p>Вернитесь в Telegram и нажмите «Я оплатил».</p>"


@app.route("/cancel.html")
def cancel_page():
    return "<h2>Оплата отменена / Payment cancelled</h2><p>Вернитесь в Telegram и попробуйте снова.</p>"


def run_flask():
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))


# ---------- Запуск ----------
if __name__ == "__main__":
    init_db()
    threading.Thread(target=run_flask, daemon=True).start()
    executor.start_polling(dp, skip_updates=True)
