import os
import logging
import asyncio
import threading
import time
import re
import json
import io
import requests
import hmac
import hashlib
import base64
import csv
import unicodedata
from datetime import datetime
from io import BytesIO
from urllib.parse import urlencode
from decimal import Decimal, InvalidOperation

from bs4 import BeautifulSoup
from flask import Flask, request, jsonify

from telegram import (
    Update, BotCommand, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, ReplyKeyboardRemove,
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters, ConversationHandler,
)
from telegram.ext import Application  # для аннотации в post_init

from openai import AsyncOpenAI
from PIL import Image

import gspread
from oauth2client.service_account import ServiceAccountCredentials

import aiocron

# ✅ Для защиты от rate limit Google Sheets (если используешь ретраи)
from tenacity import retry, wait_fixed, stop_after_attempt

# 🔐 Конфиг (токены/ключи)
from config import (
    TELEGRAM_TOKEN,
    OPENAI_API_KEY,
    TON_API_TOKEN,
    CRYPTOCLOUD_API_KEY,
    CRYPTOCLOUD_SHOP_ID,
    API_SECRET,
)

# Инициализация OpenAI-клиента (используется в ask_gpt_vision / handle_strategy_text и др.)
client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# Глобальный бот для уведомлений из вебхука (инициализируется в main())
global_bot = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PHOTO_PATH = os.path.join(BASE_DIR, "banner.jpg")
VIDEO_PATH = os.path.join(BASE_DIR, "Video_TBX.mp4")  # файл в корне!

app_flask = Flask(__name__)  # <— создаём один раз глобально

# --- анти‑дубликаты (idempotency) ---
PROCESSED_PAYMENTS: dict[str, float] = {} # хранит уникальные payment_id/tx_id/комбинации
PROCESSED_TTL_SEC = 3600  # 1 час

# 🚨 Проверка критичных ENV переменных
required_env = ["GOOGLE_CREDS", "TELEGRAM_TOKEN", "OPENAI_API_KEY"]
for var in required_env:
    if not os.getenv(var):
        raise EnvironmentError(f"🚨 Переменная окружения {var} не установлена!")

# ✅ Подключение к Google Sheets
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds_dict = json.loads(os.getenv("GOOGLE_CREDS"))
creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
gc = gspread.authorize(creds)

SPREADSHEET_ID = "1s_KQLyekb-lQjt3fMlBO39CTBuq0ayOIeKkXEhDjhbs"
sheet = gc.open_by_key(SPREADSHEET_ID).sheet1

def save_referral_data(user_id, username, ref_program, broker, uid):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    row = [str(user_id), username, now, ref_program, broker, uid]
    sheet.append_row(row)

# ✅ Rate-limit safe append для Sheets
@retry(wait=wait_fixed(2), stop=stop_after_attempt(5))
def safe_append_row(row):
    sheet.append_row(row)

def load_allowed_users():
    try:
        records = sheet.get_all_records()
        logging.info(f"🔄 Загружено {len(records)} строк из Google Sheets.")
        
        users = set()
        for row in records:
            if "user_id" in row and row["user_id"]:
                try:
                    users.add(int(row["user_id"]))
                except ValueError:
                    logging.warning(f"⚠️ Не удалось преобразовать user_id: {row['user_id']}")
        
        logging.info(f"✅ Загружено {len(users)} пользователей с доступом.")
        return users

    except Exception as e:
        logging.error(f"❌ Ошибка при загрузке пользователей из Google Sheets: {e}")
        return set()

MONTHLY_PRICE_USD = 25
LIFETIME_PRICE_USD = 199
PAY_CURRENCY = "USDT"
PAY_NETWORK = "TRC20"

# 🚀 ALLOWED_USERS с TTL cache (фон)
ALLOWED_USERS = set()
ALLOWED_USERS_TIMESTAMP = 0
_ALLOWED_REFRESHING = False
_ALLOWED_LOCK = threading.Lock()

def get_allowed_users():
    """
    Возвращает кеш авторизованных пользователей.
    Если TTL (5 мин) истёк — триггерит фоновое обновление из Google Sheets
    без блокировки async-хендлеров. При неуспехе сохраняет старый кеш.
    """
    global ALLOWED_USERS, ALLOWED_USERS_TIMESTAMP, _ALLOWED_REFRESHING

    now = time.time()
    ttl_expired = (now - ALLOWED_USERS_TIMESTAMP) > 300

    if ttl_expired and not _ALLOWED_REFRESHING:
        # Ставим флаг ДО старта потока, чтобы не запустить несколько раз подряд
        _ALLOWED_REFRESHING = True

        def _refresh():
            global ALLOWED_USERS, ALLOWED_USERS_TIMESTAMP, _ALLOWED_REFRESHING
            try:
                updated = load_allowed_users()
                # Обновляем кеш и TTL только при успешной загрузке
                if updated:
                    with _ALLOWED_LOCK:
                        ALLOWED_USERS = updated
                        ALLOWED_USERS_TIMESTAMP = time.time()
            except Exception:
                logging.exception("[get_allowed_users] refresh failed")
            finally:
                _ALLOWED_REFRESHING = False

        threading.Thread(target=_refresh, daemon=True).start()

    return ALLOWED_USERS


TON_WALLET = "UQC4nBKWF5sO2UIP9sKl3JZqmmRlsGC5B7xM7ArruA61nTGR"
PENDING_USERS = {}
RECEIVED_MEMOS = set()

reply_keyboard = [
    ["💡 Инвестор", "🚀 Трейдер", "🔍 Новости"],
    ["📚 Термин", "🌱 Психолог"],
    ["🎯 Калькулятор", "💸 Криптообмен"],
    ["💰 Купить", "ℹ️ О боте"],
    ["🔗 Бесплатный доступ через брокера"],
    ["📌 Сетап"]
]
REPLY_MARKUP = ReplyKeyboardMarkup(reply_keyboard, resize_keyboard=True)

CHAT_DISCUSS_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("💬 Обсудить в чате", url="https://t.me/ai4traders_chat")]
])

INTERPRET_NEWS, ASK_EVENT, ASK_FORECAST, ASK_ACTUAL, GENERAL_QUESTION, FOLLOWUP_1, FOLLOWUP_2, FOLLOWUP_3 = range(8)
user_inputs = {}

WAITING_FOR_PHOTO = set()
WAITING_FOR_THERAPY_INPUT = 100

RISK_CALC_1, RISK_CALC_2, RISK_CALC_3 = range(101, 104)
SETUP_1, SETUP_2, SETUP_3, SETUP_4, SETUP_5 = range(301, 306)

async def setup_instrument(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["instrument"] = update.message.text.strip()
    await update.message.reply_text("📉 Теперь укажи область риска (зона покупки):")
    return SETUP_2

async def setup_risk_area(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["risk_area"] = update.message.text.strip()
    await update.message.reply_text("🎯 Какие цели (тейки) по сделке?")
    return SETUP_3

async def setup_targets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["targets"] = update.message.text.strip()
    await update.message.reply_text("🚨 Где стоит стоп-лосс?")
    return SETUP_4

async def setup_stoploss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["stoploss"] = update.message.text.strip()
    await update.message.reply_text("📷 Прикрепи скрин сетапа.")
    return SETUP_5

async def start_risk_calc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # сохраняем ранее выбранные ключи и чистим остальное
    keys_to_keep = {"selected_market", "selected_strategy"}
    saved = {k: v for k, v in (context.user_data or {}).items() if k in keys_to_keep}
    context.user_data.clear()
    context.user_data.update(saved)

    msg = update.effective_message
    await msg.reply_text(
        "📊 Введи размер депозита в $:",
        reply_markup=ReplyKeyboardMarkup([["↩️ Выйти в меню"]], resize_keyboard=True)
    )
    return RISK_CALC_1


async def risk_calc_deposit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user_text = (msg.text or "").strip()

    if user_text in ("↩️ Выйти в меню", "↩️ Вернуться в меню"):
        context.user_data.clear()
        await msg.reply_text("🔙 Вернулись в главное меню.", reply_markup=REPLY_MARKUP)
        return ConversationHandler.END

    try:
        # поддерживаем "1 000,50", "1000.5", "1000"
        deposit = float(user_text.replace(" ", "").replace("%", "").replace(",", "."))
        if deposit <= 0:
            raise ValueError("deposit must be > 0")
        context.user_data["deposit"] = deposit
        await msg.reply_text("💡 Теперь введи процент риска на сделку (%):")
        return RISK_CALC_2
    except Exception:
        await msg.reply_text("❗️ Введи число. Пример: 1000")
        return RISK_CALC_1


async def risk_calc_risk_percent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user_text = (msg.text or "").strip()

    if user_text in ("↩️ Выйти в меню", "↩️ Вернуться в меню"):
        context.user_data.clear()
        await msg.reply_text("🔙 Вернулись в главное меню.", reply_markup=REPLY_MARKUP)
        return ConversationHandler.END

    try:
        risk_percent = float(user_text.replace(" ", "").replace("%", "").replace(",", "."))
        if not (0 < risk_percent < 100):
            raise ValueError("risk % out of range")
        context.user_data["risk_percent"] = risk_percent
        await msg.reply_text("⚠️ Введи стоп-лосс по сделке (%):")
        return RISK_CALC_3
    except Exception:
        await msg.reply_text("❗️ Введи число. Пример: 2")
        return RISK_CALC_2


async def risk_calc_stoploss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user_text = (msg.text or "").strip()

    if user_text in ("↩️ Выйти в меню", "↩️ Вернуться в меню"):
        context.user_data.clear()
        await msg.reply_text("🔙 Вернулись в главное меню.", reply_markup=REPLY_MARKUP)
        return ConversationHandler.END

    try:
        stoploss_percent = float(user_text.replace(" ", "").replace("%", "").replace(",", "."))
        if not (0 < stoploss_percent < 100):
            raise ValueError("sl % out of range")

        deposit = float(context.user_data.get("deposit", 0))
        risk_percent = float(context.user_data.get("risk_percent", 0))
        if deposit <= 0 or risk_percent <= 0:
            # на случай прямого вызова без предыдущих шагов
            await msg.reply_text("⚠️ Начни заново: /start → 🎯 Калькулятор")
            return ConversationHandler.END

        risk_amount = deposit * risk_percent / 100.0
        position_size = risk_amount / (stoploss_percent / 100.0)

        await msg.reply_text(
            f"✅ Результат:\n"
            f"• Депозит: ${deposit:.2f}\n"
            f"• Риск на сделку: {risk_percent:.2f}% (${risk_amount:.2f})\n"
            f"• Стоп-лосс: {stoploss_percent:.2f}%\n\n"
            f"📌 Рекомендуемый объём позиции: ${position_size:.2f}",
            reply_markup=REPLY_MARKUP
        )

    except Exception:
        await msg.reply_text("❗️ Введи число. Пример: 1.5")
        return RISK_CALC_3

    # финал — выходим из диалога и чистим временные поля
    for k in ("deposit", "risk_percent"):
        context.user_data.pop(k, None)
    return ConversationHandler.END

async def check_access(update: Update):
    user_id = update.effective_user.id

    # ✅ Проверка доступа через кеш, который обновляется из Google Sheets
    if user_id not in get_allowed_users():
        await update.message.reply_text(
            f"🔒 Доступ ограничен. Подключи помощника: ${MONTHLY_PRICE_USD}/мес или ${LIFETIME_PRICE_USD} навсегда.",
            reply_markup=REPLY_MARKUP
        )
        return False

    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    chat_id = update.effective_chat.id

    caption = (
        "🚀 *ТВХ — твоя точка входа*\n\n"
        "Точка входа в прибыльный трейдинг: Крипта, Forex и акции.\n"
        "Подключи и зарабатывай 💰\n\n"
        "Выбрать точку входа 👇"
    )

    try:
        with open(VIDEO_PATH, "rb") as anim:
            await context.bot.send_animation(
                chat_id=chat_id,
                animation=anim,
                caption=caption,
                parse_mode="Markdown",
                reply_markup=REPLY_MARKUP
            )
    except Exception as e:
        logging.warning(f"[start] send_animation failed, fallback to text. err={e}")
        await update.message.reply_text(
            caption,
            parse_mode="Markdown",
            reply_markup=REPLY_MARKUP
        )

    return ConversationHandler.END


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data
    msg = query.message

    logging.info(f"[button_handler] Пользователь {user_id} нажал кнопку: {data}")

    # 🚪 Доступ к колбэкам: часть разрешаем без подписки
    FREE_CB = {
        "start_menu",
        "screenshot_help", "screenshot_help_strategy",
        "back_to_signal", "back_to_strategy",
        "get_email", "interpret_calendar",
        "ref_bybit", "ref_forex4you",
        "market_crypto", "market_forex",
        "pro_access_confirm",  # PRO-подсказки разрешаем, разбор платный
        # "start_risk_calc" — обрабатывается ConversationHandler-ом, дубли здесь не нужен
    }
    if user_id not in get_allowed_users() and data not in FREE_CB:
        await msg.reply_text(
            f"🔒 Доступ ограничен. Подключи помощника: ${MONTHLY_PRICE_USD}/мес или ${LIFETIME_PRICE_USD} навсегда.",
            reply_markup=REPLY_MARKUP
        )
        return

    # --- Навигация в меню ---
    if data == "start_menu":
        context.user_data.clear()
        await msg.reply_text(
            "🚀 Возвращаемся в меню! Выбери, что сделать:",
            reply_markup=REPLY_MARKUP
        )
        return

    # --- Выбор рынка (Crypto / Forex) ---
    if data == "market_crypto":
        context.user_data["selected_market"] = "crypto"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🖼 Как правильно сделать скрин", callback_data="screenshot_help")]
        ])
        await query.edit_message_text(
            "📈 Разбор крипторынка по Smart Money Concepts (SMC)\n\n"
            "🚀 Чтобы получить чёткий торговый план (Entry / Stop / TP):\n"
            "1️⃣ Включи LazyScalp Board и проверь, чтобы DV ≥ 300M\n"
            "2️⃣ Отключи LazyScalp, включи:\n"
            "   • LuxAlgo SMC\n"
            "   • Support & Resistance Levels\n"
            "3️⃣ Выбери таймфрейм 4H или 1H\n"
            "4️⃣ Чтобы были видны: BOS, CHoCH, уровни, импульсы, imbalance\n\n"
            "🔽 Пришли скрин — сделаю разбор за 10 секунд 💰",
            reply_markup=keyboard
        )
        return

    if data == "market_forex":
        context.user_data["selected_market"] = "forex"
        if user_id == 407721399:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🧠 У меня PRO-доступ на TradingView", callback_data="pro_access_confirm")],
                [InlineKeyboardButton("🖼 Как правильно сделать скрин", callback_data="screenshot_help")]
            ])
        else:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🖼 Как правильно сделать скрин", callback_data="screenshot_help")]
            ])
        await query.edit_message_text(
            "📊 Разбор Forex по SMC (Smart Money Concepts)\n\n"
            "⚠️ На форексе нет централизованных объёмов, поэтому включи:\n"
            "   • LuxAlgo SMC\n"
            "   • Support & Resistance Levels\n"
            "📌 Таймфрейм 4H или 1H\n"
            "📌 Видны: BOS, CHoCH, уровни, импульсы, imbalance\n\n"
            "🔽 Пришли скрин — сделаю разбор и выдам точки входа и выхода 📈",
            reply_markup=keyboard
        )
        return

    if data == "pro_access_confirm":
        context.user_data["is_pro_user"] = True
        await msg.reply_text(
            "🔓 Включён PRO-анализ графиков.\n\n"
            "Теперь я буду учитывать:\n"
            "✅ Коррекцию/проекцию по Fibo\n"
            "✅ Наклонные и горизонтальные уровни\n"
            "✅ Зоны дисбаланса (FVG)\n"
            "✅ Совпадения по нескольким уровням фибоначчи (кластерные зоны)\n\n"
            "📸 Пришли скрин — я сделаю расширенный анализ!"
        )
        return

    if data == "screenshot_help":
        await msg.reply_text(
            "🖼 Как сделать идеальный скрин для анализа:\n\n"
            "✅ Таймфрейм 4H или 1H\n"
            "✅ Белый фон графика\n"
            "✅ Включены LuxAlgo SMC + Support & Resistance Levels\n"
            "✅ Видны BOS, CHoCH, уровни, импульсы, imbalance\n"
            "✅ Лишние индикаторы — убрать\n"
            "✅ Скрин на весь экран, без панелей\n"
            "✅ Ручные уровни и наклонки — приветствуются\n\n"
            "💡 Чем чище скрин, тем точнее Entry / Stop / TP.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("↩️ Вернуться к сигналу", callback_data="back_to_signal")]
            ])
        )
        return

    if data == "back_to_signal":
        context.user_data.pop("selected_market", None)
        context.user_data.pop("is_pro_user", None)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📉 Crypto", callback_data="market_crypto")],
            [InlineKeyboardButton("💱 Forex", callback_data="market_forex")]
        ])
        await msg.reply_text(
            "📝 Сначала выбери рынок — нажми одну из кнопок ниже:",
            reply_markup=keyboard
        )
        return

    # --- Логика стратегии (инвест) ---
    if data == "strategy_text":
        context.user_data.clear()
        context.user_data["awaiting_strategy"] = "text"
        await msg.reply_text(
            "✍️ Напиши свою инвестиционную цель или вопрос. Я составлю стратегию с учётом текущего рынка.",
            reply_markup=ReplyKeyboardMarkup([["↩️ Выйти в меню"]], resize_keyboard=True)
        )
        return

    if data == "strategy_photo":
        context.user_data.clear()
        context.user_data["awaiting_strategy"] = "photo"
        await msg.reply_text(
            "📸 Пришли скриншот позиции с Bybit или TradingView.\n"
            "Я дам стратегию: уровни покупок, усреднения (DCA) и фиксацию прибыли.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🖼 Как подготовить скрин для стратегии", callback_data="screenshot_help_strategy")]
            ])
        )
        await msg.reply_text(
            "Готово — загружай скрин или нажми «↩️ Выйти в меню».",
            reply_markup=ReplyKeyboardMarkup([["↩️ Выйти в меню"]], resize_keyboard=True)
        )
        return

    if data == "screenshot_help_strategy":
        await msg.reply_text(
            "🖼 Как сделать идеальный скрин для инвест-стратегии:\n\n"
            "✅ Таймфрейм 4H или 1D (средне-/долгосрочно)\n"
            "✅ Белый фон графика\n"
            "✅ Лишние индикаторы — убрать\n"
            "✅ Видны ключевые максимумы/минимумы и уровни S/R\n"
            "✅ Чётко отображены текущая цена и инструмент\n"
            "✅ На скрине есть место для целей и усреднений (DCA)\n"
            "✅ Скрин на весь экран, без панелей\n\n"
            "💡 Чем чище скрин, тем точнее будут уровни входа, усреднения и цели.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("↩️ Вернуться к стратегии", callback_data="back_to_strategy")]
            ])
        )
        return

    if data == "back_to_strategy":
        context.user_data["awaiting_strategy"] = "photo"
        await msg.reply_text(
            "Отлично. Пришли скрин — подготовлю план: первая покупка, усреднения (DCA) и цели фиксации прибыли.",
            reply_markup=ReplyKeyboardMarkup([["↩️ Выйти в меню"]], resize_keyboard=True)
        )
        return

    # --- Прочие колбэки ---
    if data == "get_email":
        context.user_data["awaiting_email"] = True
        await msg.reply_text("✉️ Напиши свой email для получения секретного PDF со стратегиями:")
        return

    if data == "interpret_calendar":
        context.user_data.clear()
        context.user_data["awaiting_calendar_photo"] = True
        await msg.reply_text(
            "📸 Пришли скриншот из экономического календаря. Я распознаю событие и дам интерпретацию.",
            reply_markup=ReplyKeyboardMarkup([["↩️ Выйти в меню"]], resize_keyboard=True)
        )
        return

    # ⚠️ start_risk_calc убран отсюда — это делает ConversationHandler калькулятора

    if data == "ref_bybit":
        context.user_data["ref_program"] = "bybit"
        context.user_data["broker"] = "Bybit"
        context.user_data["awaiting_uid"] = True
        await msg.reply_text(
            "📈 Отлично!\n"
            "Перейди по моей реферальной ссылке и зарегистрируйся на Bybit:\n"
            "👉 https://www.bybit.com/invite?ref=YYVME8\n\n"
            "Внеси депозит от $150 и пришли сюда свой UID для проверки."
        )
        return

    if data == "ref_forex4you":
        context.user_data["ref_program"] = "forex4you"
        context.user_data["broker"] = "Forex4You"
        context.user_data["awaiting_uid"] = True
        await msg.reply_text(
            "📊 Отлично!\n"
            "Перейди по моей реферальной ссылке и зарегистрируйся на Forex4You:\n"
            "👉 https://www.forex4you.org/?affid=hudpyc9\n\n"
            "Внеси депозит от $200 и пришли сюда свой UID для проверки."
        )
        return

    # На случай неожиданных data — просто вернём в меню
    await msg.reply_text("🔙 Вернулись в меню.", reply_markup=REPLY_MARKUP)


async def grant(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Безопасно получаем message
    msg = getattr(update, "message", None)

    # Проверка прав
    user_id = update.effective_user.id if update and update.effective_user else None
    if user_id not in ADMIN_IDS:
        if msg:
            await msg.reply_text("⛔ Эта команда доступна только админу.")
        return

    # Ожидаем: /grant <user_id> <username>
    args = context.args or []
    if len(args) < 2:
        if msg:
            await msg.reply_text("⚠ Используй так: /grant user_id username")
        return

    try:
        target_user_id = int(args[0])
        if target_user_id <= 0:
            raise ValueError("user_id должен быть положительным числом")
    except Exception:
        if msg:
            await msg.reply_text("⚠ user_id должен быть числом. Пример: /grant 123456789 username")
        return

    # Нормализуем username (убираем ведущий @)
    raw_username = args[1]
    target_username = raw_username.lstrip("@").strip()

    try:
        # Добавляем доступ локально
        ALLOWED_USERS.add(target_user_id)

        # Обновляем метку TTL, чтобы кеш не перезатёрся до фонового обновления
        global ALLOWED_USERS_TIMESTAMP
        ALLOWED_USERS_TIMESTAMP = time.time()

        # Запись в Google Sheets — уводим в тред‑пул (не блокируем event loop)
        await asyncio.to_thread(log_payment, target_user_id, target_username)

        # Уведомляем пользователя о выдаче доступа
        await notify_user_payment(target_user_id)

        if msg:
            await msg.reply_text(
                f"✅ Пользователь {target_user_id} (@{target_username}) добавлен в VIP и уведомлён."
            )

    except Exception as e:
        logging.exception("[grant] error")
        if msg:
            await msg.reply_text(f"❌ Ошибка: {e}")


async def reload_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Эта команда доступна только админу.")
        return

    try:
        updated = await asyncio.to_thread(load_allowed_users)
        if not updated:
            await update.message.reply_text("⚠️ Sheets вернул пусто. Кеш ALLOWED_USERS оставлен без изменений.")
            return

        global ALLOWED_USERS, ALLOWED_USERS_TIMESTAMP
        with _ALLOWED_LOCK:
            ALLOWED_USERS = updated
            ALLOWED_USERS_TIMESTAMP = time.time()
        await update.message.reply_text(f"✅ ALLOWED_USERS обновлён: {len(ALLOWED_USERS)} пользователей.")
    except Exception as e:
        logging.error(f"[reload_users] Ошибка: {e}")
        await update.message.reply_text(f"❌ Ошибка при обновлении пользователей.")


def clean_unicode(text):
    return unicodedata.normalize("NFKD", text).encode("utf-8", "ignore").decode("utf-8")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update and update.effective_user else None
    msg = update.effective_message

    # 1) Достаём file_id из фото или документа-изображения
    file_id = None
    if getattr(msg, "photo", None):
        file_id = msg.photo[-1].file_id
    elif getattr(msg, "document", None):
        doc = msg.document
        if (doc.mime_type or "").startswith("image/"):
            file_id = doc.file_id
        else:
            await msg.reply_text("⚠️ Пришли график как фото или как документ-картинку (PNG/JPG). PDF не поддерживается.")
            return
    else:
        await msg.reply_text("⚠️ Не вижу изображения. Пришли как фото или документ-картинку (PNG/JPG).")
        return

    # 2) Скачиваем изображение безопасно
    try:
        tg_file = await context.bot.get_file(file_id)
        bio = BytesIO()
        await tg_file.download_to_memory(bio)
    except Exception:
        logging.exception("[handle_photo] download error")
        await msg.reply_text("⚠️ Не удалось скачать изображение. Пришли поменьше или повтори ещё раз.")
        return

    # 3) Готовим JPEG и base64 для Vision
    try:
        image = Image.open(BytesIO(bio.getvalue())).convert("RGB")
    except Exception:
        await msg.reply_text("⚠️ Не удалось прочитать изображение. Пришли скрин в формате PNG/JPG.")
        return

    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=80)
    image_base64 = base64.b64encode(buffer.getvalue()).decode()

    # 4) Проверяем выбранный рынок
    selected_market = context.user_data.get("selected_market")
    if not selected_market:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💎 Crypto", callback_data="market_crypto")],
            [InlineKeyboardButton("💱 Forex", callback_data="market_forex")],
        ])
        await msg.reply_text(
            "📝 Сначала выбери рынок — нажми одну из кнопок ниже, чтобы я знал, какой анализ тебе нужен:",
            reply_markup=keyboard
        )
        return

    # флаг pro (оставляем как есть; может использоваться в других ветках)
    use_pro = context.user_data.get("is_pro_user") is True and user_id == 407721399  # noqa: F841

    # 5) Промпт без изменений
    prompt_text = (
        f"You are a professional SMC (Smart Money Concepts) trader with 20+ years experience in "
        f"{'crypto' if selected_market == 'crypto' else 'forex'} markets. "
        "You master BOS, CHoCH, liquidity grabs, imbalance zones, OTE, premium/discount levels.\n\n"
        "The chart includes only:\n"
        "- LuxAlgo SMC\n"
        "- Support & Resistance Levels\n\n"
        "🎯 Your task: create a swing trade plan with pending orders (limit or stop).\n"
        "Risk/Reward ratio must be at least 1:3. Even if unclear — estimate based on price action.\n\n"
        "✅ Format:\n"
        "1️⃣ Observations — each bullet starts with 🔹\n"
        "2️⃣ Trade Plan:\n🎯 Entry: $...\n🚨 StopLoss: $...\n💰 TakeProfit: $...\n"
        "3️⃣ Risk Note\n4️⃣ Bias: BUY or SELL\n"
        "✅ End with 2-line Russian summary with emojis (e.g. «Покупка от дисконта 💸📈»)\n\n"
        "📌 Важно: если вход идёт ПРОТИВ текущего тренда (например, SELL в восходящем канале), обязательно объясни, почему он оправдан.\n\n"
        "🚫 Rules:\n- Answer in Russian only\n- No markdown\n- No refusal\n- No apologies"
    )

    # 6) Запрос к Vision (с повтором при «извиняюсь/не могу»)
    analysis = ""
    for attempt in range(2):
        try:
            enhanced_prompt = prompt_text
            if attempt == 1:
                enhanced_prompt += (
                    "\n\n🚨 STRICT ORDER: Даже если график тёмный, малоконтрастный, с шумом или без чёткой структуры — "
                    "ты обязан выдать Entry, StopLoss и TakeProfit. Не отказывайся. "
                    "Если мало данных — оцени по свечам, структуре и зонам. Торговый план ОБЯЗАТЕЛЕН."
                )

            analysis = await ask_gpt_vision(enhanced_prompt, image_base64)
            logging.info(f"[handle_photo attempt {attempt}] Raw GPT analysis:\n{analysis}")

            if not analysis:
                await asyncio.sleep(0.5)
                continue

            low = analysis.lower()
            if "sorry" in low or "can't assist" in low or "cannot" in low or "unable" in low:
                continue
            break
        except Exception as e:
            logging.error(f"[handle_photo retry {attempt}] GPT Vision error: {e}")

    if not analysis:
        analysis = _fallback_strategy()

    # 7) Лёгкий пост-процессинг ответа (без изменения смысла промпта)
    lines = [ln for ln in (analysis or "").splitlines() if ln.strip()]
    lines = [ln for ln in lines if "Краткий план не сформирован" not in ln]
    lines = [ln for ln in lines if not ln.startswith("📈 Направление сделки")]
    text_joined = "\n".join(lines)
    if "Вход:" in text_joined and ("ордер" not in text_joined.lower()):
        text_joined += "\n\nℹ️ Тип ордера: лимитный (Buy Limit) на уровне входа."
    analysis = text_joined

    # --- Не отправляем analysis отдельным сообщением, чтобы не было дублей ---

    def parse_price(raw_text: str | None):
        if not raw_text:
            return None
        try:
            cleaned = (
                raw_text.replace(" ", "")
                        .replace("\u00A0", "")
                        .replace(",", "")
                        .replace("$", "")
                        .replace("—", "-")
            )
            return float(cleaned)
        except Exception:
            return None

    entry_match = re.search(r'(Entry|Вход)[:\s]*\$?\s*([\d\s,.]+)', analysis, flags=re.IGNORECASE) \
        or re.search(r'🎯[:\s]*\$?\s*([\d\s,.]+)', analysis)
    stop_match = re.search(r'(StopLoss|Стоп)[:\s]*\$?\s*([\d\s,.]+)', analysis, flags=re.IGNORECASE) \
        or re.search(r'🚨[:\s]*\$?\s*([\d\s,.]+)', analysis)
    tp_match = re.search(r'(TakeProfit|Тейк)[:\s]*\$?\s*([\d\s,.]+)', analysis, flags=re.IGNORECASE) \
        or re.search(r'💰[:\s]*\$?\s*([\d\s,.]+)', analysis)
    bias_match = re.search(r'\b(BUY|SELL|ПОКУПКА|ПРОДАЖА)\b', analysis, flags=re.IGNORECASE)

    entry = parse_price(entry_match.group(2) if entry_match and entry_match.lastindex == 2 else (entry_match.group(1) if entry_match else None))
    stop = parse_price(stop_match.group(2) if stop_match and stop_match.lastindex == 2 else (stop_match.group(1) if stop_match else None))
    tp = parse_price(tp_match.group(2) if tp_match and tp_match.lastindex == 2 else (tp_match.group(1) if tp_match else None))

    if entry and stop:
        if entry != 0:
            risk_abs = abs(entry - stop)
            risk_pct = abs((entry - stop) / entry * 100)
            risk_line = f"📌 Область риска ≈ ${risk_abs:.2f} ({risk_pct:.2f}%)"
        else:
            risk_line = "📌 Область риска: деление на ноль невозможно (entry=0)."
    else:
        risk_line = "📌 Область риска не указана явно — оценивай внимательно."

    rr_line = ""
    if entry and stop and tp and (entry != stop):
        try:
            rr_ratio = abs((tp - entry) / (entry - stop))
            rr_line = f"📊 R:R ≈ {rr_ratio:.2f}"
            if rr_ratio < 3:
                rr_line += "\n⚠️ R:R ниже 1:3 — план рискованный, подумай дважды."
        except Exception:
            pass

    bias_line = f"📈 Направление сделки: {bias_match.group(1).upper()}" if bias_match else ""

    if entry and stop and tp:
        tldr = f"✅ TL;DR: Вход {entry}, стоп {stop}, тейк {tp}."
        if rr_line:
            tldr += f" {rr_line.splitlines()[0]}"
    else:
        tldr = "✅ Краткий план не сформирован — проверь вход/стоп/тейк."

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📏 Рассчитать риск", callback_data="start_risk_calc")]
    ])

    full_message = f"📉 Анализ графика по SMC:\n\n{analysis}\n\n{risk_line}"
    if rr_line:
        full_message += f"\n{rr_line}"
    if bias_line:
        full_message += f"\n{bias_line}"
    full_message += f"\n\n{tldr}"

    # Используем msg.reply_text (а не update.message) — это устойчиво для фото и документов
    await msg.reply_text(full_message, reply_markup=keyboard)

async def setup_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Получаем фото от пользователя
    photo = update.message.photo[-1]
    file = await photo.get_file()
    photo_bytes = await file.download_as_bytearray()

    # Преобразуем в BytesIO для Telegram API
    image_stream = BytesIO(photo_bytes)
    image_stream.name = "setup.jpg"

    # Собираем данные
    instrument = context.user_data.get("instrument", "Не указано")
    risk_area = context.user_data.get("risk_area")
    targets = context.user_data.get("targets", "Не указано")
    stoploss = context.user_data.get("stoploss", "Не указано")
    entry = context.user_data.get("entry")

    # Авторасчёт области риска
    if not risk_area or risk_area == "Не указано":
        try:
            entry_value = float(entry)
            stop_value = float(stoploss)
            risk_percent = abs((entry_value - stop_value) / entry_value * 100)
            risk_area = f"{risk_percent:.2f}% (авторасчёт)"
        except:
            risk_area = "Не указана — оценивай внимательно"

    caption = (
        f"🚀 Новый сетап от админа\n\n"
        f"• 📌 Инструмент: {instrument}\n"
        f"• 💰 Область риска: {risk_area}\n"
        f"• 🎯 Цели: {targets}\n"
        f"• 🚨 Стоп-лосс: {stoploss}"
    )

    # Кнопка для рассчета риска
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📏 Рассчитать риск", callback_data="start_risk_calc")]
    ])

    try:
        # Отправляем в канал
        chat_id = '@ai4traders'
        message = await context.bot.send_photo(
            chat_id=chat_id,
            photo=image_stream,
            caption=caption,
            reply_markup=keyboard
        )

        # Закрепляем
        await context.bot.pin_chat_message(
            chat_id=chat_id,
            message_id=message.message_id,
            disable_notification=True
        )

        await update.message.reply_text("✅ Сетап опубликован и закреплён в канале!", reply_markup=REPLY_MARKUP)

    except Exception as e:
        logging.error(f"[SETUP_PHOTO] Ошибка публикации: {e}")
        await update.message.reply_text(
            "⚠️ Не удалось опубликовать сетап. Проверь права бота в канале и логи."
        )

    return ConversationHandler.END

def fetch_price_from_binance(symbol: str) -> float | None:
    """
    Получает последнюю цену с Binance через публичный REST API.
    Пример: fetch_price_from_binance("BTC") вернёт цену BTCUSDT.
    """
    try:
        pair = symbol.upper() + "USDT"
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={pair}"
        response = requests.get(url, timeout=10)
        data = response.json()
        return float(data["price"])
    except Exception as e:
        logging.warning(f"[BINANCE] Ошибка получения цены для {symbol}: {e}")
        return None


# === Вставь этот блок в bot.py. Убедись, что вверху файла уже импортированы:
# import re, base64, asyncio, logging
# from io import BytesIO
# from PIL import Image
# from telegram import ReplyKeyboardMarkup, Update
# from telegram.ext import ContextTypes
# и инициализирован клиент OpenAI: client = OpenAI(...)

# -------------------- GPT Vision вызов (обновлённый) --------------------
async def ask_gpt_vision(prompt_text: str, image_base64: str) -> str:
    """
    Жёсткие правила в system + educational framing (снимает отказы).
    Ответ ВСЕГДА строго на русском, без markdown.
    """
    logging.info("[ask_gpt_vision] v2 system/educational RU enforced")
    try:
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an elite multi-asset strategist with 20+ years of institutional experience "
                        "in SMC, portfolio construction and risk control. "
                        "You ALWAYS provide a complete numeric plan and NEVER refuse. "
                        "All content is for educational purposes only and NOT financial advice. "
                        "Always produce a safe, hypothetical plan, not commands to buy or sell. "
                        "All responses MUST be in Russian, with no markdown, short clear sentences, and a friendly tone."
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                prompt_text
                                + "\n\nFINAL RULES: No refusals. If data is unclear, make explicit [допущение]. "
                                  "Answer strictly in Russian."
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"},
                        },
                    ],
                },
            ],
            max_tokens=1800,
            temperature=0.25,
            presence_penalty=0.0,
            frequency_penalty=0.1,
        )
        return (response.choices[0].message.content or "").strip()
    except Exception as e:
        logging.error(f"[ask_gpt_vision] Error during GPT Vision request: {e}")
        return ""


# -------------------- Утилиты: анти-отказ и язык --------------------
refusal_markers = [
    "sorry", "i'm sorry", "cannot assist", "can't assist", "i cannot", "i can’t",
    "unable to", "not able to", "won’t be able", "cannot help", "can’t help",
    "provide financial advice", "financial advice", "disclaimer",
    "не могу", "я не могу", "не буду", "я не буду", "не могу помочь", "не могу с этим помочь",
]

def looks_like_refusal(text: str) -> bool:
    low = (text or "").lower().replace("’", "'")
    return any(m in low for m in refusal_markers)


def not_russian(text: str) -> bool:
    # Грубая эвристика: мало кириллицы → не по‑русски
    cyr = sum("а" <= ch.lower() <= "я" or ch == "ё" for ch in text)
    return cyr < max(20, len(text) // 10)


# -------------------- Парсеры уровней из ответа --------------------
import re

def parse_current_price_x(text: str):
    """Ищем «Текущая цена X = $…» или «цена 4285». Возвращаем float либо None."""
    m = re.search(r"текущая\s+цена\s*x\s*=\s*\$?\s*([\d\s,]+(?:\.\d{1,2})?)", text, flags=re.I)
    if not m:
        m = re.search(r"(?:цена|price)\s*[:=]?\s*\$?\s*([\d\s,]+(?:\.\d{1,2})?)", text, flags=re.I)
    if not m:
        return None
    val = m.group(1).replace(" ", "").replace(",", "")
    try:
        return float(val)
    except:
        return None


def parse_dca_prices(text: str):
    """Ищем цены из блока 4️⃣: Первая покупка / Усреднение 1 / Усреднение 2."""
    lines = []
    block = re.search(r"4️⃣\s*План покупок.*?(?:5️⃣|$)", text, flags=re.S)
    if block:
        block = block.group(0)
        for label in ["Первая покупка", "Усреднение 1", "Усреднение 2"]:
            m = re.search(rf"{label}\s*:\s*\$([\d\s,]+(?:\.\d{{1,2}})?)\s*\(.*?%.*?\)", block, flags=re.I)
            if m:
                val = m.group(1).replace(" ", "").replace(",", "")
                try:
                    lines.append(float(val))
                except:
                    pass
    return lines


def parse_tp_prices(text: str):
    vals = []
    block = re.search(r"5️⃣\s*Тактические сделки.*?(?:6️⃣|$)", text, flags=re.S)
    if block:
        for key in ["TP1", "TP2"]:
            m = re.search(rf"{key}\s*=\s*\$([\d\s,]+(?:\.\d{{1,2}})?)", block.group(0), flags=re.I)
            if m:
                val = m.group(1).replace(" ", "").replace(",", "")
                try:
                    vals.append(float(val))
                except:
                    pass
    return vals


def parse_sl(text: str):
    block = re.search(r"5️⃣\s*Тактические сделки.*?(?:6️⃣|$)", text, flags=re.S)
    if block:
        m = re.search(r"Стоп-лосс\s*:\s*\$([\d\s,]+(?:\.\d{1,2})?)", block.group(0), flags=re.I)
        if m:
            val = m.group(1).replace(" ", "").replace(",", "")
            try:
                return float(val)
            except:
                return None
    return None


# -------------------- Проверка разумности уровней --------------------
def levels_look_reasonable(x, dcas, tps, sl):
    """
    Лонг-логика:
    - 3 DCA-цены: все < X, убывают (entry > d1 > d2), в диапазоне [0.60*X ; 0.99*X]
    - Шаги не микроскопические (>= 0.3% от X)
    - TP1 > X, TP2 > TP1
    - SL < min(DCA), но SL >= 0.40*X
    """
    if x is None or len(dcas) < 3:
        return False

    entry, d1, d2 = dcas[0], dcas[1], dcas[2]
    band_low, band_high = 0.60 * x, 0.99 * x

    for p in (entry, d1, d2):
        if p is None:
            return False
        if not (band_low <= p <= band_high):
            return False
        if p >= x:
            return False

    if not (entry > d1 > d2):
        return False

    min_step = 0.003 * x
    if not (abs(entry - d1) >= min_step and abs(d1 - d2) >= min_step):
        return False

    if len(tps) < 2:
        return False
    tp1, tp2 = tps[0], tps[1]
    if not (tp1 > x and tp2 > tp1):
        return False

    if sl is None:
        return False
    if not (sl < min(entry, d1, d2)):
        return False
    if sl < 0.40 * x:
        return False

    return True


async def handle_strategy_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Инвест-режим: принимает скрин графика и формирует инвестиционную стратегию.
    Промпты на английском (стабильность), ответ строго на русском.
    Всегда возвращает структурированный ответ; при сбоях — детерминированный fallback.
    """
    logging.info("[handle_strategy_photo] investor flow start")
    msg = update.effective_message

    # -------------------- ВНУТРЕННИЕ ХЕЛПЕРЫ --------------------
    def _is_russian(text: str) -> bool:
        if not text:
            return False
        cyr = sum('а' <= ch.lower() <= 'я' or ch == 'ё' for ch in text)
        return (cyr / max(len(text), 1)) >= 0.2

    def _looks_like_refusal(text: str) -> bool:
        if not text:
            return True
        t = text.lower()
        needles = [
            "i can’t assist", "i can't assist", "cannot help", "can't help",
            "as an ai", "i am an ai", "i'm an ai", "unable to", "i cannot", "i can’t",
            "sorry, but", "apologize", "apologies",
            "не могу помочь", "не могу обработать", "не могу проанализировать",
            "как модель искусственного интеллекта"
        ]
        return any(n in t for n in needles)

    def _safe_float(x, default=None):
        try:
            return float(x)
        except Exception:
            return default

    def _rr(entry, stop, tp1):
        entry, stop, tp1 = _safe_float(entry), _safe_float(stop), _safe_float(tp1)
        if entry is None or stop is None or tp1 is None or entry == stop:
            return None
        return abs((tp1 - entry) / (entry - stop))

    # [fallback] детерминированный план на случай полного провала анализа
    def _fallback_strategy():
        X = 100.00  # допущение о «текущей цене», если извлечь не удалось
        entry = round(X * 0.97, 2)
        sl    = round(X * 0.86, 2)
        tp1   = round(X * 1.03, 2)
        tp2   = round(X * 1.06, 2)
        rr_val = _rr(entry, sl, tp1) or 1.5

        text = (
            "0️⃣ Короткая суть (оценочно):\n"
            "• Локально умеренно бычий сценарий. DCA и частичная фиксация.\n"
            "• Акцент на управлении риском и контроле просадки.\n"
            "• Учитывай новости/волатильность и зоны дисбаланса (FVG).\n\n"
            "1️⃣ Точка входа\n"
            f"• Entry: ${entry:.2f}\n\n"
            "2️⃣ Stop‑Loss\n"
            f"• SL: ${sl:.2f}\n\n"
            "3️⃣ Take‑Profit(ы)\n"
            f"• TP1: ${tp1:.2f}\n"
            f"• TP2: ${tp2:.2f}\n\n"
            "4️⃣ R:R\n"
            f"• По TP1: {rr_val:.2f}\n\n"
            "5️⃣ Комментарии/предупреждения\n"
            "• План DCA: докупать по сигналам слабости, риск на сделку ≤ 1.5%.\n"
            "• Не финансовый совет. Сверь уровни на своём графике.\n"
            f"• Текущая цена X ~ ${X:.2f} (оценочно для шаблона)\n"
        )

        summary = {
            "entry": entry,
            "stop": sl,
            "tp": [tp1, tp2],
            "direction": "LONG",
            "rr": round(rr_val, 2),
            "confidence": 0.4
        }
        text += '\n\n' + '"""' + json.dumps(summary, ensure_ascii=False) + '"""'
        return text

    async def _download_image_as_b64() -> str | None:
        """Достаём картинку из photo/document и возвращаем data:URL base64 для multimodal вызова."""
        file_id = None
        if getattr(msg, "photo", None):
            file_id = msg.photo[-1].file_id
        elif getattr(msg, "document", None):
            doc = msg.document
            if (doc.mime_type or "").startswith("image/"):
                file_id = doc.file_id
            elif (doc.mime_type or "").lower().endswith("/pdf"):
                await msg.reply_text("⚠️ PDF не подходит. Пришли график как фото (PNG/JPG).")
                return None
            else:
                await msg.reply_text("⚠️ Пришли график как фото или документ‑картинку (PNG/JPG).")
                return None
        else:
            await msg.reply_text("⚠️ Не вижу изображения. Пришли график как фото или документ (PNG/JPG).")
            return None

        try:
            tg_file = await context.bot.get_file(file_id)
            bio = io.BytesIO()
            await tg_file.download_to_memory(out=bio)
            bio.seek(0)
            b64 = base64.b64encode(bio.read()).decode("utf-8")
            return f"data:image/png;base64,{b64}"
        except Exception as e:
            logging.error(f"[handle_strategy_photo] download error: {e}", exc_info=True)
            await msg.reply_text("⚠️ Не удалось скачать изображение. Пришли скрин ещё раз.")
            return None

    # -------------------- СКАЧИВАНИЕ И ПРОМПТЫ (EN) --------------------
    image_b64_url = await _download_image_as_b64()
    if not image_b64_url:
        return  # пользователю уже отправили пояснение

    system_role = (
        "You are an institutional investment strategist with 20+ years of experience, "
        "specializing in cryptocurrency markets, mid- and long-term investments without leverage. "
        "Provide structured, professional, and executable analysis.\n\n"
        "IMPORTANT:\n"
        "- Your response must be strictly in Russian (Cyrillic).\n"
        "- Never refuse to answer; never say 'as an AI'.\n"
        "- Always follow this structure:\n"
        "0) Short summary (3 lines)\n"
        "1) Entry point\n"
        "2) Stop-Loss\n"
        "3) Take-Profit levels (at least TP1 and TP2)\n"
        "4) Risk-to-Reward ratio (R:R)\n"
        "5) Comments / Warnings\n\n"
        "REQUIREMENTS:\n"
        "- Concrete price levels in USD ($) with 2 decimals.\n"
        "- Minimum R:R by TP1 must be ≥ 1.5; if lower, explicitly warn and propose a correction.\n"
        "- Mention risk warnings: volatility spikes, FVG, news events, liquidity zones.\n"
        "- No AI disclaimers. Be concise and professional."
    )

    user_prompt = (
        "Analyze the attached trading chart in an investment context (preferred timeframe: 1D or 1W). "
        "Determine the overall market bias, identify nearby key levels, and provide:\n"
        "- Entry point (USD)\n- Stop-Loss (USD)\n- At least two Take-Profit levels (USD)\n"
        "- Risk-to-Reward ratio (R:R)\n- Short comments/warnings on risks (volatility spikes, FVG, news)\n\n"
        "⚠️ Respond strictly in Russian, following the required structure."
    )

    messages = [
        {"role": "system", "content": system_role},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user_prompt},
                {"type": "input_image", "image_url": image_b64_url},
            ],
        },
    ]

    # -------------------- ВЫЗОВ МОДЕЛИ --------------------
    analysis = None
    try:
        resp = await client.chat.completions.create(
            model="gpt-4o",
            temperature=0.2,
            top_p=0.9,
            max_tokens=900,
            messages=messages,
        )
        analysis = (resp.choices[0].message.content or "").strip() if resp.choices else ""
    except Exception as e:
        logging.error(f"[handle_strategy_photo] LLM error: {e}", exc_info=True)
        analysis = None

    # -------------------- ПОСТ-ВАЛИДАЦИЯ И ФОЛБЭКИ --------------------
    if not analysis:
        logging.warning("[handle_strategy_photo] empty analysis — using fallback")
        analysis = _fallback_strategy()

    if _looks_like_refusal(analysis) or not _is_russian(analysis):
        logging.warning("[handle_strategy_photo] refusal or non-RU — using fallback")
        analysis = _fallback_strategy()

    # Простая sanity‑проверка R:R: попытаемся извлечь уровни из ответа
    def _find_money(label: str) -> float | None:
        pat = re.compile(rf"{label}[^$]*\$\s*([0-9]+(?:\.[0-9]{{1,2}})?)", re.IGNORECASE)
        m = pat.search(analysis)
        return _safe_float(m.group(1)) if m else None

    entry = _find_money("Entry") or _find_money("вход") or None
    stop  = _find_money("SL") or _find_money("Stop") or _find_money("стоп") or None
    tp1   = _find_money("TP1") or _find_money("тейк") or None

    rr_val = _rr(entry, stop, tp1)
    if rr_val is not None and rr_val < 1.5:
        analysis += (
            "\n\n⚠️ Предупреждение: вычисленный R:R по TP1 ниже 1.5. "
            "Рассмотри более консервативный SL или более дальний TP для улучшения соотношения."
        )

    # -------------------- ОТВЕТ ПОЛЬЗОВАТЕЛЮ --------------------
    await msg.reply_text(
        f"📊 Инвестиционная стратегия по твоему скрину:\n\n{analysis}",
        reply_markup=ReplyKeyboardMarkup([["↩️ Выйти в меню"]], resize_keyboard=True)
    )
    context.user_data.clear()


# --- INVEST QUESTION (текстовая стратегия через кнопку "💡 Инвестор") ---
async def handle_invest_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Пользователь нажал «💡 Инвестор», бот ждёт текст с запросом.
    Здесь просто проксируем в существующий пайплайн handle_strategy_text,
    чтобы не дублировать логику формирования стратегии.
    """
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text(
            "✍️ Напиши, какую стратегию тебе нужна.\n"
            "Например: «консервативный портфель на 3 года» или «куда вложить $5000 на 6 месяцев с высоким риском»."
        )
        return

    try:
        await handle_strategy_text(update, context)  # используем уже готовый обработчик
    finally:
        # в любом случае снимаем флаг режима
        context.user_data.pop("awaiting_invest_question", None)


async def help_invest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # ✅ Проверка доступа через кеш, подтягивающий Google Sheets
    if user_id not in get_allowed_users():
        await update.message.reply_text(
            f"🔒 Доступ только после активации: ${MONTHLY_PRICE_USD}/мес или ${LIFETIME_PRICE_USD} навсегда.",
            reply_markup=REPLY_MARKUP
        )
        return

    # 🧹 Чистим состояние и включаем режим вопроса по инвестам
    context.user_data.clear()
    context.user_data["awaiting_invest_question"] = True

    await update.message.reply_text(
        "💡 Напиши, какую стратегию тебе нужна.\n"
        "Примеры: «консервативный портфель на 3 года», "
        "«куда вложить $5000 с высоким риском на 6 месяцев», "
        "«сделай план усреднений по BTC и ETH».",
        reply_markup=ReplyKeyboardMarkup([["↩️ Выйти в меню"]], resize_keyboard=True)
    )
    return


async def handle_strategy_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Безопасное чтение текста
    msg = getattr(update, "message", None)
    user_text = ((msg.text if msg and msg.text else "")).strip()

    # 🚪 Выход по кнопке
    if user_text == "↩️ Выйти в меню":
        context.user_data.pop("awaiting_strategy", None)
        if msg:
            await msg.reply_text(
                "🔙 Ты вышел из режима стратегии. Возвращаемся в главное меню.",
                reply_markup=REPLY_MARKUP
            )
        return

    user_id = update.effective_user.id if update and update.effective_user else None

    # 📈 Получаем цену BTC и ETH без блокировки event loop
    btc_price = eth_price = None
    try:
        # Предпочитаем существующую в проекте функцию fetch_price_from_binance (не ломаем контракт)
        async def _fetch(symbol: str):
            try:
                return await asyncio.to_thread(fetch_price_from_binance, symbol)
            except NameError:
                # Fallback: если fetch_price_from_binance отсутствует, безопасно уходим в thread pool
                import requests
                def _rq(sym: str):
                    url = f"https://api.binance.com/api/v3/ticker/price?symbol={sym}USDT"
                    return float(requests.get(url, timeout=10).json()["price"])
                return await asyncio.to_thread(_rq, symbol)

        btc_price, eth_price = await asyncio.gather(_fetch("BTC"), _fetch("ETH"))
    except Exception as e:
        logging.error(f"[handle_strategy_text] Binance price fetch error: {e}")
        btc_price = eth_price = None

    # 🧠 Промпт для GPT (сохранён; не меняем смысл и структуру)
    prompt = (
        "You are a top-tier investment strategist with over 20 years of experience in multi-asset portfolio management. "
        "You specialize in creating fully personalized investment strategies specifically for Russian-speaking clients. "
        "Your strategies are simple, clear, beginner-friendly, and always explained with warmth, care, and confidence.\n\n"

        "📌 You are only allowed to recommend the following asset classes:\n"
        "- Cryptocurrencies: BTC, ETH, USDT\n"
        "- Forex pairs: EUR/USD, GBP/USD, etc.\n"
        "- Moscow Exchange instruments: Russian stocks, OFZ bonds, FinEx ETFs\n"
        "- Gold/silver only via MOEX futures or unallocated metal accounts (ОМС)\n\n"
        "🚫 DO NOT mention Eurobonds, foreign brokers, or international ETFs — strictly forbidden.\n\n"

        f"🧑‍💬 The client's question or investment goal is:\n{user_text}\n\n"

        "💰 Current market context:\n"
        f"{('- BTC: $' + str(btc_price)) if btc_price is not None else ''}\n"
        f"{('- ETH: $' + str(eth_price)) if eth_price is not None else ''}\n\n"

        "🎯 Your task:\n"
        "Craft a full, step-by-step, deeply personalized investment strategy that feels like a private consultation. "
        "Make it friendly, structured, easy to read, and 100% suitable for Telegram.\n\n"

        "⚠️ MANDATORY RULES:\n"
        "- Answer strictly in Russian — no English words without explanation.\n"
        "- No markdown (no **bold**, _italics_, etc.)\n"
        "- Each section must be clearly separated with emojis and headers.\n"
        "- Use short paragraphs (1–3 sentences max) for readability.\n"
        "- Beginner-friendly and emotionally supportive.\n\n"

        "📦 REQUIRED FORMAT:\n\n"

        "1️⃣ 👤 Профиль инвестора\n"
        "- Оцени риск-профиль и горизонт инвестора.\n"
        "- Определи его цель: накопление, сохранение капитала или пассивный доход.\n\n"

        "2️⃣ 📊 Рекомендуемый портфель\n"
        "- Распредели активы между криптой, Forex, MOEX и металлами.\n"
        "- Для каждого класса укажи причины включения.\n\n"

        "3️⃣ 🛡️ Управление рисками\n"
        "- Объясни принципы размера позиций, усреднения, фиксации прибыли и стопов.\n\n"

        "4️⃣ 🌐 Защита от рыночных рисков\n"
        "- Опиши риски и как портфельная структура их снижает.\n\n"

        "5️⃣ 🚀 План действий\n"
        "- Конкретные шаги прямо сейчас: где открыть счёт, с чего начать.\n\n"

        "6️⃣ 📈📉 Сценарии рынка\n"
        "- Дай план на случай роста и падения.\n\n"

        "7️⃣ ✅ Заключение\n"
        "- 2–3 тёплые строки поддержки с эмодзи.\n\n"

        "🧠 Всегда говори так, будто клиент — новичок, который доверяет тебе. "
        "Твоя задача — вдохновить, направить и защитить его капитал.\n"
    )

    try:
        gpt_response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1000
        )

        analysis = (gpt_response.choices[0].message.content or "").strip()
        if not analysis:
            if msg:
                await msg.reply_text(
                    "⚠️ GPT не дал ответа. Попробуй задать вопрос ещё раз.",
                    reply_markup=ReplyKeyboardMarkup([["↩️ Выйти в меню"]], resize_keyboard=True)
                )
            return

        if msg:
            await msg.reply_text(
                f"📈 Вот твоя персональная стратегия:\n\n{analysis}",
                reply_markup=ReplyKeyboardMarkup([["↩️ Выйти в меню"]], resize_keyboard=True)
            )

    except Exception as e:
        logging.error(f"[handle_strategy_text] GPT error: {e}")
        if msg:
            await msg.reply_text(
                "⚠️ GPT временно недоступен. Попробуй позже.",
                reply_markup=ReplyKeyboardMarkup([["↩️ Выйти в меню"]], resize_keyboard=True)
            )

    # Сброс локальных флагов (сохраняем прежнее поведение)
    context.user_data.clear()

# --- UID SUBMISSION (реферал через брокера) ---
async def handle_uid_submission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Пользователь выбрал брокера по кнопке и прислал UID для проверки.
    Записываем заявку в таблицу и подтверждаем приём.
    """
    uid = (update.message.text or "").strip()
    if not uid.isdigit():
        await update.message.reply_text("❗️ Пришли, пожалуйста, UID цифрами. Пример: 12345678.")
        return

    user_id = update.effective_user.id
    username = update.effective_user.username or "no_username"
    ref_program = context.user_data.get("ref_program", "broker_ref")
    broker = context.user_data.get("broker", "unknown")

    # Пишем в таблицу безопасным способом (без rate‑limit проблем)
    try:
        from datetime import datetime  # на случай, если не импортирован наверху
        row = [str(user_id), username, datetime.now().strftime("%Y-%m-%d %H:%M"), ref_program, broker, uid]
        await asyncio.to_thread(safe_append_row, row)
        logging.info(f"[REF_UID] {user_id=} {username=} {broker=} {uid=}")
        await update.message.reply_text(
            "✅ UID принят. Проверка займёт до 10 минут. Я отпишусь, как только доступ будет активирован.",
            reply_markup=REPLY_MARKUP
        )
    except Exception as e:
        logging.error(f"[handle_uid_submission] Google Sheets error: {e}")
        await update.message.reply_text(
            "⚠️ Не удалось зафиксировать UID. Попробуй ещё раз позже или напиши менеджеру @zhbankov_alex.",
            reply_markup=REPLY_MARKUP
        )

    # Снимаем флаг ожидания UID
    context.user_data.pop("awaiting_uid", None)


async def handle_calendar_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    photo = update.message.photo[-1]
    file = await photo.get_file()
    image_bytes = await file.download_as_bytearray()

    image = Image.open(BytesIO(image_bytes)).convert("RGB")
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=80)
    image_base64 = base64.b64encode(buffer.getvalue()).decode()

    await update.message.reply_text("🔎 Распознаю значения и формирую интерпретацию...")

    result = await generate_news_from_image(image_base64)

    if result:
        await update.message.reply_text(f"📈 Интерпретация по скриншоту:\n\n{result}", reply_markup=ReplyKeyboardMarkup([["↩️ Выйти в меню"]], resize_keyboard=True))
    else:
        await update.message.reply_text("⚠️ Не удалось распознать данные. Попробуйте загрузить более чёткий скрин.", reply_markup=ReplyKeyboardMarkup([["↩️ Выйти в меню"]], resize_keyboard=True))

async def generate_news_from_image(image_base64: str) -> str:
    prompt = (
        "Act as a world-class macroeconomic strategist with 20+ years of experience advising hedge funds, prop trading desks, and crypto funds. "
        "You specialize in interpreting economic calendar data, surprises in forecasts, and macro releases to assess their short-term market impact.\n\n"
        "You are analyzing a screenshot from an economic calendar (such as 'Initial Jobless Claims', 'CPI', etc). Extract from the image:\n"
        "- Event\n- Fact\n- Forecast\n- Previous\n\n"
        "Then give a professional, concise macroeconomic interpretation.\n\n"
        "🎯 Your response must be written STRICTLY in Russian, without using markdown symbols (*, _, -).\n\n"
        "📐 Structure your analysis as follows:\n\n"
        "1️⃣ Фундаментальная интерпретация события:\n"
        "2️⃣ Влияние на ликвидность, волатильность и поведение участников:\n"
        "3️⃣ Возможные сценарии:\n"
        "➡️ Bullish —\n"
        "➡️ Bearish —\n"
        "4️⃣ Историческая аналогия:\n\n"
        "🚫 Do NOT give trade entries, SL, or TP levels. Focus only on macro reasoning, narrative shifts, and positioning logic.\n"
        "Use short paragraphs. Be direct, sharp, and professional. Absolutely no markdown."
    )

    try:
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}}
                ]}
            ]
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"[generate_news_from_image error] {e}")
        return None

async def handle_definition_term(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()

    # 🚪 Выход по кнопке
    if user_text == "↩️ Выйти в меню":
        context.user_data.pop("awaiting_definition_term", None)
        await update.message.reply_text(
            "🔙 Ты вышел из режима терминов. Возвращаемся в главное меню.",
            reply_markup=REPLY_MARKUP
        )
        return

    term = user_text

    prompt = (
        f"You are a professional trader and educator with over 10 years of experience.\n\n"
        f"Explain in very simple terms what '{term}' means, as if teaching someone who is a complete beginner with zero trading experience.\n\n"
        "- Provide a short, clear definition in one or two sentences.\n"
        "- Then give a simple analogy (like comparing to a store, sports, or everyday life) so the concept becomes intuitive.\n"
        "- Finally, give a concrete example from trading practice where this term is used.\n\n"
        "Avoid unnecessary fluff and do not use professional jargon without immediately explaining it.\n"
        "Answer strictly in Russian."
    )

    try:
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}]
        )

        reply_markup = ReplyKeyboardMarkup([["↩️ Выйти в меню"]], resize_keyboard=True)

        text = response.choices[0].message.content.strip()
        if not text:
            await update.message.reply_text(
                "⚠️ GPT не дал ответа. Попробуй задать термин ещё раз.",
                reply_markup=reply_markup
            )
            return

        await update.message.reply_text(
            f"📘 Определение:\n\n{text}",
            reply_markup=reply_markup
        )

    except Exception as e:
        logging.error(f"[DEFINITION] GPT error: {e}")
        await update.message.reply_text(
            "⚠️ GPT временно недоступен. Попробуй позже.",
            reply_markup=ReplyKeyboardMarkup([["↩️ Выйти в меню"]], resize_keyboard=True)
        )

async def handle_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    text = (msg.text or "").strip()
    user_id = update.effective_user.id if update and update.effective_user else None

    logging.info(f"[handle_main] Пользователь {user_id} нажал кнопку: {text}")

    # 🚪 Проверка доступа (кеш из Google Sheets).
    # Разрешаем без подписки: «Купить», «О боте», «Бесплатный доступ через брокера», «Криптообмен».
    free_paths = {"💰 Купить", "ℹ️ О боте", "🔗 Бесплатный доступ через брокера", "💸 Криптообмен"}
    if user_id not in get_allowed_users() and text not in free_paths:
        await msg.reply_text(
            f"🔒 Доступ только после активации: ${MONTHLY_PRICE_USD}/мес или ${LIFETIME_PRICE_USD}. Либо через брокера.",
            reply_markup=REPLY_MARKUP
        )
        return

    # 💡 Инвестор (выбор формата)
    if text == "💡 Инвестор":
        context.user_data.clear()
        # 👇 включаем дефолтный «инвест-режим по фото», чтобы скрин сразу ушёл в handle_strategy_photo
        context.user_data["awaiting_strategy"] = "photo"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✍️ Написать текст", callback_data="strategy_text")],
            [InlineKeyboardButton("📸 Отправить скрин", callback_data="strategy_photo")],
        ])
        await msg.reply_text("👇 Выберите формат стратегии:", reply_markup=keyboard)
        return

    # 🎯 Калькулятор риска (fallback-вход; основной вход — через ConversationHandler)
    if text == "🎯 Калькулятор":
        return await start_risk_calc(update, context)

    # 🌱 Психолог
    if text == "🌱 Психолог":
        return await start_therapy(update, context)

    # 🔍 Новости / 🔎 Анализ (интерпретация скрина календаря)
    if text in ("🔍 Новости", "🔎 Анализ"):
        context.user_data.clear()
        context.user_data["awaiting_calendar_photo"] = True
        await msg.reply_text(
            "📸 Пришли скриншот из экономического календаря. Я распознаю событие и дам интерпретацию.",
            reply_markup=ReplyKeyboardMarkup([["↩️ Выйти в меню"]], resize_keyboard=True)
        )
        return

    # 📚 Термин
    if text == "📚 Термин":
        context.user_data.clear()
        context.user_data["awaiting_definition_term"] = True
        await msg.reply_text(
            "✍️ Напиши термин, который нужно объяснить.",
            reply_markup=ReplyKeyboardMarkup([["↩️ Выйти в меню"]], resize_keyboard=True)
        )
        return

    # 🚀 Трейдер (выбор рынка)
    if text == "🚀 Трейдер":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💎 Crypto", callback_data="market_crypto")],
            [InlineKeyboardButton("💱 Forex", callback_data="market_forex")],
        ])
        await msg.reply_text("⚡ Для какого рынка сделать анализ?", reply_markup=keyboard)
        return

    # 💸 Криптообмен (разрешено без подписки)
    if text == "💸 Криптообмен":
        await msg.reply_text(
            "💸 Криптообмен — быстро, безопасно и без лишних вопросов\n\n"
            "🔹 Работаем официально и в рамках закона\n"
            "🔹 17 регионов РФ — удобно и близко к вам\n"
            "🔹 Только проверенные и «чистые» средства\n"
            "🔹 Любые суммы — от частных до оптовых сделок\n\n"
            "💼 Преимущества для вас:\n"
            "✅ Без скрытых комиссий и переплат\n"
            "🚀 Мгновенные расчёты\n"
            "💰 Выдача наличными или перевод на счёт\n"
            "🛡 Полная конфиденциальность\n\n"
            "📩 Для обмена напиши прямо сейчас 👉 @zhbankov_alex",
            reply_markup=ReplyKeyboardMarkup([["↩️ Вернуться в меню"]], resize_keyboard=True)
        )
        return

    # 💰 Купить
    if text == "💰 Купить":
        if user_id in get_allowed_users():
            await msg.reply_text("✅ У тебя уже активирована подписка!", reply_markup=REPLY_MARKUP)
        else:
            await send_payment_link(update, context)
        return

    # ℹ️ О боте
    if text == "ℹ️ О боте":
        await msg.reply_text(
            "🤖 GPT-Трейдер — ИИ-ассистент в Telegram для крипты и форекса.\n\n"
            "Что умеет:\n"
            "• По скрину графика за 10 сек: Entry / Stop / TakeProfit\n"
            "• Инвест-план: покупка, уровни усреднений (DCA), цели и риски\n"
            "• Макро-интерпретация новостей (календарь, CPI, ФРС и др.)\n"
            "• Обучение простым языком и словарь терминов\n"
            "• Психолог для трейдера и калькулятор риска\n\n"
            "Как начать:\n"
            "1) Нажми «💰 Купить» и активируй доступ.\n"
            "2) Пришли скрин — получи уровни и план.\n"
            "3) Проверь размер позиции через «🎯 Калькулятор».\n\n"
            f"Доступ: ${MONTHLY_PRICE_USD}/мес или ${LIFETIME_PRICE_USD} навсегда (USDT TRC20 через CryptoCloud).\n"
            "Альтернатива: бесплатный доступ через регистрацию у брокера — «🔗 Бесплатный доступ через брокера».\n\n"
            "Важно: информация носит образовательный характер и не является инвестиционной рекомендацией.",
            reply_markup=REPLY_MARKUP
        )
        return

    # 🔗 Бесплатный доступ через брокера
    if text == "🔗 Бесплатный доступ через брокера":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Bybit", callback_data="ref_bybit")],
            [InlineKeyboardButton("Forex4You", callback_data="ref_forex4you")],
        ])
        await msg.reply_text(
            "🚀 Выберите брокера для регистрации по моей реферальной ссылке:\n"
            "- Для Bybit минимальный депозит $150\n"
            "- Для Forex4You минимальный депозит $200\n\n"
            "После регистрации пришлите сюда свой UID для проверки.",
            reply_markup=keyboard
        )
        return

    # 📌 Сетап (для админа)
    if text == "📌 Сетап":
        if user_id not in ADMIN_IDS:
            await msg.reply_text("⛔️ Эта функция доступна только админу.")
            return
        await msg.reply_text("✍️ Укажи торговый инструмент (например: BTC/USDT):")
        return SETUP_1

    # ✅ Открытые диалоги (продолжаем, если есть ожидания)
    if context.user_data.get("awaiting_invest_question"):
        return await handle_invest_question(update, context)
    if context.user_data.get("awaiting_definition_term"):
        return await handle_definition_term(update, context)
    if context.user_data.get("awaiting_therapy_input"):
        return await gpt_psychologist_response(update, context)
    if context.user_data.get("awaiting_uid"):
        return await handle_uid_submission(update, context)

    # ↩️ Универсальный выход
    if text in ("↩️ Вернуться в меню", "↩️ Выйти в меню"):
        context.user_data.clear()
        await msg.reply_text("🔙 Вернулись в главное меню.", reply_markup=REPLY_MARKUP)
        return

    # 🔄 Если ничего не ожидаем — мягкий сброс
    saved = {k: v for k, v in context.user_data.items() if k in ("selected_market", "selected_strategy")}
    context.user_data.clear()
    context.user_data.update(saved)
    await msg.reply_text("🔄 Сброс всех ожиданий. Продолжай.", reply_markup=REPLY_MARKUP)

async def start_therapy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Устанавливаем флаг, чтобы handle_main понимал, что активен психолог
    context.user_data["awaiting_therapy_input"] = True

    therapy_keyboard = [["↩️ Выйти в меню"]]
    reply_markup = ReplyKeyboardMarkup(therapy_keyboard, resize_keyboard=True)

    await update.message.reply_text(
        "😵‍💫 Ну что, опять рынок побрил как барбер в пятницу? Бывает, друг.\n\n"
        "Напиши, что случилось — GPT-психолог с доброй иронией выслушает, подбодрит и вставит мем.\n\n"
        "Когда захочешь вернуться к аналитике — просто нажми «↩️ Выйти в меню».",
        reply_markup=reply_markup
    )

async def gpt_psychologist_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()

    # Обработка выхода в меню
    if user_text == "↩️ Выйти в меню":
        context.user_data.pop("awaiting_therapy_input", None)
        await update.message.reply_text(
            "🔁 Возвращаемся в главное меню!",
            reply_markup=REPLY_MARKUP
        )
        return

    prompt = (
        "You are a GPT-psychologist for traders. "
        "You respond with warm irony and light humor, helping them cope with gambling addiction tendencies, losing streaks, and emotional swings. "
        "Avoid gender-specific words like 'bro' or 'girl', use neutral terms such as 'friend', 'colleague', or 'trader'.\n\n"
        f"User's message:\n{user_text}\n\n"
        "📌 Follow this exact structure:\n\n"
        "1️⃣ React empathetically, but without pity. Show you understand the feeling of losses.\n\n"
        "2️⃣ Provide a metaphor to help the trader realize that a drawdown isn't the end. "
        "For example: 'it's like pulling back a slingshot before it fires.'\n\n"
        "3️⃣ Give a fact or story showing that even top traders have losing streaks (like Soros or Druckenmiller). "
        "This builds confidence that everyone experiences losses.\n\n"
        "4️⃣ Suggest one simple micro-action to feel in control right now, like closing the terminal, journaling emotions, or stepping outside.\n\n"
        "5️⃣ Finish with a trading meme or funny short quote, e.g.: '— Are you holding a position? — No, I'm holding back tears 😭.'\n\n"
        "⚠️ Avoid generic phrases like 'don't worry' or 'everything will be fine'. Be specific, warm, and slightly ironic.\n"
        "Answer everything strictly in Russian."
    )

    try:
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}]
        )

        reply_markup = ReplyKeyboardMarkup([["↩️ Выйти в меню"]], resize_keyboard=True)

        await update.message.reply_text(
            f"🧘 GPT-психолог:\n{response.choices[0].message.content.strip()}",
            reply_markup=reply_markup
        )

    except Exception as e:
        logging.error(f"[GPT_PSYCHOLOGIST] Ошибка при ответе: {e}")
        await update.message.reply_text(
            "⚠️ Произошла ошибка. Попробуй ещё раз позже.",
            reply_markup=ReplyKeyboardMarkup([["↩️ Выйти в меню"]], resize_keyboard=True)
        )

def extract_tx_id(d: dict) -> str:
    """Пытаемся достать идентификатор транзакции из разных возможных ключей IPN."""
    if not isinstance(d, dict):
        return ""
    # 1) Прямые ключи
    for k in ("tx_id", "txid", "txn_id", "tx_hash", "hash", "transaction_id", "payment_id", "id"):
        v = d.get(k)
        if v:
            return str(v)

    # 2) Частые вложенные контейнеры
    for container in ("transaction", "payment"):
        sub = d.get(container)
        if isinstance(sub, dict):
            for k in ("id", "tx_id", "txid", "hash"):
                v = sub.get(k)
                if v:
                    return str(v)

    return ""


def parse_order_id(raw: str) -> tuple[int | None, str, str]:
    """
    Поддерживаем форматы:
      user_{user_id}_{username}_{plan}
      user_{user_id}_{plan}
      user_{user_id}
    Возвращаем (user_id, username, plan)
    """
    if not isinstance(raw, str) or not raw.startswith("user_"):
        raise ValueError(f"Unexpected order_id prefix: {raw}")

    rest = raw[len("user_"):]
    # отделяем user_id
    if "_" in rest:
        uid_str, remainder = rest.split("_", 1)
    else:
        uid_str, remainder = rest, ""

    try:
        user_id = int(uid_str)
    except Exception as e:
        raise ValueError(f"Bad user_id in order_id: {uid_str}") from e

    username = ""
    plan = "unknown"

    if remainder:
        # если есть и username, и plan — забираем план как последний сегмент
        if "_" in remainder:
            username, plan = remainder.rsplit("_", 1)
        else:
            username, plan = "", remainder

    username = (username or "").lstrip("@").strip()
    plan = (plan or "unknown").strip().lower()
    if plan not in {"monthly", "lifetime"}:
        plan = "unknown"

    return user_id, username, plan


def validate_payment_fields(data: dict, plan: str) -> tuple[bool, str, Decimal, str, str]:
    """
    Жёсткая проверка суммы/валюты/сети по выбранному плану.
    Возвращает кортеж: (ok, reason, amount, currency, network_norm)

    Правила:
    - Сумма: строго равна ожидаемой по плану (с точностью до 0.01).
    - Валюта: строго равна PAY_CURRENCY (без учёта регистра).
    - Сеть: если провайдер прислал, сверяем после нормализации (TRC20≡TRON, BEP20≡BSC).
            Если сеть не прислана, проверку сети пропускаем.
    """
    # 1) Ожидаемая сумма по плану
    plan_map = {
        "monthly": Decimal(str(MONTHLY_PRICE_USD)),
        "lifetime": Decimal(str(LIFETIME_PRICE_USD)),
    }
    expected = plan_map.get(plan)
    if expected is None:
        return False, "unknown plan", Decimal(0), "", ""

    # 2) Сумма (может прийти числом/строкой/с запятой)
    raw_amount = data.get("amount") if isinstance(data, dict) else None
    if raw_amount is None:
        return False, "missing amount", Decimal(0), "", ""
    try:
        # допускаем запятую как десятичный разделитель
        amount = Decimal(str(raw_amount).replace(",", ".")).quantize(Decimal("0.01"))
    except InvalidOperation:
        return False, f"bad amount: {raw_amount}", Decimal(0), "", ""

    # 3) Валюта/сеть
    currency = (data.get("currency") or "").strip().upper()
    network_raw = (data.get("network") or data.get("chain") or "").strip().upper()

    # нормализация сетей
    aliases = {
        "TRC20": "TRON", "TRON": "TRON",
        "BEP20": "BSC",  "BSC": "BSC",
        "ERC20": "ERC20",
        "TON": "TON",
    }
    network_norm = aliases.get(network_raw, network_raw)

    # 4) Ожидаемые из конфигурации (могут быть пустыми/None)
    pay_curr = (PAY_CURRENCY or "").strip().upper()
    pay_net = (PAY_NETWORK or "").strip().upper()
    pay_net_norm = aliases.get(pay_net, pay_net)

    # 5) Строгие сравнения
    if amount != expected:
        return False, f"amount mismatch {amount} != {expected}", amount, currency, network_norm

    if pay_curr and currency != pay_curr:
        return False, f"currency mismatch {currency} != {PAY_CURRENCY}", amount, currency, network_norm

    # Если провайдер прислал network И у нас задана ожидаемая сеть — сверяем после нормализации
    if network_norm and pay_net_norm and network_norm != pay_net_norm:
        return False, f"network mismatch {network_norm} != {PAY_NETWORK}", amount, currency, network_norm

    return True, "ok", amount, currency, network_norm


# ✅ Webhook от CryptoCloud
@app_flask.route("/cryptocloud_webhook", methods=["POST"])
def cryptocloud_webhook():
    body = request.get_data()  # bytes
    signature_hdr = (request.headers.get("X-Signature-SHA256") or "").strip().lower()
    calc_sig = hmac.new(API_SECRET.encode(), body, hashlib.sha256).hexdigest().lower()

    # Безопасное сравнение подписи
    if not hmac.compare_digest(signature_hdr, calc_sig):
        logging.warning("⚠ Неверная подпись IPN")
        return jsonify({"status": "invalid signature"}), 400

    data = request.json or {}
    if not isinstance(data, dict):
        logging.warning("⚠ Некорректное тело IPN (не dict)")
        return jsonify({"status": "bad payload"}), 400

    status = str(data.get("status") or "").lower()
    raw_order_id = (data.get("order_id") or "").strip()
    tx_id = extract_tx_id(data)

    # Логируем основные поля (без чувствительных данных)
    logging.info(
        "✅ IPN: status=%s, order_id='%s', tx_id='%s', amount='%s', currency='%s', network='%s'",
        status,
        raw_order_id,
        tx_id,
        data.get("amount"),
        data.get("currency"),
        (data.get("network") or data.get("chain")),
    )

    # Принимаем только успешные платежи
    if status != "paid":
        return jsonify({"status": "ignored (not paid)"}), 200

    if not raw_order_id:
        return jsonify({"status": "missing order_id"}), 400

    # Парсим order_id → (user_id, username, plan)
    try:
        user_id, username, plan = parse_order_id(raw_order_id)
    except Exception as e:
        logging.error("❌ Ошибка парсинга order_id='%s': %s", raw_order_id, e)
        return jsonify({"status": "bad order_id"}), 400

    # Идемпотентность с TTL: не обрабатываем повторно одну и ту же транзакцию/платёж
    unique_key = tx_id or f"{raw_order_id}:{data.get('amount')}:{data.get('currency')}"
    now = time.time()
    # Очистка старых ключей
    for k, ts in list(PROCESSED_PAYMENTS.items()):
        if now - ts > PROCESSED_TTL_SEC:
            PROCESSED_PAYMENTS.pop(k, None)
    if unique_key in PROCESSED_PAYMENTS:
        logging.info("♻️ Повторная доставка IPN, пропускаем. key='%s'", unique_key)
        return jsonify({"status": "duplicate ignored"}), 200
    PROCESSED_PAYMENTS[unique_key] = now

    # Жёсткая валидация суммы/валюты/сети (с нормализацией сетей внутри)
    ok, reason, amount, currency, network = validate_payment_fields(data, plan)
    if not ok:
        logging.error("⛔ Валидация не пройдена: %s. plan=%s, tx_id='%s'", reason, plan, tx_id)
        return jsonify({"status": "validation failed", "reason": reason}), 400

    # Активируем доступ локально + асинхронно логируем в Google Sheets
    try:
        ALLOWED_USERS.add(user_id)
        # продлеваем TTL кеша, чтобы не перезатёрся до фонового обновления
        global ALLOWED_USERS_TIMESTAMP
        ALLOWED_USERS_TIMESTAMP = time.time()

        # Планируем запись в Sheets на основном loop (через тред-пул)
        loop = getattr(app_flask, "loop", None) or asyncio.get_event_loop()
        asyncio.run_coroutine_threadsafe(
            asyncio.to_thread(log_payment, user_id, username),
            loop
        )
    except Exception as e:
        logging.error("❌ Ошибка постановки записи в Google Sheets: %s", e)

    # Уведомление пользователю — асинхронно в loop бота
    try:
        loop = getattr(app_flask, "loop", None) or asyncio.get_event_loop()
        asyncio.run_coroutine_threadsafe(
            notify_user_payment(user_id),
            loop
        )
    except Exception as e:
        logging.error("❌ Не удалось запланировать уведомление %s: %s", user_id, e)

    logging.info(
        "🎉 Оплата подтверждена: user_id=%s, plan=%s, amount=%s %s%s, tx_id='%s'",
        user_id,
        plan,
        amount,
        currency,
        ("/" + network) if network else "",
        tx_id
    )

    return jsonify({"ok": True}), 200

def sanitize_username(u: str | None) -> str:
    if not u:
        return "nouser"
    # оставляем только [A-Za-z0-9_], режем до 32 символов
    return re.sub(r"[^\w]+", "", u)[:32]

# 🚀 Функция генерации ссылок POS: месяц и навсегда (с username в order_id)
async def send_payment_link(update, context):
    user_id = update.effective_user.id
    uname = sanitize_username(update.effective_user.username)

    monthly_qs = urlencode({
        "amount": MONTHLY_PRICE_USD,
        "currency": PAY_CURRENCY,
        "network": PAY_NETWORK,
        "order_id": f"user_{user_id}_{uname}_monthly",
        "desc": "GPT_Trader_Monthly"
    })
    lifetime_qs = urlencode({
        "amount": LIFETIME_PRICE_USD,
        "currency": PAY_CURRENCY,
        "network": PAY_NETWORK,
        "order_id": f"user_{user_id}_{uname}_lifetime",
        "desc": "GPT_Trader_Lifetime"
    })

    monthly_link  = f"https://pay.cryptocloud.plus/pos/{CRYPTOCLOUD_SHOP_ID}?{monthly_qs}"
    lifetime_link = f"https://pay.cryptocloud.plus/pos/{CRYPTOCLOUD_SHOP_ID}?{lifetime_qs}"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"💳 Оплатить ${MONTHLY_PRICE_USD}/мес", url=monthly_link)],
        [InlineKeyboardButton(f"🏆 Разово ${LIFETIME_PRICE_USD} навсегда", url=lifetime_link)]
    ])
    await update.message.reply_text("💵 Выбери вариант доступа к GPT‑Трейдеру:", reply_markup=keyboard)

# 🚀 Запуск Flask в отдельном потоке с loop
def run_flask(loop):
    app_flask.loop = loop
    port = int(os.environ.get("PORT", 5000))
    print(f"[render-port] Server bound to PORT={port}")
    app_flask.run(host="0.0.0.0", port=port)

# 👇 ВСТАВЬ ЗДЕСЬ:
ADMIN_IDS = {407721399}  # замени на свой user_id

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PHOTO_PATH = os.path.join(BASE_DIR, "GPT-Трейдер помощник.png")

async def publish_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔️ У тебя нет прав на публикацию.")
        return

    # используем общую ссылку на бота из константы, если есть
    bot_url = globals().get("BOT_URL", "https://t.me/CtyptorobBot")

    caption = (
        "🚀 *ТВХ (Твоя точка входа)* — экосистема трейдинга: 🤖 GPT-бот, 📢 публичный канал, 💬 чат с топиками и 🔒 VIP-сигналы.\n\n"
        "📊 Что даёт бот ТВХ:\n"
        "• Прогноз по скрину за 10 секунд\n"
        "• Чёткие уровни: вход, стоп, тейки\n"
        "• Рынки: Crypto, Forex и MOEX\n"
        "• Анализ новостей (ФРС, ETF, хардфорки, макро)\n"
        "• Поддержка GPT-психолога 😅\n\n"
        "📰 Плюс: ссылки на проверенные источники — без шума, лудоманов и инфоцыган\n"
        "⚡️ Премиум: авторские скальперские сетапы + «люксовые» сигналы ИИ (с PRO TradingView)\n\n"
        f"🔥 Подключи ТВХ — всего ${MONTHLY_PRICE_USD}/мес или ${LIFETIME_PRICE_USD} навсегда.\n\n"
        "👥 Чат трейдеров 👉 [TBX Chat](https://t.me/+yUYqG8JuwuZiZmUy)\n"
        "💬 Вопросы 👉 [@zhbankov_alex](https://t.me/zhbankov_alex)\n\n"
        "✨ И это только начало. Мы с ботом будем каждый день становиться лучше, чтобы ты рос вместе с комьюнити. "
        "ТВХ — это твоя точка входа и твоя поддержка. 🚀"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Получить доступ", url=bot_url)]
    ])

    try:
        chat_id = "@TBXtrade"

        # убираем старый закреп, если есть
        chat_obj = await context.bot.get_chat(chat_id)
        if getattr(chat_obj, "pinned_message", None):
            await context.bot.unpin_chat_message(
                chat_id=chat_id,
                message_id=chat_obj.pinned_message.message_id
            )

        # публикуем одну и ту же анимацию, что и в /start; при ошибке — фото
        try:
            with open(VIDEO_PATH, "rb") as anim:
                message = await context.bot.send_animation(
                    chat_id=chat_id,
                    animation=anim,
                    caption=caption,
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )
        except Exception as e_anim:
            logging.warning(f"[publish_post] send_animation failed, fallback to photo. err={e_anim}")
            with open(PHOTO_PATH, "rb") as photo:
                message = await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=photo,
                    caption=caption,
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )

        # закрепляем пост
        await context.bot.pin_chat_message(
            chat_id=chat_id,
            message_id=message.message_id,
            disable_notification=True
        )

        await update.message.reply_text("✅ Пост опубликован и закреплён в канале.")
    except Exception as e:
        logging.error(f"[PUBLISH] Ошибка публикации: {e}")
        await update.message.reply_text("⚠️ Не удалось опубликовать или закрепить пост. Проверь файл, права и логи.")

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔️ Эта команда доступна только админу.")
        return

    args = context.args
    if not args:
        await update.message.reply_text("✍️ Используй так:\n/broadcast Текст рассылки для VIP подписчиков")
        return

    message_text = " ".join(args)
    success_count = 0
    failed_users = []

    for vip_id in ALLOWED_USERS:
        try:
            await context.bot.send_message(
                chat_id=vip_id,
                text=f"🚀 *VIP-обновление от трейдера:*\n\n{message_text}",
                parse_mode="Markdown"
            )
            success_count += 1
        except Exception as e:
            logging.error(f"[BROADCAST] Не удалось отправить VIP {vip_id}: {e}")
            failed_users.append(vip_id)

    await update.message.reply_text(
        f"✅ Рассылка завершена.\n"
        f"📬 Отправлено {success_count} пользователям.\n"
        f"{'⚠️ Ошибки у некоторых пользователей.' if failed_users else ''}"
    )

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Безопасно получаем message
    msg = getattr(update, "message", None)

    # Проверка прав
    user_id = update.effective_user.id if update and update.effective_user else None
    if user_id not in ADMIN_IDS:
        if msg:
            await msg.reply_text("⛔ Эта команда доступна только админу.")
        return

    try:
        # Чтение всех записей из Google Sheets без блокировки event loop
        records = await asyncio.to_thread(sheet.get_all_records)
        total_records = len(records)
        allowed_count = len(ALLOWED_USERS)

        last_entry = records[-1] if records else {}
        # Ограничим размер последней записи (на случай очень длинных значений)
        try:
            last_entry_str = json.dumps(last_entry, ensure_ascii=False, indent=2)
            if len(last_entry_str) > 3000:
                last_entry_str = last_entry_str[:3000] + "…"
        except Exception:
            last_entry_str = str(last_entry)[:3000] + "…"

        text = (
            "📊 Статистика:\n\n"
            f"• Подписчиков в ALLOWED_USERS: {allowed_count}\n"
            f"• Всего записей в Google Sheets: {total_records}\n\n"
            "📝 Последняя запись:\n"
            f"{last_entry_str}"
        )

        if msg:
            await msg.reply_text(text)

    except Exception as e:
        logging.error(f"[STATS] Ошибка: {e}")
        if msg:
            await msg.reply_text("⚠️ Не удалось получить статистику.")


async def export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Безопасно получаем message
    msg = getattr(update, "message", None)

    # Проверка прав
    user_id = update.effective_user.id if update and update.effective_user else None
    if user_id not in ADMIN_IDS:
        if msg:
            await msg.reply_text("⛔ Эта команда доступна только админу.")
        return

    try:
        # Чтение записей из Google Sheets без блокировки event loop
        records = await asyncio.to_thread(sheet.get_all_records)

        # Готовим CSV в памяти
        import csv
        from io import StringIO, BytesIO

        csv_text = StringIO()
        writer = csv.DictWriter(csv_text, fieldnames=["user_id", "username", "email", "date"])
        writer.writeheader()
        for row in records:
            writer.writerow({
                "user_id": row.get("user_id", ""),
                "username": row.get("username", ""),
                "email": row.get("email", ""),
                "date": row.get("date", ""),
            })

        # В PTB v21 корректно отдаём файл как BytesIO с именем
        data = csv_text.getvalue().encode("utf-8")
        bio = BytesIO(data)
        bio.name = "users_export.csv"

        if msg:
            await msg.reply_document(
                document=bio,
                caption="📥 Все пользователи и email из Google Sheets"
            )

    except Exception as e:
        logging.error(f"[EXPORT] Ошибка: {e}")
        if msg:
            await msg.reply_text("⚠️ Не удалось выгрузить пользователей.")


async def unified_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 🧾 Нормализуем вход
    msg = update.effective_message
    text = (getattr(msg, "text", "") or "").strip()

    # Фото или документ-картинка (PNG/JPG)
    doc = getattr(msg, "document", None)
    is_image_doc = bool(doc and (doc.mime_type or "").startswith("image/"))
    has_photo = bool(getattr(msg, "photo", None)) or is_image_doc

    # ↩️ Универсальный выход
    if text in ("↩️ Выйти в меню", "↩️ Вернуться в меню"):
        context.user_data.clear()
        await msg.reply_text("🔙 Вернулись в главное меню.", reply_markup=REPLY_MARKUP)
        return

    # 📨 Сбор email
    if context.user_data.get("awaiting_email"):
        if text and "@" in text and "." in text:
            try:
                await asyncio.to_thread(safe_append_row, [
                    str(update.effective_user.id),
                    update.effective_user.username or "",
                    text,
                ])
                await msg.reply_text("✅ Email сохранён! Бонус придёт в ближайшее время.")
            except Exception as e:
                logging.error(f"[EMAIL_SAVE] {e}")
                await msg.reply_text("⚠️ Не удалось сохранить. Попробуй позже.")
        else:
            await msg.reply_text("❌ Похоже, это не email. Попробуй снова.")
            return
        context.user_data.pop("awaiting_email", None)
        return

    # 🗓 Экономкалендарь — приоритетнее любых фото
    if context.user_data.get("awaiting_calendar_photo"):
        if has_photo:
            await handle_calendar_photo(update, context)
        else:
            await msg.reply_text("📸 Пришли скрин экономического календаря или нажми «↩️ Выйти в меню».")
        return

    # 💡 Инвест-стратегия: ТЕКСТ
    if context.user_data.get("awaiting_strategy") == "text":
        if text:
            await handle_strategy_text(update, context)
        else:
            await msg.reply_text("❌ Для текстовой стратегии нужно отправить текстовое сообщение.")
        return

    # 💡 Инвест-стратегия: СКРИН — должно идти ПЕРЕД общим разбором фото!
    if context.user_data.get("awaiting_strategy") == "photo":
        if has_photo:
            await handle_strategy_photo(update, context)   # инвесторский разбор
        else:
            await msg.reply_text("📸 Пришли скрин для инвест-стратегии или нажми «↩️ Выйти в меню».")
        return

    # 🖼 Если просто прислали фото/документ-картинку — трейдерский разбор
    if has_photo:
        await handle_photo(update, context)
        return

    # ✅ Остальные режимы (текст)
    if context.user_data.get("awaiting_potential"):
        context.user_data.pop("awaiting_potential", None)
        await msg.reply_text("⚠️ Этот режим временно недоступен. Возвращаю в меню.", reply_markup=REPLY_MARKUP)
        return

    if context.user_data.get("awaiting_definition_term"):
        await handle_definition_term(update, context); return

    if context.user_data.get("awaiting_invest_question"):
        await handle_invest_question(update, context); return
    if context.user_data.get("awaiting_uid"):
        await handle_uid_submission(update, context); return

    # Ничего не ожидаем — отдаём в главный роутер
    await handle_main(update, context)

async def restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("🔄 Бот перезапущен. Выбери действие:", reply_markup=REPLY_MARKUP)
    return ConversationHandler.END

async def post_init(app: Application) -> None:
    try:
        info = await app.bot.get_webhook_info()
        if info and info.url:
            await app.bot.delete_webhook(drop_pending_updates=True)
            logging.info(f"🔌 Webhook отключён: был установлен {info.url}")
        else:
            logging.info("🔌 Webhook не был установлен — переходим к polling.")
    except Exception as e:
        # даже если не удалось получить/снять webhook — не валим запуск
        logging.error(f"⚠️ Не удалось проверить/снять webhook: {e}")

def main():
    global global_bot, ALLOWED_USERS, ALLOWED_USERS_TIMESTAMP

    # 🔄 Кеш допуска при старте (не блокирует хендлеры)
    ALLOWED_USERS = load_allowed_users()
    ALLOWED_USERS_TIMESTAMP = time.time()
    logging.info(f"📥 ALLOWED_USERS загружен при старте: {len(ALLOWED_USERS)} пользователей")

    # ✅ Telegram-приложение (post_init снимет webhook, чтобы не было 409)
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    logging.info("🚀 GPT-Трейдер стартовал!")

    # ✅ Глобальный bot для уведомлений из вебхука
    global_bot = app.bot

    # 🚀 Общий asyncio-loop (его передаём во Flask-поток для run_coroutine_threadsafe)
    loop = asyncio.get_event_loop()

    # 🌐 Flask (CryptoCloud webhook) в отдельном демонизированном потоке
    svc_type = (os.getenv("RENDER_SERVICE_TYPE", "web") or "web").lower()
    if svc_type in ("web", "web_service", "webservice"):
        threading.Thread(target=run_flask, args=(loop,), daemon=True).start()
        logging.info("[render-port] Flask started (Web Service).")
    else:
        logging.info("[render-port] Worker mode detected — Flask server is not started.")

    # ✅ Глобальный error handler
    async def error_handler(update, context):
        logging.exception("❌ Unhandled exception in handler")
        try:
            msg = getattr(update, "message", None)
            if msg:
                await msg.reply_text("⚠️ Произошла внутренняя ошибка. Попробуйте позже.")
        except Exception:
            pass
    app.add_error_handler(error_handler)

    # 🔄 Еженедельная рассылка (по умолчанию: пн 12:00)
    CRON_TIME = os.getenv("CRON_TIME", "0 12 * * mon")

    @aiocron.crontab(CRON_TIME)
    async def weekly_broadcast():
        message_text = (
            "🚀 Еженедельный обзор:\n"
            "• BTC сейчас около $108,700 — зона интереса $108,000–109,000.\n"
            "• ETH держится на $2,576 — ищем покупки в диапазоне $2,520–2,600.\n"
            "• Стопы держи коротко, цели фиксируй по R:R ~2:1."
        )
        success, fails = 0, []
        for vip_id in get_allowed_users():
            try:
                await app.bot.send_message(chat_id=vip_id, text=message_text)
                success += 1
            except Exception as e:
                logging.error(f"[WEEKLY BROADCAST] {vip_id}: {e}")
                fails.append(vip_id)
        logging.info(f"✅ Рассылка завершена: {success} успехов, {len(fails)} ошибок.")

    # 🧘 GPT-Психолог (опциональный отдельный диалог)
    therapy_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🧘 Спокойствие$"), start_therapy)],
        states={
            WAITING_FOR_THERAPY_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, gpt_psychologist_response)
            ]
        },
        fallbacks=[
            CommandHandler("start", start, block=False),
            CommandHandler("restart", restart, block=False),
            MessageHandler(filters.Regex("^🔄 Перезапустить бота$"), restart),
        ],
    )

    # 📏 Калькулятор риска (вход и по кнопке, и по inline-колбэку)
    risk_calc_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(r"^📏 Калькулятор риска$|^🎯 Калькулятор$"), start_risk_calc),
            CallbackQueryHandler(start_risk_calc, pattern="^start_risk_calc$"),
        ],
        states={
            RISK_CALC_1: [MessageHandler(filters.TEXT & ~filters.COMMAND, risk_calc_deposit)],
            RISK_CALC_2: [MessageHandler(filters.TEXT & ~filters.COMMAND, risk_calc_risk_percent)],
            RISK_CALC_3: [MessageHandler(filters.TEXT & ~filters.COMMAND, risk_calc_stoploss)],
        },
        fallbacks=[
            CommandHandler("start", start, block=False),
            CommandHandler("restart", restart, block=False),
            MessageHandler(filters.Regex("^🔄 Перезапустить бота$"), restart),
        ],
    )

    # 📌 Сетап (многошаговый ввод)
    setup_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📌 Сетап$"), handle_main)],
        states={
            SETUP_1: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_instrument)],
            SETUP_2: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_risk_area)],
            SETUP_3: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_targets)],
            SETUP_4: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_stoploss)],
            SETUP_5: [MessageHandler(filters.PHOTO, setup_photo)],
        },
        fallbacks=[
            CommandHandler("start", start, block=False),
            CommandHandler("restart", restart, block=False),
            MessageHandler(filters.Regex("^🔄 Перезапустить бота$"), restart),
        ],
    )

    # ✅ Команды
    app.add_handler(CommandHandler("start", start, block=False))
    app.add_handler(CommandHandler("restart", restart, block=False))
    app.add_handler(CommandHandler("publish", publish_post, block=False))
    app.add_handler(CommandHandler("broadcast", broadcast, block=False))
    app.add_handler(CommandHandler("grant", grant, block=False))
    app.add_handler(CommandHandler("reload_users", reload_users, block=False))
    app.add_handler(CommandHandler("stats", stats, block=False))
    app.add_handler(CommandHandler("export", export, block=False))

    # ✅ Диалоги
    app.add_handler(therapy_handler)
    app.add_handler(risk_calc_handler)
    app.add_handler(setup_handler)

    # ✅ CallbackQuery и универсальный обработчик текста/фото/док-картинок
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.PHOTO | filters.Document.IMAGE) & ~filters.COMMAND,
            unified_text_handler
        )
    )

    # 🚀 Запуск polling (post_init уже снял webhook с drop_pending_updates=True)
    app.run_polling()

def log_payment(user_id, username):
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        safe_append_row([str(user_id), username, timestamp])
        logging.info(f"🧾 Записано в Google Sheets: {user_id}, {username}, {timestamp}")
    except Exception as e:
        logging.error(f"❌ Ошибка при записи в Google Sheets: {e}")

async def notify_user_payment(user_id):
    try:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚀 Перейти в меню", callback_data="start_menu")],
            [
                InlineKeyboardButton("📈 Получить сигнал", callback_data="back_to_signal"),
                InlineKeyboardButton("🧠 Инвест‑стратегия", callback_data="strategy_photo")
            ],
            [
                InlineKeyboardButton("📏 Калькулятор риска", callback_data="start_risk_calc"),
                InlineKeyboardButton("🔒 VIP‑канал", url="https://t.me/+your_invite_hash")
            ]
        ])

        await global_bot.send_message(
            chat_id=user_id,
            text=(
                "✅ Оплата получена! Подписка активирована 🎉\n\n"
                "Что дальше:\n"
                "1) Пришли скрин графика — найду Entry / Stop / TP за 10 секунд.\n"
                "2) Или загрузи скрин для инвест‑плана: покупка, усреднения (DCA) и цели.\n"
                "3) Проверь размер позиции через калькулятор риска.\n\n"
                "📢 Доступ к закрытому каналу с VIP‑сетапами уже открыт:\n"
                "👉 [Перейти в VIP‑канал](https://t.me/+TAbYnYSzHYI0YzVi)\n\n"
                "🎁 Бонус: курс по скальпингу и позиционке\n"
                "👉 [Открыть курс в Google Drive](https://drive.google.com/drive/folders/1EEryIr4RDtqM4WyiMTjVP1XiGYJVxktA?clckid=3f56c187)"
            ),
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        logging.info(f"📩 Уведомление отправлено пользователю {user_id}")
    except Exception as e:
        logging.error(f"❌ Не удалось уведомить пользователя {user_id}: {e}")

if __name__ == '__main__':
    main()









