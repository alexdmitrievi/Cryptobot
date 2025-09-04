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
import inspect
from pathlib import Path
from datetime import datetime
from urllib.parse import urlencode
from decimal import Decimal, InvalidOperation
from typing import Tuple, Optional, Dict, Any, List
from io import BytesIO  # для работы с изображениями в памяти
from dataclasses import dataclass, asdict

from bs4 import BeautifulSoup
from flask import Flask, request, jsonify

from telegram import (
    Update, BotCommand, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, ReplyKeyboardRemove,
    PhotoSize, Document,    # ⬅️ важно: используются в _extract_image_bytes
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters, ConversationHandler,
)
from telegram.ext import Application  # для аннотации в post_init

from openai import AsyncOpenAI
from PIL import Image  # для проверки/конвертации картинок

# Google Sheets
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# Cron и ретраи
import aiocron
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

# =====================[ CONSTANTS / GLOBALS ]=====================
# Scopes для Google Sheets
SCOPES = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

# Инициализация OpenAI-клиента (используется в ask_gpt_vision / handle_strategy_* и т.п.)
client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# Глобальный bot для уведомлений из вебхуков (инициализируй в main())
global_bot = None

# Абсолютные пути к файлам (безопаснее для Render)
BASE_DIR = Path(__file__).resolve().parent

PHOTO_PATH = BASE_DIR / "banner.jpg"          # баннер
VIDEO_PATH = BASE_DIR / "Video_TBX.mp4"       # тестовое/другое видео
POST_VIDEO_PATH = BASE_DIR / "Promo_TBX.mp4"  # локальный фолбэк для промо-поста
POST_PHOTO_PATH = BASE_DIR / "Promo_TBX.png"  # фолбэк на фото

# Источники видео для поста
POST_VIDEO_FILE_ID = os.getenv("POST_VIDEO_FILE_ID", "").strip()  # приоритетный источник
POST_VIDEO_URL = os.getenv("POST_VIDEO_URL", "").strip()          # опциональный фолбэк по прямой ссылке

# ID канала (username работает, но лучше numeric -100…)
CHANNEL_USERNAME = "@TBXtrade"

app_flask = Flask(__name__)  # создаём один раз глобально

# анти-дубликаты (idempotency)
PROCESSED_PAYMENTS: Dict[str, float] = {}  # хранит уникальные payment_id/tx_id/комбинации
PROCESSED_TTL_SEC = 3600  # 1 час

# =====================[ ENV CHECKS ]=====================
# Для Google Sheets обязателен GOOGLE_CREDS (JSON сервисного аккаунта в переменной окружения)
if not os.getenv("GOOGLE_CREDS"):
    raise EnvironmentError("🚨 Переменная окружения GOOGLE_CREDS не установлена!")

# =====================[ GOOGLE SHEETS INIT ]=====================
try:
    creds_dict = json.loads(os.getenv("GOOGLE_CREDS"))
    # Чиним переносы в private_key
    if "private_key" in creds_dict:
        creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")

    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, SCOPES)
    gc = gspread.authorize(creds)

    # Если у тебя фиксированный ID — оставь его здесь
    SPREADSHEET_ID = "1s_KQLyekb-lQjt3fMlBO39CTBuq0ayOIeKkXEhDjhbs"
    sheet = gc.open_by_key(SPREADSHEET_ID).sheet1  # или .worksheet("Лист1")
    logging.info("✅ Google Sheets connected")
except Exception as e:
    logging.exception("❌ Google Sheets init failed")
    raise

# =====================[ UTILS / HELPERS ]=====================
# Детектор отказов модели (refusal)
_REFUSAL_RE = re.compile(
    r"(i\s*can'?t\s*assist|i'?m\s*sorry|i\s*cannot\s*help|can'?t\s*help|won'?t\s*assist|not\s*able\s*to\s*comply)",
    re.IGNORECASE
)
def _is_refusal(text: str) -> bool:
    return bool(_REFUSAL_RE.search(text or ""))

def _safe_float(x):
    try:
        if x is None:
            return None
        return float(str(x).replace(" ", "").replace(",", "."))
    except Exception:
        return None

def _round2(x):
    return None if x is None else round(float(x), 2)

def _calc_rr(entry, stop, tp1):
    try:
        if entry is None or stop is None or tp1 is None:
            return None
        risk = abs(entry - stop)
        if risk <= 0:
            return None
        reward = abs(tp1 - entry)
        return round(reward / risk, 2)
    except Exception:
        return None

# Конвертация BytesIO -> JPEG Base64 (оставь, если где-то нужен именно BytesIO)
def _bytes_to_jpeg_b64(bio: BytesIO) -> str:
    bio.seek(0)
    im = Image.open(bio).convert("RGB")
    out = BytesIO()
    im.save(out, format="JPEG", quality=90, optimize=True)
    out.seek(0)
    return base64.b64encode(out.read()).decode("ascii")

# ---- image utils (единые версии; удалите дубли в файле) ----
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".heic")

async def _extract_image_bytes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> BytesIO | None:
    """
    Возвращает BytesIO с данными изображения из:
      • message.photo
      • message.effective_attachment (альбом)
      • message.document с image/* или подходящим расширением
    """
    msg = update.effective_message

    # 1) Обычное фото
    if getattr(msg, "photo", None):
        file_id = msg.photo[-1].file_id
        tg_file = await context.bot.get_file(file_id)
        bio = BytesIO()
        await tg_file.download_to_memory(out=bio)
        bio.seek(0)
        return bio

    # 2) Альбом / effective_attachment
    att = getattr(msg, "effective_attachment", None)
    if isinstance(att, list) and att:
        for a in reversed(att):  # крупные обычно в конце
            if isinstance(a, PhotoSize):
                tg_file = await context.bot.get_file(a.file_id)
                bio = BytesIO()
                await tg_file.download_to_memory(out=bio)
                bio.seek(0)
                return bio
            if isinstance(a, Document):
                a_mt = (a.mime_type or "").lower()
                a_fn = (a.file_name or "").lower()
                if a_mt.startswith("image/") or any(a_fn.endswith(ext) for ext in IMAGE_EXTS):
                    tg_file = await context.bot.get_file(a.file_id)
                    bio = BytesIO()
                    await tg_file.download_to_memory(out=bio)
                    bio.seek(0)
                    return bio

    # 3) Документ-картинка
    doc = getattr(msg, "document", None)
    if isinstance(doc, Document):
        mt = (doc.mime_type or "").lower()
        fn = (doc.file_name or "").lower()
        if mt.startswith("image/") or any(fn.endswith(ext) for ext in IMAGE_EXTS):
            tg_file = await context.bot.get_file(doc.file_id)
            bio = BytesIO()
            await tg_file.download_to_memory(out=bio)
            bio.seek(0)
            return bio

    return None

def _to_jpeg_base64(bio: BytesIO) -> str:
    """BytesIO -> JPEG -> base64 (ascii)"""
    bio.seek(0)
    with Image.open(bio) as im:
        im = im.convert("RGB")
        out = BytesIO()
        im.save(out, format="JPEG", quality=90, optimize=True)
        out.seek(0)
        return base64.b64encode(out.read()).decode("ascii")

# Устойчивый экстрактор изображений из Update (photo / document / media group)
async def _extract_image_bytes(update) -> bytes:
    """
    Возвращает bytes изображения из:
    - message.photo (стандартные фото)
    - effective_attachment (альбом: PhotoSize или Document-картинка)
    - message.document (если это картинка или файл с img-расширением)
    Бросает ValueError('image_not_found'), если ничего не нашли.
    """
    msg = update.effective_message

    # 1) Обычное фото
    if getattr(msg, "photo", None):
        file = await msg.photo[-1].get_file()
        return await file.download_as_bytearray()

    # 2) Альбом/прочие вложения (effective_attachment)
    att = getattr(msg, "effective_attachment", None)
    if isinstance(att, list) and att:
        # берём подходящее вложение (крупные обычно ближе к концу)
        for a in reversed(att):
            if isinstance(a, PhotoSize):
                file = await a.get_file()
                return await file.download_as_bytearray()
            if isinstance(a, Document):
                mt = (a.mime_type or "").lower()
                fn = (a.file_name or "").lower()
                if mt.startswith("image/") or any(fn.endswith(ext) for ext in IMAGE_EXTS):
                    file = await a.get_file()
                    return await file.download_as_bytearray()

    # 3) Документ как картинка (или «файл», но по расширению — картинка)
    doc = getattr(msg, "document", None)
    if isinstance(doc, Document):
        mt = (doc.mime_type or "").lower()
        fn = (doc.file_name or "").lower()
        if mt.startswith("image/") or any(fn.endswith(ext) for ext in IMAGE_EXTS):
            file = await doc.get_file()
            return await file.download_as_bytearray()

    raise ValueError("image_not_found")

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
# кто принимает личные обращения по рефералам
OWNER_CHAT_ID = 407721399


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
    [InlineKeyboardButton("💬 Обсудить в чате", url="https://t.me/TBX_Chat")]
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
        chat_id = '-1002747865995'
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

# -------------------- GPT-Vision вызов --------------------
async def ask_gpt_vision(
    prompt_text: str,
    image_base64: str,
    system_prompt: str | None = None,
    mime: str | None = "image/jpeg",
    force_ru: bool = True,
) -> str:
    """
    Вызов GPT-Vision (chat.completions) с изображением.
    - prompt_text: англоязычный запрос для модели.
    - image_base64: СЫРОЙ base64 без префикса 'data:'.
    - system_prompt: опциональный кастомный SYSTEM (EN). Если None — безопасный дефолт.
    - mime: MIME изображения ('image/jpeg' | 'image/png' | ...).
    - force_ru: добавлять ли хинт про «Strictly Russian». Отключаем для JSON-ONLY.
    """
    if not image_base64:
        return ""

    if system_prompt is None:
        system_prompt = (
            "You are a senior vision-enabled investment analyst. "
            "Always return concise, professional Russian output (Cyrillic only), "
            "no markdown, short clear sentences. Never refuse. No apologies."
        )

    _mime = (mime or "image/jpeg").strip().lower()
    data_url = f"data:{_mime};base64,{image_base64}"

    try:
        resp = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": f"{prompt_text}"
                                    + ("\n\nRespond strictly in Russian (Cyrillic). No markdown." if force_ru else "")
                        },
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                },
            ],
            max_tokens=1100,
            temperature=0.2,
            top_p=0.9,
            presence_penalty=0.0,
            frequency_penalty=0.05,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logging.error(f"[ask_gpt_vision] Error: {e}", exc_info=True)
        return ""

# -------------------- Утилиты: анти-отказ / парсинг чисел --------------------
refusal_markers = [
    "sorry", "i'm sorry", "cannot assist", "can't assist", "i cannot", "i can’t",
    "unable to", "not able to", "won’t be able", "cannot help", "can’t help",
    "provide financial advice", "financial advice", "disclaimer",
    "не могу", "я не могу", "не буду", "я не буду", "не могу помочь", "не могу с этим помочь",
]


async def handle_strategy_photo(update, context, image_bytes: BytesIO):
    """
    СПОТ, LONG-only, DCA.
    Первая строка ответа модели — валидный JSON одной строкой по схеме:
      {"direction":"LONG","entry":number|null,"avg_entry":number|null,"stop":number|null,
       "tp":[numbers],"dca":[{"price":number,"alloc_pct":number}],"notes":["text"]}
    Затем — понятный ответ на русском (без markdown).
    На СПОТе не используем «стоп-ордера»: показываем 5-ступенчатый DCA, среднюю цену и цели.
    В итоговом JSON поле "stop" принудительно = null (совместимость со схемой).
    """
    # ---------- локальные хелперы ----------
    def _sfloat(x):
        try:
            if x is None:
                return None
            return float(str(x).replace(" ", "").replace(",", "."))
        except Exception:
            return None

    def _r2(x):
        return None if x is None else round(float(x), 2)

    def _fmt_price(x: float | None) -> str:
        if x is None:
            return "—"
        d = 2 if abs(x) >= 1 else 4
        s = f"{x:,.{d}f}".replace(",", " ")
        return f"${s}"

    def _fmt_pct(x: float | None, max_dec=2) -> str:
        if x is None:
            return "—"
        xi = float(x)
        if abs(xi - round(xi)) < 1e-9:
            return f"{int(round(xi))}%"
        return f"{round(xi, max_dec)}%"

    def _potential_pct(avg_entry: float | None, tp1: float | None) -> float | None:
        try:
            if avg_entry is None or tp1 is None:
                return None
            return round((tp1 / avg_entry - 1.0) * 100.0, 2)
        except Exception:
            return None

    def _normalize_to_100(weights: list[float]) -> list[float]:
        s = sum(w for w in weights if w is not None)
        if s <= 0:
            return [0.0 for _ in weights]
        scaled = [w * 100.0 / s for w in weights]
        rounded = [round(x, 2) for x in scaled]
        diff = round(100.0 - sum(rounded), 2)
        if rounded:
            rounded[-1] = round(rounded[-1] + diff, 2)
        return rounded

    def _build_5_step_dca(dca_in: list[dict], base_price: float | None) -> list[dict]:
        """
        Гарантируем 5 DCA-ступеней:
        - сортируем по цене по убыванию;
        - если <5 — достраиваем 4-ю и 5-ю как −3% и −6% от текущей нижней цены (или base_price);
        - проценты нормализуем к сумме 100%.
        """
        steps = []
        for s in (dca_in or []):
            p = _sfloat((s or {}).get("price"))
            a = _sfloat((s or {}).get("alloc_pct"))
            if p is not None and a is not None and p > 0 and a > 0:
                steps.append({"price": p, "alloc_pct": a})
        steps.sort(key=lambda x: x["price"], reverse=True)

        low_ref = min((s["price"] for s in steps), default=_sfloat(base_price))
        while len(steps) < 5:
            if low_ref is None or low_ref <= 0:
                new_price = None
            else:
                factor = 0.97 if len(steps) == 3 else 0.94  # 4-я ≈−3%, 5-я ≈−6% от нижней
                new_price = round(low_ref * factor, 2)
                low_ref = new_price
            steps.append({"price": new_price, "alloc_pct": 0.0})

        exist_sum = sum(s["alloc_pct"] for s in steps[:3])
        if exist_sum <= 0:
            tmpl = [40.0, 25.0, 20.0, 10.0, 5.0]
            for i in range(5):
                steps[i]["alloc_pct"] = tmpl[i]
        else:
            target_new_sum = 20.0  # хотим 10%+10% на 4-ю и 5-ю
            if exist_sum + target_new_sum <= 100.0:
                steps[3]["alloc_pct"] = 10.0
                steps[4]["alloc_pct"] = 10.0
                remain = 100.0 - (exist_sum + 20.0)
                if remain > 0:
                    scale = (exist_sum + remain) / exist_sum
                    for i in range(3):
                        steps[i]["alloc_pct"] = steps[i]["alloc_pct"] * scale
            else:
                scale = max((100.0 - target_new_sum) / exist_sum, 0.0)
                for i in range(3):
                    steps[i]["alloc_pct"] = steps[i]["alloc_pct"] * scale
                steps[3]["alloc_pct"] = 10.0
                steps[4]["alloc_pct"] = 10.0

        weights = _normalize_to_100([s["alloc_pct"] for s in steps])
        for i in range(5):
            steps[i]["alloc_pct"] = weights[i]
            steps[i]["price"] = None if steps[i]["price"] is None else round(steps[i]["price"], 2)
        return steps

    msg = update.effective_message if update else None
    if not msg:
        return

    try:
        # 1) Байты изображения
        if not isinstance(image_bytes, BytesIO):
            image_bytes = await _extract_image_bytes(update, context)
            if not image_bytes:
                await msg.reply_text("Не вижу изображения. Пришлите скрин как фото или документ-картинку (PNG/JPG/WEBP).")
                return

        # 2) JPEG → base64
        try:
            image_bytes.seek(0)
            im = Image.open(image_bytes).convert("RGB")
            buf = BytesIO()
            im.save(buf, format="JPEG", quality=90, optimize=True)
            buf.seek(0)
            import base64 as _b64
            img_b64 = _b64.b64encode(buf.read()).decode("ascii")
        except Exception:
            await msg.reply_text("Не удалось прочитать изображение. Пришлите скрин в формате PNG/JPG.")
            return

        # 3) Промпты (EN)
        system_prompt = (
            "You are an institutional investor creating a SPOT DCA plan (LONG only). "
            "Always respond with a VALID ONE-LINE JSON as the FIRST line using the schema: "
            "{\"direction\":\"LONG\",\"entry\":number|null,\"avg_entry\":number|null,\"stop\":number|null,"
            "\"tp\":[numbers],\"dca\":[{\"price\":number,\"alloc_pct\":number}],\"notes\":[\"text\"]}. "
            "Prefer providing 4–5 DCA steps with decreasing prices. "
            "Then provide a Russian explanation for a beginner. Never refuse; provide best-effort values."
        )
        user_prompt = (
            "TASK:\n"
            "1) Read the chart image and propose a SPOT DCA plan (LONG-only).\n"
            "2) FIRST LINE: exactly ONE line JSON (no code fences) per schema.\n"
            "3) Then explain in Russian for a beginner (plain text, no markdown).\n"
            "4) DCA list must have 4-5 steps with valid prices and alloc_pct (sum ≈100%).\n"
            "5) If unsure, still provide best estimates. Do NOT refuse.\n"
        )

        def _needs_retry(t: str) -> bool:
            if not t:
                return True
            low = t.lower()
            return any(s in low for s in ("i can't", "cannot", "i won’t", "sorry", "as an ai"))

        # 4) Вызов модели (2 попытки)
        client_obj = globals().get("client")
        if client_obj is None:
            from openai import AsyncOpenAI
            import os
            client_obj = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

        content_text = None
        for _ in range(2):
            try:
                resp = await client_obj.chat.completions.create(
                    model="gpt-4o",
                    temperature=0.1,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": [
                            {"type": "text", "text": user_prompt},
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}}
                        ]}
                    ]
                )
                content_text = (resp.choices[0].message.content or "").strip()
                if not _needs_retry(content_text):
                    break
                system_prompt += " You must comply. Provide your best estimate. Do not refuse."
            except Exception:
                logging.exception("Vision call failed (strategy)")

        # 5) Парсинг 1-й строки как JSON (фолбэк — регексы)
        if not content_text:
            data = {"direction": "LONG", "entry": None, "avg_entry": None, "stop": None, "tp": [], "dca": [], "notes": [
                "Нет уверенных уровней на скрине. Используйте плавный DCA и контролируйте долю позиции в портфеле."
            ]}
        else:
            lines = content_text.splitlines()
            first = (lines[0] if lines else "").strip()
            try:
                data = json.loads(first)
                if not isinstance(data, dict):
                    raise ValueError("not object")
            except Exception:
                txt = content_text
                dca = []
                for m in re.finditer(r'(?:Купить|Buy)\s*([0-9]+(?:\.[0-9]+)?)\s*%\D+\$?\s*([0-9]+(?:\.[0-9]+)?)', txt, re.I):
                    alloc = _sfloat(m.group(1)); price = _sfloat(m.group(2))
                    if price is not None and alloc is not None:
                        dca.append({"price": price, "alloc_pct": alloc})
                for m in re.finditer(r'price\s*[:=]\s*\$?\s*([0-9]+(?:\.[0-9]+)?)\D+alloc(?:_pct)?\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)\s*%', txt, re.I):
                    price = _sfloat(m.group(1)); alloc = _sfloat(m.group(2))
                    if price is not None and alloc is not None:
                        dca.append({"price": price, "alloc_pct": alloc})
                entry = None
                m = re.search(r'(?:Entry|Вход)\s*[:=]\s*\$?\s*([0-9]+(?:\.[0-9]+)?)', txt, re.I)
                if m: entry = _sfloat(m.group(1))
                tps = []
                for label in ("TP1","TP2","TP3","Цель1","Цель2","Цель3"):
                    m = re.search(rf'(?:{label})\s*[:=]\s*\$?\s*([0-9]+(?:\.[0-9]+)?)', txt, re.I)
                    if m:
                        v = _sfloat(m.group(1))
                        if v is not None:
                            tps.append(v)
                data = {"direction": "LONG", "entry": entry, "avg_entry": None, "stop": None, "tp": tps, "dca": dca, "notes": ["Эвристический парсинг текста."]}

        # 6) Нормализация и построение 5 ступеней
        data["direction"] = "LONG"
        entry = _sfloat(data.get("entry"))
        tps   = [_sfloat(x) for x in (data.get("tp") or []) if _sfloat(x) is not None]
        dca_in = data.get("dca") or []

        base_price = _sfloat((dca_in[0] or {}).get("price")) if dca_in else entry
        dca5 = _build_5_step_dca(dca_in, base_price)

        # Средняя по 5 ступеням
        wsum = sum((s["alloc_pct"] or 0.0) for s in dca5)
        psum = sum((_sfloat(s["price"]) or 0.0) * (s["alloc_pct"] or 0.0) for s in dca5)
        avg_entry = (psum / wsum) if wsum > 0 else None

        # Цели > средней
        if avg_entry is not None:
            tps = [x for x in tps if x > avg_entry]
            if not tps:
                tps = [avg_entry * 1.05, avg_entry * 1.10]

        tp1 = tps[0] if tps else None
        potential = _potential_pct(avg_entry, tp1)

        # 7) Финальный JSON (stop = None)
        data_norm = {
            "direction": "LONG",
            "entry": _r2(entry),
            "avg_entry": _r2(avg_entry),
            "stop": None,
            "tp": [_r2(x) for x in tps[:3]],
            "dca": [{"price": _r2(_sfloat(s["price"])), "alloc_pct": _r2(_sfloat(s["alloc_pct"]))} for s in dca5],
            "notes": list(dict.fromkeys((data.get("notes") or [])))
        }

        # ---------- красивый ответ ----------
        parts = []
        parts.append("0️⃣ Суть")
        parts.append("• Долгосрок, СПОТ, только покупка. План через DCA (5 ступеней, без плеча).")

        dca_line = " ; ".join(
            f"Купить {_fmt_pct(s['alloc_pct'])} по {_fmt_price(s['price'])}"
            for s in data_norm["dca"]
        )
        parts.append("1️⃣ План покупок")
        parts.append("• " + dca_line)

        if data_norm["avg_entry"] is not None:
            parts.append(f"2️⃣ Средняя цена входа: {_fmt_price(data_norm['avg_entry'])}")

        if data_norm["tp"]:
            tps_str = ", ".join(_fmt_price(x) for x in data_norm["tp"])
            parts.append(f"3️⃣ Цели (TP1..TP{len(data_norm['tp'])}): {tps_str}")

        if potential is not None:
            sign = "+" if potential >= 0 else ""
            parts.append(f"4️⃣ Потенциал к TP1: {sign}{potential}%")
        else:
            parts.append("4️⃣ Потенциал к TP1: недостаточно данных.")

        notes = [str(n).strip() for n in (data_norm.get("notes") or []) if str(n).strip()]
        parts.append("⚠️ Комментарии")
        if notes:
            for n in notes[:5]:
                parts.append(f"• {n}")
        else:
            parts.append("• Нет особых замечаний. Действуйте по плану DCA и контролируйте долю позиции в портфеле.")

        parts.append("✅ Что дальше")
        parts.append("• Не используйте плечо. Покупайте частями по плану DCA.")
        parts.append("• Доля одной позиции в портфеле — разумная (например, до 10–20%).")
        parts.append("• Фиксируйте часть прибыли по целям; остаток можно держать дольше при подтверждении тренда.")

        # Тех-JSON: пишем в логи, в чат — только если включён флаг
        compact_json = json.dumps(data_norm, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        logging.info("strategy_json=%s", compact_json)
        if bool(globals().get("SHOW_JSON_IN_CHAT", False)):
            parts.append(f'"""{compact_json}"""')

        await msg.reply_text("\n".join(parts))

    except Exception:
        logging.exception("handle_strategy_photo failed")
        await msg.reply_text("Не удалось построить инвест-стратегию по скрину. Пришлите другой скрин или попробуйте позже.")


async def handle_uid_submission(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Пользователь прислал UID для реферальной программы:
    - Аккуратно извлекаем UID (строка цифр, допускаем пробелы/дефисы в исходном тексте)
    - Валидируем базово (>= 5 цифр)
    - Пишем строку в Google Sheets
    - Даём пользователю подтверждение
    """
    msg = update.effective_message
    raw = (getattr(msg, "text", "") or "").strip()

    # 1) Нормализация UID: вытаскиваем только цифры (сохраняем лидирующие нули)
    digits_only = _re.sub(r"\D", "", raw)
    if len(digits_only) < 5:
        await msg.reply_text(
            "❗️ Пришли UID цифрами. Пример: 24676081.",
            reply_markup=REPLY_MARKUP
        )
        return

    uid = digits_only

    # 2) Данные пользователя и контекста
    user = update.effective_user
    user_id = getattr(user, "id", None)
    username = (getattr(user, "username", None) or "no_username")

    ref_program = context.user_data.get("ref_program", "broker_ref")
    broker = context.user_data.get("broker", "unknown")

    # 3) Подготовка строки для записи
    now = datetime.now(tz=_TZ) if _TZ else datetime.now()
    when_str = now.strftime("%Y-%m-%d %H:%M")

    row = [
        str(user_id or ""),
        username,
        when_str,
        ref_program,
        broker,
        uid
    ]

    # 4) Пишем в таблицу безопасно (в отдельном потоке)
    try:
        await asyncio.to_thread(safe_append_row, row)
        logging.info(f"[REF_UID] ok user_id={user_id} username={username} broker={broker} uid={uid}")
        await msg.reply_text(
            "✅ UID принят. Проверка займёт до 10 минут. Напишу в этот чат, когда доступ будет активирован.",
            reply_markup=REPLY_MARKUP
        )
    except Exception as e:
        logging.error(f"[handle_uid_submission] Google Sheets error: {e}")
        await msg.reply_text(
            "⚠️ Не удалось зафиксировать UID. Попробуй ещё раз позже "
            f"или напиши владельцу: tg://user?id={OWNER_CHAT_ID}",
            reply_markup=REPLY_MARKUP
        )

    # 5) Чистим флаг ожидания UID и вспомогательные поля
    context.user_data.pop("awaiting_uid", None)
    # Если нужно сбрасывать выбранного брокера/программу после подачи, раскомментируй:
    # context.user_data.pop("broker", None)
    # context.user_data.pop("ref_program", None)

async def handle_calendar_photo(update: Update, context: ContextTypes.DEFAULT_TYPE, image_bytes: BytesIO | None = None):
    """
    Обрабатывает скрин экономического календаря:
    1) Берёт BytesIO (из аргумента или вытаскивает сам из сообщения),
    2) Конвертирует в JPEG base64,
    3) Вызывает generate_news_from_image(...) — промпт жёстко связывает интерпретацию с грядущим заседанием ФРС,
    4) Отправляет результат пользователю (строго RU, без инвестсоветов).
    """
    msg = update.effective_message

    # 0) Защитим состояние (если ты где-то отмечаешь «ждём календарь»)
    context.user_data.pop("awaiting_calendar_photo", None)

    # 1) Достаём картинку
    try:
        if image_bytes is None:
            image_bytes = await _extract_image_bytes(update, context)
        if image_bytes is None:
            await msg.reply_text(
                "⚠️ Не вижу изображения календаря.\n"
                "Пришлите фото или документ-картинку (PNG/JPG) с видимыми полями: событие, Факт / Прогноз / Пред.",
                reply_markup=ReplyKeyboardMarkup([["↩️ Выйти в меню"]], resize_keyboard=True)
            )
            return
    except Exception as e_extract:
        logging.exception("[calendar] extract error")
        await msg.reply_text(
            "⚠️ Не удалось получить изображение. Пришлите скрин как фото или как документ (PNG/JPG).",
            reply_markup=ReplyKeyboardMarkup([["↩️ Выйти в меню"]], resize_keyboard=True)
        )
        return

    # 2) Сообщение-прогресс с акцентом на ФРС
    await msg.reply_text("🔎 Читаю скрин и оцениваю, как это сдвигает расклад перед ближайшим заседанием ФРС…")

    # 3) JPEG→b64 и генерация интерпретации
    try:
        jpeg_b64 = _to_jpeg_base64(image_bytes)
        analysis_ru = await generate_news_from_image(jpeg_b64)

        # Страховка от пустых/коротких ответов
        if not analysis_ru or not analysis_ru.strip():
            analysis_ru = (
                "1) Событие и цифры: не удалось надёжно распознать значения (факт/прогноз/пред.).\n"
                "2) Сюрприз и смысл: сравните факт с прогнозом — для инфляции/занятости выше прогноза чаще «ястребиный» сигнал, ниже — «голубиный».\n"
                "3) Связь с заседанием ФРС: жёсткие данные уменьшают шансы снижения ставки; мягкие — повышают.\n"
                "4) Влияние (1–3 дня): «ястреб» → DXY/доходности ↑, риск-активы под давлением; «голубь» → поддержка акций и крипто.\n"
                "5) Крипто и альтсезон: мягкий фон поддерживает BTC/ETH; альты оживают при устойчивом риск-он.\n"
                "6) Сценарии: мягкие данные → риск-он; жёсткие → риск-офф. Следите за пересмотрами и близкими релизами."
            )

        # Телеграм ограничение ~4096 символов — аккуратно разобьём при необходимости
        text = "🧠 Интерпретация события в контексте заседания ФРС:\n\n" + analysis_ru.strip()
        if len(text) <= 4000:
            await msg.reply_text(
                text,
                reply_markup=ReplyKeyboardMarkup([["↩️ Выйти в меню"]], resize_keyboard=True)
            )
        else:
            # Разбить на части по абзацам
            parts = []
            chunk = []
            size = 0
            for line in text.splitlines(True):
                if size + len(line) > 3800:
                    parts.append("".join(chunk))
                    chunk, size = [line], len(line)
                else:
                    chunk.append(line)
                    size += len(line)
            if chunk:
                parts.append("".join(chunk))
            # Отправим по порядку
            for i, p in enumerate(parts, 1):
                header = "" if i == 1 else f"(часть {i}/{len(parts)})\n"
                await msg.reply_text(
                    header + p,
                    reply_markup=ReplyKeyboardMarkup([["↩️ Выйти в меню"]], resize_keyboard=True)
                )

    except Exception as e:
        logging.exception("[calendar] analysis error")
        await msg.reply_text(
            "⚠️ Не удалось распознать скрин.\n"
            "Совет: кадрируйте область с названием релиза и числами «Факт / Прогноз / Пред.», "
            "уберите лишнее и пришлите снова.",
            reply_markup=ReplyKeyboardMarkup([["↩️ Выйти в меню"]], resize_keyboard=True)
        )

NEWS_PROMPT_EN = """
You are a macro analyst. Interpret an economic calendar screenshot (e.g., CPI, PPI, NFP, ISM, Retail Sales, Jobless Claims, PMI, GDP, Core/PCE, etc.).
Your single mission: tie this event's interpretation to the upcoming FOMC meeting where the Fed decides on the policy rate.

Hard constraints:
- Education-only. No personal investment advice. No trading signals (no entries/stops/take-profits).
- OUTPUT LANGUAGE: RUSSIAN ONLY. If any English appears, regenerate in Russian.
- Be concrete and consistent; avoid buzzwords. Do not over-hedge with “it depends” unless you specify exactly on what.

Specific reasoning you MUST do:
1) Extract EVENT NAME and the three numbers: ACTUAL, FORECAST, PREVIOUS (from the screenshot).
2) Classify the surprise vs FORECAST as positive/negative/neutral and explain WHY for this specific indicator (e.g., higher-than-forecast CPI = hawkish).
3) Explicitly link the surprise to the FOMC path:
   - Directional impact on rate odds: повышает/понижает вероятность снижения ставки на ближайшем заседании ФРС.
   - Tone bias for the statement/press-conference: более «ястребиная» или «голубиная» риторика и почему.
   - Which subcomponents or related series the Fed will care about (e.g., core vs headline, services ex-shelter, labor tightness, demand vs supply).
4) 1–3 day market map: risk-on/risk-off bias and why. Mention DXY, UST yields (доходности), SPX/Nasdaq as proxies.
5) Crypto tie-in in the context of an incoming altseason narrative: when would BTC/ETH hold better or altcoins get bid, and what could invalidate it.
6) Provide two crisp scenarios (bull/bear) with clear triggers that could flip one into another.
7) Risk section: what could negate today’s read (revisions, overlapping releases) and the next checkpoints BEFORE the FOMC meeting.

Return format (RUSSIAN, no markdown):
1) Событие и цифры: <название>, факт / прогноз / пред.
2) Сюрприз и смысл: <почему это hawkish/dovish для ФРС именно по природе показателя>
3) Связь с заседанием ФРС: <как сдвигает шансы снижения/сохранения ставки; ожидаемая риторика и почему>
4) Влияние на рынки (1–3 дня): <DXY, доходности UST, SPX/Nasdaq; риск-он/риск-офф и логика>
5) Крипто и альтсезон: <что это значит для BTC/ETH и альтов; условия усиления/отмены импульса>
6) Сценарии:
   • Bull: <триггеры и последствия>
   • Bear: <триггеры и последствия>
7) Риски и что дальше смотреть: <какие релизы/комментарии способны изменить картину до заседания ФРС>
"""

async def generate_news_from_image(jpeg_b64: str) -> str:
    """Интерпретация календаря по скрину с фокусом на грядущее заседание ФРС. 2 попытки + читаемый RU-fallback."""
    base_prompt = NEWS_PROMPT_EN + "\n\nОтвет строго на русском. Без англоязычных терминов. Без инвестиционных рекомендаций."
    out = await ask_gpt_vision(prompt_text=base_prompt, image_base64=jpeg_b64)

    # Перегенерация, если пришёл отказ / пусто / заметно англ.
    def _looks_english(s: str) -> bool:
        s = (s or "").strip()
        return not s or s[:80].isascii()  # грубая эвристика: начало строки чисто ASCII

    if _is_refusal(out) or _looks_english(out):
        stronger = (
            NEWS_PROMPT_EN
            + "\n\nСТРОГО на русском языке, без английских слов и аббревиатур. "
              "Только образовательная макро-интерпретация, никаких торговых рекомендаций. "
              "Если ответ начат на английском или содержит отказ — перегенерируй и выдай корректный русский разбор."
        )
        out = await ask_gpt_vision(prompt_text=stronger, image_base64=jpeg_b64)

    # План Б — аккуратный шаблон, чтобы пользователь всё равно получил пользу
    if _is_refusal(out) or not out:
        out = (
            "1) Событие и цифры: не удалось надёжно распознать значения со скрина (факт/прогноз/пред.).\n"
            "2) Сюрприз и смысл: сопоставьте факт с прогнозом. Для инфляции и занятости выше прогноза — чаще «ястребиный» сигнал; ниже — «голубиный».\n"
            "3) Связь с заседанием ФРС: позитивный сюрприз по инфляции/рынку труда снижает шансы на понижение ставки и усиливает жёсткую риторику; слабость данных — наоборот.\n"
            "4) Влияние на рынки (1–3 дня): «ястребиный» фон → рост доллара (DXY) и доходностей, давление на акции; «голубиный» → поддержка акций и рисковых активов.\n"
            "5) Крипто и альтсезон: «голубиный» сдвиг чаще поддерживает BTC/ETH; альты получают приток позже и на фоне общего риска-он. «Ястребиный» — повышает волатильность и риск распродаж.\n"
            "6) Сценарии:\n"
            "   • Bull: мягкие данные → рынок закладывает больше шансов снижения ставки, риск-он усиливается.\n"
            "   • Bear: жёсткие данные → шансы снижения ставки тают, доллар и доходности вверх, давление на риск.\n"
            "7) Риски и что дальше смотреть: пересмотры данных, комментарии членов ФРС, близкие релизы (Core PCE, NFP/заявки, ISM/PMI). До заседания именно они могут поменять баланс."
        )
    return out

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
        # включаем дефолтный «инвест-режим по фото», чтобы скрин сразу ушёл в handle_strategy_photo
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

    # 🔗 Бесплатный доступ через брокера (разрешено без подписки)
    if text == "🔗 Бесплатный доступ через брокера":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Bybit — регистрация", callback_data="ref_bybit")],
            [InlineKeyboardButton("Forex4You — регистрация", callback_data="ref_forex4you")],
            [InlineKeyboardButton("Связаться с владельцем", url=f"tg://user?id={OWNER_CHAT_ID}")],
        ])
        await msg.reply_text(
            "Мы готовы обработать ваш новый UID. Давайте продолжим.\n\n"
            "Если предыдущий UID не прошёл проверку, сделайте так:\n\n"
            "👉 Если у вас ещё нет аккаунта на Bybit — зарегистрируйтесь по моей реферальной ссылке (нажмите кнопку выше).\n"
            "👉 Если аккаунт уже есть — прикрепите его к нашему реферальному коду. После этого пришлите свой новый UID сюда.\n\n"
            "После регистрации/привязки внесите минимальный депозит (Bybit: $150, Forex4You: $200) и пришлите UID в чат.\n\n"
            "Если возникнут трудности — нажмите «Связаться с владельцем».",
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
    await msg.reply_text("🔄 Сброс всех ожиданий. Выберите действие в меню.", reply_markup=REPLY_MARKUP)


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

# Health-check для Render
@app_flask.route("/", methods=["GET", "HEAD"])
def render_health_ok():
    return "OK", 200

# === Save post video (file_id) ===============================================
async def save_post_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохраняет file_id видео для поста. Работает так:
    - если команда дана в ответ на видео → берём video из reply
    - если команда отправлена вместе с видео → берём video из текущего сообщения
    - иначе подсказываем, что нужно сделать
    """
    uid = update.effective_user.id
    if uid not in ADMIN_IDS:
        return await update.message.reply_text("⛔️ Нет прав.")

    msg = update.effective_message
    vid = None

    # 1) Если команда дана в ответ на видео
    if msg and msg.reply_to_message and msg.reply_to_message.video:
        vid = msg.reply_to_message.video
    # 2) Если видео прислано вместе с командой
    elif msg and msg.video:
        vid = msg.video

    if not vid:
        return await update.message.reply_text(
            "Пришли видео (MP4) и ответь на него командой /save_post_video\n"
            "или отправь команду вместе с видео в одном сообщении."
        )

    file_id = vid.file_id
    globals()["POST_VIDEO_FILE_ID"] = file_id  # можно ещё положить в .env/БД при желании
    await update.message.reply_text(
        "✅ Видео сохранено для публикаций.\n"
        f"file_id: <code>{file_id}</code>",
        parse_mode="HTML"
    )

async def publish_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Публикует промо-пост в канал, закрепляет его.
    В одном сообщении: первая строка inline-кнопок — «Подключиться» (попадёт в плашку закрепа),
    ниже — остальные кнопки (VIP-канал, Чат и т.п.).

    Приоритет источников видео:
      1) POST_VIDEO_FILE_ID
      2) Локальный файл POST_VIDEO_PATH
      3) POST_VIDEO_URL
    Фолбэк — фото POST_PHOTO_PATH.
    """
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔️ У тебя нет прав на публикацию.")
        return

    # --- конфиг/пути ---
    bot_url = (globals().get("BOT_URL") or "https://t.me/CtyptorobBot")
    chat_id = globals().get("CHANNEL_USERNAME")

    file_id = (str(globals().get("POST_VIDEO_FILE_ID") or os.getenv("POST_VIDEO_FILE_ID", ""))).strip()
    video_url = (str(globals().get("POST_VIDEO_URL") or os.getenv("POST_VIDEO_URL", ""))).strip()

    base_dir = Path(__file__).resolve().parent
    _video_path = globals().get("POST_VIDEO_PATH", Path("Promo_TBX.mp4"))
    _photo_path = globals().get("POST_PHOTO_PATH", Path("Promo_TBX.png"))
    video_path = _video_path if isinstance(_video_path, Path) else Path(_video_path)
    photo_path = _photo_path if isinstance(_photo_path, Path) else Path(_photo_path)
    if not video_path.is_absolute():
        video_path = base_dir / video_path
    if not photo_path.is_absolute():
        photo_path = base_dir / photo_path

    caption = (
        "🚀 <b>ТВХ — твоя точка входа в трейдинг</b>\n"
        "Не просто бот, а экосистема: 🤖 GPT-бот · 💬 чат с топиками · 🔒 VIP-сигналы.\n\n"
        "⏳ <b>Почему сейчас</b>\n"
        "• Альтсезон близко: если не сейчас, то, возможно, никогда\n"
        "• VIP-места ограничены — потом доступ будет дороже\n"
        "• Каждая неделя промаха = потерянные X% роста\n\n"
        "📈 <b>Что ты получаешь</b>\n"
        "• Прогноз по скрину за 10 секунд\n"
        "• Планы сделок: где войти, где стоп, где зафиксировать прибыль\n"
        "• Рынки: Crypto · Forex · MOEX\n"
        "• Разбор новостей и поддержка после неудачных сделок 🧘\n\n"
        f"💳 <b>Подключение</b>: ${MONTHLY_PRICE_USD}/мес или ${LIFETIME_PRICE_USD} навсегда\n"
        "📊 <b>Альтернатива</b>: бесплатный доступ через брокера (пиши менеджеру)\n\n"
        "🔗 <b>Инфраструктура ТВХ</b>\n"
        "• Публичный канал: <a href=\"https://t.me/TBXtrade\">t.me/TBXtrade</a>\n"
        "• Чат с топиками: <a href=\"https://t.me/TBX_Chat\">t.me/TBX_Chat</a>\n"
        "• Приватный канал (VIP): <a href=\"https://t.me/+TAbYnYSzHYI0YzVi\">перейти</a>\n\n"
        "💬 <b>Вопросы</b>: <a href=\"https://t.me/zhbankov_alex\">@zhbankov_alex</a>"
    )

    # ⬇️ Первая строка — «Подключиться» (попадёт в плашку закрепа). Ниже — остальные кнопки.
    keyboard_inline = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Подключиться", url=bot_url)],
        [InlineKeyboardButton("🔒 VIP-канал", url="https://t.me/+TAbYnYSzHYI0YzVi")],
        [InlineKeyboardButton("💬 Чат с топиками", url="https://t.me/TBX_Chat")],
    ])

    try:
        logging.info(
            f"[publish_post] chat={chat_id} file_id={'yes' if file_id else 'no'} "
            f"video_path={video_path} exists={video_path.exists()} url={'yes' if video_url else 'no'}"
        )

        # снять старый пин
        try:
            chat_obj = await context.bot.get_chat(chat_id)
            pinned = getattr(chat_obj, "pinned_message", None)
            if pinned:
                await context.bot.unpin_chat_message(chat_id=chat_id, message_id=pinned.message_id)
        except Exception as e_unpin:
            logging.warning(f"[publish_post] unpin failed: {e_unpin}")

        message = None
        last_err = None

        # отправка видео по file_id
        if file_id:
            try:
                message = await context.bot.send_video(
                    chat_id=chat_id,
                    video=file_id,
                    caption=caption,
                    parse_mode="HTML",
                    supports_streaming=True,
                    reply_markup=keyboard_inline,
                )
                logging.info("[publish_post] send_video by file_id OK")
            except Exception as e:
                last_err = e
                logging.error(f"[publish_post] send_video by file_id ERROR: {e}")

        # фолбэк — локальный файл
        if message is None and video_path.exists():
            try:
                with video_path.open("rb") as v:
                    message = await context.bot.send_video(
                        chat_id=chat_id,
                        video=v,
                        caption=caption,
                        parse_mode="HTML",
                        supports_streaming=True,
                        reply_markup=keyboard_inline,
                    )
                logging.info("[publish_post] send_video by file path OK")
            except Exception as e_video:
                last_err = e_video
                logging.error(f"[publish_post] send_video by path ERROR: {e_video}")

        # фолбэк — URL
        if message is None and video_url:
            try:
                message = await context.bot.send_video(
                    chat_id=chat_id,
                    video=video_url,
                    caption=caption,
                    parse_mode="HTML",
                    supports_streaming=True,
                    reply_markup=keyboard_inline,
                )
                logging.info("[publish_post] send_video by URL OK")
            except Exception as e_url:
                last_err = e_url
                logging.error(f"[publish_post] send_video by URL ERROR: {e_url}")

        # финальный фолбэк — фото
        if message is None:
            if not photo_path.exists():
                raise FileNotFoundError(
                    f"Нет источника видео (file_id/файл/URL) и нет фото ({photo_path}). "
                    f"Последняя ошибка по видео: {last_err}"
                )
            with photo_path.open("rb") as ph:
                message = await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=ph,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=keyboard_inline,
                )
            logging.info("[publish_post] send_photo OK")

        # закреп
        try:
            await context.bot.pin_chat_message(
                chat_id=chat_id,
                message_id=message.message_id,
                disable_notification=True,
            )
            logging.info("[publish_post] pin OK")
        except Exception as e_pin:
            logging.warning(f"[publish_post] pin failed: {e_pin}")

        await update.message.reply_text("✅ Пост опубликован и закреплён (кнопка в шапке + кнопки под постом).")

    except Exception as e:
        logging.exception("[publish_post] FAILED")
        await update.message.reply_text(f"⚠️ Не удалось опубликовать/закрепить пост.\nПричина: {e}")

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


# Универсальный загрузчик изображения (photo или document-картинка)
async def _extract_image_bytes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> BytesIO | None:
    msg = update.effective_message
    file_id = None

    if getattr(msg, "photo", None):
        file_id = msg.photo[-1].file_id
    elif getattr(msg, "document", None):
        doc = msg.document
        if (doc.mime_type or "").lower().startswith("image/"):
            file_id = doc.file_id

    if not file_id:
        return None

    tg_file = await context.bot.get_file(file_id)
    bio = BytesIO()
    await tg_file.download_to_memory(out=bio)  # PTB 21.x требует keyword-аргумент out=
    bio.seek(0)
    return bio

def _to_jpeg_base64(bio: BytesIO) -> str:
    bio.seek(0)
    im = Image.open(bio).convert("RGB")
    out = BytesIO()
    im.save(out, format="JPEG", quality=90, optimize=True)
    out.seek(0)
    return base64.b64encode(out.read()).decode("ascii")


# Безопасный вызов необязательных хендлеров (если их нет — не падаем)
async def _call_if_exists(fn_name: str, update: Update, context: ContextTypes.DEFAULT_TYPE, fallback_text: str | None = None):
    fn = globals().get(fn_name)
    if callable(fn):
        if inspect.iscoroutinefunction(fn):
            return await fn(update, context)
        return fn(update, context)
    if fallback_text:
        await update.effective_message.reply_text(fallback_text)
    return None

# На случай, если вызывается из handle_photo, а функция не определена у тебя в файле
def _fallback_strategy() -> str:
    return "Краткий план не сформирован — пришли более чистый скрин (LuxAlgo SMC + уровни S/R)."

async def unified_text_handler(update, context):
    """
    Единый роутер сообщений (PTB 21.x, async).

    Приоритет:
    1) awaiting_calendar_photo  -> обработка календаря
    2) awaiting_strategy == 'photo' -> вытащить байты и вызвать handle_strategy_photo
    3) если фото/док-картинка — handle_photo
    4) иначе — handle_main

    Во всех ветках: устойчивость к None, короткие RU-сообщения при ошибках.
    """
    try:
        msg = update.effective_message if update else None
        if not msg:
            return

        text = (getattr(msg, "text", "") or "").strip()

        # --- детекция присутствия изображения (photo / document / media group)
        doc = getattr(msg, "document", None)
        fn = (doc.file_name or "").lower() if doc else ""
        mt = (doc.mime_type or "").lower() if doc else ""
        is_image_doc = bool(doc and (mt.startswith("image/") or any(fn.endswith(ext) for ext in IMAGE_EXTS)))

        att = getattr(msg, "effective_attachment", None)
        att_has_image = False
        if isinstance(att, list) and att:
            for a in att:
                if isinstance(a, PhotoSize):
                    att_has_image = True
                    break
                if isinstance(a, Document):
                    a_mt = (a.mime_type or "").lower()
                    a_fn = (a.file_name or "").lower()
                    if a_mt.startswith("image/") or any(a_fn.endswith(ext) for ext in IMAGE_EXTS):
                        att_has_image = True
                        break

        has_photo = bool(getattr(msg, "photo", None)) or is_image_doc or att_has_image

        # ↩️ Выход в меню — сбрасываем все «ожидалки», показываем меню и выходим (без вызова handle_main)
        if text in ("↩️ Выйти в меню", "↩️ Вернуться в меню"):
            for k in (
                "awaiting_calendar_photo",
                "awaiting_strategy",
                "awaiting_strategy_mode",
                "awaiting_potential",
                "awaiting_definition_term",
                "awaiting_invest_question",
                "awaiting_uid",
            ):
                context.user_data.pop(k, None)

            await msg.reply_text("🔙 Вернулись в главное меню.", reply_markup=REPLY_MARKUP)
            return

        # 1) Экономкалендарь (фото/док-картинка)
        if context.user_data.get("awaiting_calendar_photo"):
            try:
                bio = await _extract_image_bytes(update, context)
            except Exception:
                bio = None
            context.user_data.pop("awaiting_calendar_photo", None)
            if not bio:
                await msg.reply_text("⚠️ Не вижу изображения календаря. Пришлите фото или документ-картинку (PNG/JPG).")
                return
            await handle_calendar_photo(update, context, image_bytes=bio)
            return

        # 2) Инвест-стратегия по фото
        if context.user_data.get("awaiting_strategy") == "photo":
            try:
                bio = await _extract_image_bytes(update, context)
            except Exception:
                bio = None
            context.user_data.pop("awaiting_strategy", None)
            if not bio:
                await msg.reply_text("⚠️ Не вижу изображения. Пришлите скрин как фото или документ-картинку.")
                return
            await handle_strategy_photo(update, context, image_bytes=bio)
            return  # важно: не сваливаться потом в handle_main

        # 3) Обычное фото/док-картинка/альбом -> трейдерский разбор
        if has_photo:
            await handle_photo(update, context)
            return  # важно: не дублировать меню после ответа

        # 4) Иначе — главное меню
        await _call_if_exists(
            "handle_main",
            update, context,
            fallback_text="Я готов помочь. Выберите действие в меню или пришлите скрин графика для разбора."
        )
        return

    except Exception:
        logging.exception("unified_text_handler failed")
        try:
            await update.effective_message.reply_text("⚠️ Ошибка обработки сообщения. Попробуйте ещё раз.")
        except Exception:
            pass

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

    # ✅ Глобальный bot для уведомлений из вебхуков
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

    # 🔐 Сохранение file_id видео для постов (команда /save_post_video)
    # Работает: ответь командой на видео ИЛИ пришли команду вместе с видео в одном сообщении.
    app.add_handler(CommandHandler("save_post_video", save_post_video, block=False))

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
                InlineKeyboardButton("🔒 VIP‑канал", url="https://t.me/+TAbYnYSzHYI0YzVi")
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









