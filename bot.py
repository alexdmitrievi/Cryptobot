import os
import logging
import asyncio
import threading
import time
import re
import json
import requests
import hmac
import hashlib
import base64
import csv
from datetime import datetime
from io import BytesIO
from bs4 import BeautifulSoup

from telegram import (
    Update, BotCommand, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, ReplyKeyboardRemove
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters, ConversationHandler
)

from config import (
    TELEGRAM_TOKEN, OPENAI_API_KEY, TON_API_TOKEN,
    CRYPTOCLOUD_API_KEY, CRYPTOCLOUD_SHOP_ID, API_SECRET
)
from openai import AsyncOpenAI
from PIL import Image

# 📊 Google Sheets API
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# 🔥 Flask для webhook от CryptoCloud POS
from flask import Flask, request, jsonify

# 🔄 AioCron для еженедельных рассылок
import aiocron

# ✅ Для защиты от rate limit Google Sheets
from tenacity import retry, wait_fixed, stop_after_attempt

client = AsyncOpenAI(api_key=OPENAI_API_KEY)

global_bot = None

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

# 🚀 ALLOWED_USERS с TTL cache
ALLOWED_USERS = set()
ALLOWED_USERS_TIMESTAMP = 0

def get_allowed_users():
    global ALLOWED_USERS, ALLOWED_USERS_TIMESTAMP
    if time.time() - ALLOWED_USERS_TIMESTAMP > 300:
        ALLOWED_USERS = load_allowed_users()
        ALLOWED_USERS_TIMESTAMP = time.time()
    return ALLOWED_USERS

client = AsyncOpenAI(api_key=OPENAI_API_KEY)
logging.basicConfig(level=logging.INFO)

TON_WALLET = "UQC4nBKWF5sO2UIP9sKl3JZqmmRlsGC5B7xM7ArruA61nTGR"
PENDING_USERS = {}
RECEIVED_MEMOS = set()

reply_keyboard = [
    ["💡 Стратегия", "🚀 Сигнал", "🔍 Анализ"],
    ["📖 Обучение", "📚 Термин", "🌱 Психолог"],
    ["🎯 Риск", "💸 Криптообмен"],
    ["💰 Купить", "ℹ️ О боте"],
    ["🔗 Бесплатный доступ через брокера"],
    ["📌 Сетап"]
]
REPLY_MARKUP = ReplyKeyboardMarkup(reply_keyboard, resize_keyboard=True)

CHAT_DISCUSS_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("💬 Обсудить в чате", url="https://t.me/ai4traders_chat")]
])

# Фоновая проверка платежей по username
RECEIVED_MEMOS = set()

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
    # Сохраняем выбранные ранее strategy и market
    keys_to_keep = {"selected_market", "selected_strategy"}
    saved_data = {k: v for k, v in context.user_data.items() if k in keys_to_keep}
    context.user_data.clear()
    context.user_data.update(saved_data)

    message = update.message if update.message else update.callback_query.message

    await message.reply_text(
        "📊 Введи размер депозита в $:",
        reply_markup=ReplyKeyboardMarkup([["↩️ Выйти в меню"]], resize_keyboard=True)
    )
    return RISK_CALC_1


async def risk_calc_deposit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()
    if user_text == "↩️ Выйти в меню":
        context.user_data.clear()
        await update.message.reply_text("🔙 Вернулись в главное меню.", reply_markup=REPLY_MARKUP)
        return ConversationHandler.END

    try:
        context.user_data["deposit"] = float(user_text)
        await update.message.reply_text("💡 Теперь введи процент риска на сделку (%):")
        return RISK_CALC_2
    except ValueError:
        await update.message.reply_text("❗️ Введи число. Пример: 1000")
        return RISK_CALC_1


async def risk_calc_risk_percent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()
    if user_text == "↩️ Выйти в меню":
        context.user_data.clear()
        await update.message.reply_text("🔙 Вернулись в главное меню.", reply_markup=REPLY_MARKUP)
        return ConversationHandler.END

    try:
        context.user_data["risk_percent"] = float(user_text)
        await update.message.reply_text("⚠️ Введи стоп-лосс по сделке (%):")
        return RISK_CALC_3
    except ValueError:
        await update.message.reply_text("❗️ Введи число. Пример: 2")
        return RISK_CALC_2


async def risk_calc_stoploss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()
    if user_text == "↩️ Выйти в меню":
        context.user_data.clear()
        await update.message.reply_text("🔙 Вернулись в главное меню.", reply_markup=REPLY_MARKUP)
        return ConversationHandler.END

    try:
        stoploss_percent = float(user_text)
        deposit = context.user_data["deposit"]
        risk_percent = context.user_data["risk_percent"]

        risk_amount = deposit * risk_percent / 100
        position_size = risk_amount / (stoploss_percent / 100)

        await update.message.reply_text(
            f"✅ Результат:\n"
            f"• Депозит: ${deposit:.2f}\n"
            f"• Риск на сделку: {risk_percent:.2f}% (${risk_amount:.2f})\n"
            f"• Стоп-лосс: {stoploss_percent:.2f}%\n\n"
            f"📌 Рекомендуемый объём позиции: ${position_size:.2f}",
            reply_markup=REPLY_MARKUP
        )
        return ConversationHandler.END

    except ValueError:
        await update.message.reply_text("❗️ Введи число. Пример: 1.5")
        return RISK_CALC_3

async def check_access(update: Update):
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USERS:
        await update.message.reply_text("🔒 Доступ ограничен. Подключи помощника за $25.", reply_markup=REPLY_MARKUP)
        return False
    return True

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "👋 Привет! Ты попал в GPT-Трейдера — инструмент для тех, кто хочет торговать наравне с фондами.\n\n"
        "💥 Сегодня крипту скупают BlackRock, Fidelity и крупнейшие фонды через ETF. "
        "А ты можешь заходить туда же, но без регуляторов и со своим управлением риском.\n\n"
        "🧠 Что умеет GPT-Трейдер:\n"
        "• Делает прогноз по твоему скрину за 10 секунд\n"
        "• Объясняет макро-новости и даёт сценарии\n"
        "• Даёт VIP-сигналы\n"
        "• Поддержит мемом, если рынок побрил 😅",
        reply_markup=REPLY_MARKUP
    )
    await update.message.reply_text(
        "👇 Выбери, что сделать:",
        reply_markup=REPLY_MARKUP
    )
    return ConversationHandler.END

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    logging.info(f"[button_handler] Пользователь {user_id} нажал кнопку: {query.data}")

    if query.data == "start_menu":
        await query.message.reply_text(
            "🚀 Возвращаемся в меню! Выбери, что сделать:",
            reply_markup=REPLY_MARKUP
        )
        return

    if query.data == "market_crypto":
        context.user_data["selected_market"] = "crypto"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🖼 Как правильно сделать скрин", callback_data="screenshot_help")]
        ])
        await query.edit_message_text(
            "📈 Smart Money Concepts (SMC) для крипты\n\n"
            "1️⃣ Сначала включи индикатор LazyScalp Board и проверь, чтобы DV ≥ 300M.\n"
            "2️⃣ Потом отключи LazyScalp и включи два индикатора:\n"
            "- LuxAlgo SMC\n"
            "- Support & Resistance Levels\n\n"
            "📸 Чтобы я выдал максимально точный торговый план:\n"
            "✅ Выбери таймфрейм 4H или 1H\n"
            "✅ Убедись, что на скрине видны:\n"
            "• Уровни BOS и CHoCH\n"
            "• Поддержка и сопротивление\n"
            "• Импульсы цены\n"
            "• Зоны дисбаланса (imbalance)\n\n"
            "📏 Хочешь ещё точнее? Нарисуй вручную горизонтальные уровни и наклонные линии тренда — я это тоже увижу и учту.\n\n"
            "🔽 Пришли скрин — я выдам Entry / Stop / TakeProfit 💰",
            reply_markup=keyboard
        )

    elif query.data == "market_forex":
        context.user_data["selected_market"] = "forex"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🖼 Как правильно сделать скрин", callback_data="screenshot_help")]
        ])
        await query.edit_message_text(
            "📈 Smart Money Concepts (SMC) для форекса\n\n"
            "⚠️ На Forex нет централизованного объёма, поэтому сразу включи два индикатора:\n"
            "- LuxAlgo SMC\n"
            "- Support & Resistance Levels\n\n"
            "📸 Чтобы получить точный разбор:\n"
            "✅ Выбери таймфрейм 4H или 1H\n"
            "✅ Убедись, что на скрине видны:\n"
            "• BOS и CHoCH\n"
            "• Поддержка и сопротивление\n"
            "• Импульсы цены\n"
            "• Зоны дисбаланса\n\n"
            "📏 Рекомендуется вручную добавить горизонтальные уровни и линии тренда — это улучшит точность прогноза.\n\n"
            "🔽 Пришли скрин — я сделаю SMC-анализ и выдам Entry / SL / TP 📊",
            reply_markup=keyboard
        )

    elif query.data == "forecast_by_image":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🖼 Как правильно сделать скрин", callback_data="screenshot_help")]
        ])
        await query.message.reply_text(
            "📸 Пришли скриншот графика — я сделаю технический разбор и прогноз.\n\n"
            "Если не уверен, как его оформить — нажми кнопку ниже:",
            reply_markup=keyboard
        )

    elif query.data == "screenshot_help":
        await query.message.reply_text(
            "📸 Как правильно подготовить скрин для точного анализа:\n\n"
            "1. Таймфрейм:\n"
            "- Используй 4H или 1H. Это лучшие варианты для анализа по SMC.\n\n"
            "2. Цвет фона:\n"
            "- Рекомендуется белый фон. GPT Vision лучше видит уровни и свечи на светлом фоне.\n"
            "- Как изменить: нажми правой кнопкой мыши по графику → Настройки → Вкладка 'Внешний вид' → Измени фон на белый.\n\n"
            "3. Индикаторы:\n"
            "- Включи только два:\n"
            "  • LuxAlgo SMC\n"
            "  • Support & Resistance Levels with Breaks\n"
            "- Удали все остальные (MACD, RSI, объёмы и т.д.)\n\n"
            "4. Что должно быть видно на скрине:\n"
            "- Уровни BOS и CHoCH\n"
            "- Поддержка и сопротивление\n"
            "- Зоны дисбаланса (imbalance)\n"
            "- 2–3 последних импульсных движения цены\n\n"
            "5. Дополнительно:\n"
            "- Можно вручную нарисовать горизонтальные уровни и наклонные линии тренда — это улучшит результат.\n\n"
            "6. Перед скрином:\n"
            "- Убери лишние панели (внизу и сбоку)\n"
            "- Сделай график на весь экран\n\n"
            "7. Скриншот:\n"
            "- Используй кнопку 📷 вверху справа или нажми Windows + Shift + S\n\n"
            "✅ Чем чище и информативнее скрин, тем точнее Entry / Stop / TakeProfit",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("↩️ Вернуться к сигналу", callback_data="back_to_signal")]
            ])
        )

    elif query.data == "back_to_signal":
        context.user_data.pop("selected_market", None)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📉 Crypto", callback_data="market_crypto")],
            [InlineKeyboardButton("💱 Forex", callback_data="market_forex")]
        ])
        await query.message.reply_text(
            "📝 Сначала выбери рынок — нажми одну из кнопок ниже, чтобы я знал, какой анализ тебе нужен:",
            reply_markup=keyboard
        )

    elif query.data == "get_email":
        context.user_data["awaiting_email"] = True
        await query.message.reply_text(
            "✉️ Напиши свой email для получения секретного PDF со стратегиями:"
        )

    elif query.data == "interpret_calendar":
        context.user_data.clear()
        context.user_data["awaiting_calendar_photo"] = True
        await query.message.reply_text(
            "📸 Пришли скриншот из экономического календаря (например, CPI, NFP и т.д.). Я распознаю событие и дам интерпретацию.",
            reply_markup=ReplyKeyboardMarkup([["↩️ Выйти в меню"]], resize_keyboard=True)
        )

    elif query.data == "start_risk_calc":
        keys_to_keep = {"selected_market"}
        saved_data = {k: v for k, v in context.user_data.items() if k in keys_to_keep}
        context.user_data.clear()
        context.user_data.update(saved_data)
        await start_risk_calc(update, context)

    elif query.data == "ref_bybit":
        context.user_data["ref_program"] = "bybit"
        context.user_data["broker"] = "Bybit"
        context.user_data["awaiting_uid"] = True
        await query.message.reply_text(
            "📈 Отлично!\n"
            "Перейди по моей реферальной ссылке и зарегистрируйся на Bybit:\n"
            "👉 https://www.bybit.com/invite?ref=YYVME8\n\n"
            "Внеси депозит от $150 и пришли сюда свой UID для проверки."
        )

    elif query.data == "ref_forex4you":
        context.user_data["ref_program"] = "forex4you"
        context.user_data["broker"] = "Forex4You"
        context.user_data["awaiting_uid"] = True
        await query.message.reply_text(
            "📊 Отлично!\n"
            "Перейди по моей реферальной ссылке и зарегистрируйся на Forex4You:\n"
            "👉 https://www.forex4you.org/?affid=hudpyc9\n\n"
            "Внеси депозит от $200 и пришли сюда свой UID для проверки."
        )

async def grant(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Эта команда доступна только админу.")
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text("⚠ Используй так: /grant user_id username")
        return

    try:
        target_user_id = int(args[0])
        target_username = args[1]

        # Добавляем в ALLOWED_USERS
        ALLOWED_USERS.add(target_user_id)

        # Обновляем TTL, чтобы не слетело при автозагрузке через 5 мин
        global ALLOWED_USERS_TIMESTAMP
        ALLOWED_USERS_TIMESTAMP = time.time()

        # Записываем в Google Sheets
        log_payment(target_user_id, target_username)

        # Уведомляем пользователя
        await notify_user_payment(target_user_id)

        await update.message.reply_text(
            f"✅ Пользователь {target_user_id} ({target_username}) добавлен в VIP и уведомлён."
        )

    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def reload_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Эта команда доступна только админу.")
        return

    try:
        global ALLOWED_USERS
        ALLOWED_USERS = load_allowed_users()
        await update.message.reply_text(
            f"✅ ALLOWED_USERS обновлен. Загружено {len(ALLOWED_USERS)} пользователей из Google Sheets."
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка при обновлении пользователей: {e}")
        logging.error(f"[reload_users] Ошибка: {e}")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    photo = update.message.photo[-1]
    file = await photo.get_file()
    original_photo_bytes = await file.download_as_bytearray()

    image = Image.open(BytesIO(original_photo_bytes)).convert("RGB")
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=80)
    image_base64 = base64.b64encode(buffer.getvalue()).decode()

    selected_market = context.user_data.get("selected_market")
    if not selected_market:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📉 Crypto", callback_data="market_crypto")],
            [InlineKeyboardButton("💱 Forex", callback_data="market_forex")]
        ])
        await update.message.reply_text(
            "📝 Please select the market first:",
            reply_markup=keyboard
        )
        return

    prompt_text = (
        f"You are a world-class Smart Money Concepts (SMC) trader with 10+ years experience in "
        f"{'cryptocurrency' if selected_market == 'crypto' else 'forex'} markets.\n\n"
        "You are skilled in BOS, CHoCH, liquidity grabs, FVG, OTE, mitigation, POI, premium/discount zones.\n"
        "The chart includes only:\n- LuxAlgo SMC\n- Support & Resistance Levels with Breaks\n\n"
        "🎯 Your task: interpret the price action and produce a high-confidence swing trade plan with pending orders.\n\n"
        "✅ FORMAT (your response must be in Russian):\n"
        "1️⃣ Наблюдения (start each with 🔹)\n"
        "2️⃣ План сделки: Entry / StopLoss / TakeProfit\n"
        "3️⃣ Комментарий по риску\n"
        "4️⃣ Смещение (BUY или SELL)\n"
        "✅ Заверши двумя строками резюме с эмодзи (e.g. «Покупка из дисконта 📈🟢»)\n\n"
        "📌 Rules:\n"
        "- Risk/Reward must be at least 1.5. If < 3.0, explain why it's acceptable.\n"
        "- Entry must be realistic and reachable.\n"
        "- Choose BUY or SELL (not both). Justify.\n"
        "- Prefer confirmation-based entries.\n\n"
        "🚫 ABSOLUTE:\n"
        "- Reply STRICTLY in Russian\n"
        "- No markdown\n"
        "- No apologies or refusal\n"
        "- Analysis is MANDATORY. If structure unclear — estimate from candles, BOS/CHoCH hints, zones."
    )

    analysis = ""
    for attempt in range(2):
        try:
            enhanced_prompt = prompt_text
            if attempt == 1:
                enhanced_prompt += (
                    "\n\n🚨 SECOND ATTEMPT: You MUST give Entry, StopLoss and TakeProfit even if chart lacks clarity. "
                    "Estimate from candles and structure. Never say 'I can't'. Response MUST be in Russian. Format required."
                )

            vision_response = await client.chat.completions.create(
                model="gpt-4o",
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": enhanced_prompt},
                        {"type": "image_url", "image_url": {
                            "url": f"data:image/jpeg;base64,{image_base64}"
                        }}
                    ]
                }],
                max_tokens=1000
            )

            message_obj = vision_response.choices[0].message
            analysis = message_obj.content.strip() if message_obj and message_obj.content else ""

            if "can't assist" in analysis.lower() or "sorry" in analysis.lower() or len(analysis) < 100:
                analysis = ""
                continue
            if analysis:
                break

        except Exception as e:
            logging.error(f"[handle_photo GPT error] {e}")
            continue

    if not analysis:
        await update.message.reply_text(
            "⚠️ GPT не смог проанализировать этот скрин.\n\n"
            "Проверь следующее:\n"
            "• Сделай фон графика белым\n"
            "• Удали все лишние индикаторы\n"
            "• Убедись, что видны BOS, CHoCH, зоны ликвидности и структура\n\n"
            "📸 Затем отправь скрин снова."
        )
        return

    def parse_price(raw_text):
        try:
            return float(raw_text.replace(" ", "").replace(",", ".").replace("$", ""))
        except:
            return None

    entry_match = re.search(r'(Entry|Вход)[:\s]*\$?\s*([\d\s,.]+)', analysis, flags=re.IGNORECASE) \
        or re.search(r'🎯[:\s]*\$?\s*([\d\s,.]+)', analysis)
    stop_match = re.search(r'(StopLoss|Стоп)[:\s]*\$?\s*([\d\s,.]+)', analysis, flags=re.IGNORECASE) \
        or re.search(r'🚨[:\s]*\$?\s*([\d\s,.]+)', analysis)
    tp_match = re.search(r'(TakeProfit|Тейк)[:\s]*\$?\s*([\d\s,.]+)', analysis, flags=re.IGNORECASE) \
        or re.search(r'💰[:\s]*\$?\s*([\d\s,.]+)', analysis)
    bias_match = re.search(r'\b(BUY|SELL|ПОКУПКА|ПРОДАЖА)\b', analysis, flags=re.IGNORECASE)

    entry = parse_price(entry_match.group(2) if entry_match and entry_match.lastindex == 2 else entry_match.group(1)) if entry_match else None
    stop = parse_price(stop_match.group(2) if stop_match and stop_match.lastindex == 2 else stop_match.group(1)) if stop_match else None
    tp = parse_price(tp_match.group(2) if tp_match and tp_match.lastindex == 2 else tp_match.group(1)) if tp_match else None
    direction = bias_match.group(1).upper() if bias_match else None

    rr_line, risk_line, tldr, unrealistic_note = "", "", "", ""
    rr_ratio = None

    if entry and stop:
        risk_abs = abs(entry - stop)
        risk_pct = abs((entry - stop) / entry * 100)
        risk_line = f"📌 Область риска ≈ ${risk_abs:.4f} ({risk_pct:.2f}%)"
    else:
        risk_line = "📌 Область риска не указана — проверь вручную."

    if entry and stop and tp and (entry != stop):
        rr_ratio = abs((tp - entry) / (entry - stop))
        rr_line = f"📊 R:R ≈ {rr_ratio:.2f}"
        if rr_ratio < 1.5:
            rr_line += "\n⚠️ R:R ниже 1.5 — вход может быть неэффективным."
        elif rr_ratio < 3.0:
            rr_line += "\n⚠️ R:R ниже 3.0 — допустимо, если структура сильная."

    if direction and entry and tp:
        if direction in ["SELL", "ПРОДАЖА"] and entry < tp:
            unrealistic_note = "⚠️ Entry ниже тейка при SELL — возможна ошибка или цена уже прошла."
        elif direction in ["BUY", "ПОКУПКА"] and entry > tp:
            unrealistic_note = "⚠️ Entry выше тейка при BUY — проверь логику."

    bias_line = f"📈 Направление сделки: {direction}" if direction else ""

    if entry and stop and tp:
        tldr = f"✅ TL;DR: Вход {entry}, стоп {stop}, тейк {tp}."
        if rr_ratio:
            tldr += f" 📊 R:R ≈ {rr_ratio:.2f}"
    else:
        tldr = "✅ Краткий план не сформирован — проверь вход/стоп/тейк."

    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("📏 Рассчитать риск", callback_data="start_risk_calc")]])

    full_message = f"📉 Анализ графика по SMC:\n\n{analysis}\n\n{risk_line}"
    if rr_line:
        full_message += f"\n{rr_line}"
    if bias_line:
        full_message += f"\n{bias_line}"
    if unrealistic_note:
        full_message += f"\n{unrealistic_note}"
    full_message += f"\n\n{tldr}"

    await update.message.reply_text(full_message, reply_markup=keyboard)

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


async def help_invest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USERS:
        await update.message.reply_text(
            "🔒 Доступ только после активации подписки за $25.",
            reply_markup=REPLY_MARKUP
        )
        return

    context.user_data.clear()
    context.user_data["awaiting_invest_question"] = True
    await update.message.reply_text(
        "💡 Напишите, какую стратегию для инвестирования вы хотите получить (например: «хочу консервативный портфель на 3 года» или «куда вложить $5000 с высоким риском на полгода»)."
    )
    return

async def handle_invest_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()

    # 🚪 Выход в главное меню по кнопке
    if user_text == "↩️ Выйти в меню":
        context.user_data.pop("awaiting_invest_question", None)
        await update.message.reply_text(
            "🔙 Ты вышел из режима стратегий. Возвращаемся в главное меню.",
            reply_markup=REPLY_MARKUP
        )
        return

    user_id = update.effective_user.id

    # 🪝 Fetch BTC & ETH prices
    try:
        btc_data = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT").json()
        eth_data = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=ETHUSDT").json()
        btc_price = float(btc_data["price"])
        eth_price = float(eth_data["price"])
    except Exception as e:
        logging.error(f"[handle_invest_question] Binance price fetch error: {e}")
        btc_price = eth_price = None

    # 📝 Build advanced prompt
    prompt = (
        "You are a premier investment strategist with over 20 years of expertise in managing multi-asset portfolios. "
        "You specialize in creating highly personalized investment plans specifically for Russian clients, "
        "leveraging three primary asset classes: cryptocurrencies, Forex, and Moscow Exchange (MOEX) instruments. "
        "You also include gold and silver only via MOEX futures or unallocated metal accounts.\n\n"
        
        f"The client's question or goal is:\n{user_text}\n\n"
        
        "📌 For context, here are current prices:\n"
        f"{('- BTC: $' + str(btc_price)) if btc_price else ''}\n"
        f"{('- ETH: $' + str(eth_price)) if eth_price else ''}\n\n"

        "🚀 Your task is to craft an ultra-detailed, step-by-step personalized investment strategy that feels like a high-end private consultation. "
        "Respond entirely in Russian. Use a warm, human, beginner-friendly tone with plenty of emojis. "
        "Always write in short paragraphs (1-3 sentences), never use markdown (no asterisks or underscores). "
        "Immediately explain any financial term in simple words.\n\n"

        "Structure your response exactly as follows:\n\n"

        "1️⃣ 👀 Client profile\n"
        "- Estimate their investment horizon (short, medium, long-term) and risk appetite (aggressive, moderate, conservative) with a short justification.\n"
        "- State their primary goal: capital growth, protecting savings, or passive income.\n\n"

        "2️⃣ 📊 Recommended portfolio breakdown\n"
        "- Divide the portfolio across three groups: crypto (BTC, ETH, USDT), Forex (e.g. EUR/USD, GBP/USD), and MOEX instruments (stocks like Sberbank, Gazprom, OFZ bonds, FinEx ETFs), plus gold and silver.\n"
        "- Provide approximate percentages for each category and explain in plain language why each is included.\n"
        "- Emphasize the protective role of gold and silver during uncertainty.\n\n"

        "3️⃣ 💎 Risk management & DCA tactics\n"
        "- Explain dollar-cost averaging (buying in portions), when to take partial profits (like after +20-30%), and why simple stop-losses matter.\n\n"

        "4️⃣ 🌍 Macro & local risks\n"
        "- List key macroeconomic and geopolitical risks for Russian investors: Central Bank rates, inflation, RUB volatility, global tensions.\n"
        "- Describe how this portfolio structure helps reduce these risks.\n\n"

        "5️⃣ 🚀 Immediate action plan\n"
        "- Clearly tell the client what to do right now: open a MOEX brokerage account, activate Forex, register on a crypto exchange.\n"
        "- How to set up auto-deposits or partial buys.\n"
        "- How often to review the portfolio (e.g. every 3-6 months) and what to watch for.\n\n"

        "6️⃣ 📈 Scenario playbook\n"
        "- If markets rise: where to lock in some gains and when adding more makes sense.\n"
        "- If markets drop: why not to panic-sell and how to average down or hold.\n\n"

        "7️⃣ ✅ Short conclusion\n"
        "- Finish with 2-3 lines in Russian using emojis. Example:\n"
        "🚀 Стратегия на 3+ года, сбалансированный риск и золотая подушка. Пересмотр дважды в год — строим капитал шаг за шагом.\n\n"

        "⚠️ VERY IMPORTANT: Write everything in Russian, be extremely clear, use emojis in every block, "
        "avoid English jargon without instant translation, separate sections with empty lines for easy Telegram reading."
    )

    try:
        gpt_response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "user", "content": prompt}
            ],
            max_tokens=1000
        )

        analysis = gpt_response.choices[0].message.content.strip()
        if not analysis:
            await update.message.reply_text(
                "⚠️ GPT не дал ответа. Попробуй задать вопрос ещё раз.",
                reply_markup=ReplyKeyboardMarkup([["↩️ Выйти в меню"]], resize_keyboard=True)
            )
            return

        await update.message.reply_text(
            f"💼 Вот твоя персональная инвестиционная стратегия:\n\n{analysis}",
            reply_markup=ReplyKeyboardMarkup([["↩️ Выйти в меню"]], resize_keyboard=True)
        )

    except Exception as e:
        logging.error(f"[handle_invest_question] GPT error: {e}")
        await update.message.reply_text(
            "⚠️ GPT временно недоступен.\n\n"
            "На глаз: для умеренного риска обычно делают так 📊\n"
            "- 40-50% акции MOEX (Сбер, Газпром и др.)\n"
            "- 20-30% облигации ОФЗ\n"
            "- 10-15% золото и серебро через MOEX или ОМС\n"
            "- 10-15% крипта (BTC, ETH)\n"
            "- и часть в Forex (EUR/USD, GBP/USD) для валютной подушки.\n\n"
            "📝 Пересматривай портфель раз в 6 месяцев, усредняй покупки частями и фиксируй часть прибыли при росте +20-30%.\n"
            "Детальнее подскажу после восстановления сервиса!",
            reply_markup=ReplyKeyboardMarkup([["↩️ Выйти в меню"]], resize_keyboard=True)
        )
        context.user_data.clear()

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

async def teacher_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()

    # 🚪 Выход в меню по кнопке
    if user_text == "↩️ Выйти в меню":
        context.user_data.pop("awaiting_teacher_question", None)
        await update.message.reply_text(
            "🔙 Ты вышел из режима обучения. Возвращаемся в главное меню.",
            reply_markup=REPLY_MARKUP
        )
        return

    # GPT-промпт
    prompt = (
        "You are a professional trading and investing teacher with over 20 years of experience "
        "across cryptocurrency, forex, stock, and commodity markets. "
        "You have taught both retail traders and institutional clients. "
        "Your explanations are extremely clear, structured, and use simple language. "
        "You immediately explain any jargon with practical examples. "
        "You are patient and willing to break down complex ideas into simple terms.\n\n"
        f"Student's question:\n{user_text}\n\n"
        "Break your answer into structured steps with empty lines after each step or paragraph.\n\n"
        "Use emojis to visually anchor each section (like ➡️, ⚠️, ✅, 📈), but do NOT use asterisks or any Markdown-style bold or italics.\n\n"
        "Keep each paragraph short (1-3 sentences max) for easy reading in Telegram.\n\n"
        "1️⃣ Start with a short, direct thesis that answers the main question.\n\n"
        "2️⃣ Provide a detailed step-by-step explanation, with a blank line after each step.\n\n"
        "3️⃣ Include one example from the crypto market and one from forex or stocks.\n\n"
        "4️⃣ Point out the most common mistakes beginners make in this situation and how to avoid them.\n\n"
        "5️⃣ End with a short, practical tip (1-2 sentences) that the student can apply right now.\n\n"
        "⚠️ Never use empty words like 'maybe' or 'probably' without justification. "
        "Avoid clichés like 'don't worry' or 'everything will be fine'. "
        "Justify each conclusion with logic or examples.\n\n"
        "Respond STRICTLY in Russian."
    )

    try:
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}]
        )

        reply_markup = ReplyKeyboardMarkup([["↩️ Выйти в меню"]], resize_keyboard=True)

        # Защита от пустого ответа или структуры
        if not response.choices or not response.choices[0].message or not response.choices[0].message.content:
            await update.message.reply_text(
                "⚠️ GPT не дал ответа. Попробуй задать вопрос ещё раз.",
                reply_markup=reply_markup
            )
            return

        text = response.choices[0].message.content.strip()
        await update.message.reply_text(
            f"📖 Обучение:\n\n{text}",
            reply_markup=reply_markup
        )

    except Exception as e:
        logging.error(f"[TEACHER_RESPONSE] GPT error: {e}", exc_info=True)
        await update.message.reply_text(
            "⚠️ GPT временно недоступен. Попробуй позже.",
            reply_markup=ReplyKeyboardMarkup([["↩️ Выйти в меню"]], resize_keyboard=True)
        )

async def handle_definition(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    text = update.message.text.strip()
    user_id = update.effective_user.id

    logging.info(f"[handle_main] Пользователь {user_id} нажал кнопку: {text}")

    # 🚪 Проверка доступа
    if user_id not in ALLOWED_USERS and text not in ["💰 Купить", "ℹ️ О боте", "🔗 Бесплатный доступ через брокера"]:
        await update.message.reply_text(
            "🔒 Доступ только после активации подписки за $25 или через брокера.",
            reply_markup=REPLY_MARKUP
        )
        return

    # 💡 Стратегия
    if text == "💡 Стратегия":
        context.user_data["awaiting_invest_question"] = True
        await update.message.reply_text(
            "✍️ Напиши свой вопрос или опиши свою инвестиционную цель, "
            "чтобы я составил стратегию с учётом текущих цен BTC/ETH и рекомендациями по диверсификации.",
            reply_markup=ReplyKeyboardMarkup([["↩️ Выйти в меню"]], resize_keyboard=True)
        )
        return

    # 🎯 Риск
    if text == "🎯 Риск":
        return await start_risk_calc(update, context)

    # 🌱 Психолог
    if text == "🌱 Психолог":
        return await start_therapy(update, context)

    # 🔍 Анализ
    if text == "🔍 Анализ":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Экономический календарь", callback_data="interpret_calendar")]
        ])
        await update.message.reply_text(
            "🗓 Хочешь получить интерпретацию по скриншоту из экономического календаря (например, CPI, NFP)? Пришли изображение события.",
            reply_markup=keyboard
        )
        return

    # 📖 Обучение
    if text == "📖 Обучение":
        context.user_data["awaiting_teacher_question"] = True
        await update.message.reply_text(
            "✍️ Напиши свой вопрос — я отвечу как преподаватель с 20+ годами опыта в трейдинге и инвестициях.",
            reply_markup=ReplyKeyboardMarkup([["↩️ Выйти в меню"]], resize_keyboard=True)
        )
        return

    # 📚 Термин
    if text == "📚 Термин":
        context.user_data["awaiting_definition_term"] = True
        await update.message.reply_text(
            "✍️ Напиши термин, который нужно объяснить.",
            reply_markup=ReplyKeyboardMarkup([["↩️ Выйти в меню"]], resize_keyboard=True)
        )
        return

    # 🚀 Сигнал
    if text == "🚀 Сигнал":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💎 Crypto", callback_data="market_crypto")],
            [InlineKeyboardButton("💱 Forex", callback_data="market_forex")]
        ])
        await update.message.reply_text(
            "⚡ Для какого рынка сделать анализ?",
            reply_markup=keyboard
        )
        return

    # 💸 Криптообмен
    if text == "💸 Криптообмен":
        await update.message.reply_text(
            "💸 Криптообмен без риска\n\n"
            "⚖️ Легально, быстро и прозрачно — мы производим обмен криптовалюты в 17 регионах России. "
            "Все средства имеют чистое и официальное происхождение.\n\n"
            "✅ Без скрытых комиссий\n"
            "🚀 Моментальные сделки\n"
            "💰 Деньги сразу к вам в руки или на счёт\n"
            "🔥 Полная конфиденциальность и защита данных\n\n"
            "Хочешь выгодно и безопасно обменять крипту?\n"
            "✍️ Напиши мне прямо сейчас 👉 @zhbankov_alex",
            reply_markup=ReplyKeyboardMarkup([["↩️ Вернуться в меню"]], resize_keyboard=True)
        )
        return

    # 💰 Купить
    if text == "💰 Купить":
        if user_id in ALLOWED_USERS:
            await update.message.reply_text(
                "✅ У тебя уже активирована подписка!",
                reply_markup=REPLY_MARKUP
            )
        else:
            await send_payment_link(update, context)
        return

    # ℹ️ О боте
    if text == "ℹ️ О боте":
        await update.message.reply_text(
            "Подписка активируется через CryptoCloud или через регистрацию у брокера.\n"
            "Нажми 💰 Купить для оплаты или 🔗 Бесплатный доступ через брокера.",
            reply_markup=REPLY_MARKUP
        )
        return

    # 🔗 Бесплатный доступ через брокера
    if text == "🔗 Бесплатный доступ через брокера":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Bybit", callback_data="ref_bybit")],
            [InlineKeyboardButton("Forex4You", callback_data="ref_forex4you")]
        ])
        await update.message.reply_text(
            "🚀 Выберите брокера для регистрации по моей реферальной ссылке:\n"
            "- Для Bybit минимальный депозит $150\n"
            "- Для Forex4You минимальный депозит $200\n\n"
            "После регистрации пришлите сюда свой UID для проверки.",
            reply_markup=keyboard
        )
        return

    # 📌 Сетап
    if text == "📌 Сетап":
        if user_id not in ADMIN_IDS:
            await update.message.reply_text("⛔️ Эта функция доступна только админу.")
            return
        await update.message.reply_text("✍️ Укажи торговый инструмент (например: BTC/USDT):")
        return SETUP_1

    # ✅ Обработка открытых диалогов для всех режимов
    if context.user_data.get("awaiting_invest_question"):
        return await handle_invest_question(update, context)
    if context.user_data.get("awaiting_teacher_question"):
        return await teacher_response(update, context)
    if context.user_data.get("awaiting_definition_term"):
        return await handle_definition_term(update, context)
    if context.user_data.get("awaiting_news"):
        return await generate_news_interpretation(update, context)
    if context.user_data.get("awaiting_therapy_input"):
        return await gpt_psychologist_response(update, context)
    if context.user_data.get("awaiting_uid"):
        return await handle_uid_submission(update, context)

    # ↩️ Вернуться в меню (универсальный выход)
    if text in ["↩️ Вернуться в меню", "↩️ Выйти в меню"]:
        context.user_data.clear()
        await update.message.reply_text(
            "🔙 Вернулись в главное меню.",
            reply_markup=REPLY_MARKUP
        )
        return

    # 🔄 Если ничего не ожидаем - сброс
    saved_data = {k: v for k, v in context.user_data.items() if k in ("selected_market", "selected_strategy")}
    context.user_data.clear()
    context.user_data.update(saved_data)
    await update.message.reply_text(
        "🔄 Сброс всех ожиданий. Продолжай.",
        reply_markup=REPLY_MARKUP
    )

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

# 🚀 Функция генерации ссылки POS для Telegram
async def send_payment_link(update, context):
    user_id = update.effective_user.id
    pay_link = (
        f"https://pay.cryptocloud.plus/pos/{CRYPTOCLOUD_SHOP_ID}"
        f"?amount=25&currency=USDT&network=TRC20&order_id=user_{user_id}&desc=GPT_Trader_Bot"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Оплатить через CryptoCloud", url=pay_link)]
    ])
    await update.message.reply_text(
        "💵 Перейдите по кнопке для оплаты подписки GPT Trader Bot:",
        reply_markup=keyboard
    )

# 🚀 Flask webhook для IPN от POS с проверкой HMAC
app_flask = Flask(__name__)

# ✅ Healthcheck endpoint
@app_flask.route("/")
def index():
    return jsonify({"status": "ok", "allowed_users": len(get_allowed_users())})

# ✅ Webhook от CryptoCloud
@app_flask.route("/cryptocloud_webhook", methods=["POST"])
def cryptocloud_webhook():
    body = request.get_data()
    signature = request.headers.get("X-Signature-SHA256")
    calc_sig = hmac.new(API_SECRET.encode(), body, hashlib.sha256).hexdigest()

    if signature != calc_sig:
        logging.warning(f"⚠ Неверная подпись IPN: {signature} != {calc_sig}")
        return jsonify({"status": "invalid signature"})

    data = request.json
    logging.info(f"✅ IPN от CryptoCloud:\n{json.dumps(data, indent=2, ensure_ascii=False)}")

    if data.get("status") == "paid":
        order_id = data.get("order_id")
        if order_id and order_id.startswith("user_"):
            try:
                user_id = int(order_id.split("_")[1])
            except (IndexError, ValueError):
                logging.error(f"❌ Ошибка парсинга user_id в order_id: {order_id}")
                return jsonify({"status": "bad order_id"})

            username = order_id.split("_")[2] if len(order_id.split("_")) > 2 else ""

            # ✅ Добавляем пользователя в кеш
            ALLOWED_USERS.add(user_id)
            # ✅ Записываем в Google Sheets
            safe_append_row([str(user_id), username, datetime.now().strftime("%Y-%m-%d %H:%M:%S")])

            # ✅ Уведомляем через Telegram
            asyncio.run_coroutine_threadsafe(
                notify_user_payment(user_id),
                app_flask.loop
            )
            logging.info(f"🎉 Пользователь {user_id} ({username}) активирован через POS!")

    return jsonify({"ok": True})

# 🚀 Запуск Flask в отдельном потоке с loop
def run_flask(loop):
    app_flask.loop = loop
    port = int(os.environ.get("PORT", 5000))
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

    caption = (
        "🚀 *GPT-Трейдер* — твой Telegram-ассистент для рынка крипты и форекса.\n\n"
        "📊 Что делает бот?\n"
        "• Находит входы, стопы и цели по твоим скринам за 10 секунд\n"
        "• Анализирует BOS, ликвидность, пробои, OTE (по Smart Money)\n"
        "• Строит сценарии на 1-2 дня, на неделю и на месяц\n"
        "• Делает макро-анализ после новостей (ФРС, ETF, хардфорки)\n"
        "• Учит money-management и помогает пережить минусы через GPT-психолога 😅\n\n"
        "🎯 Плюс:\n"
        "• VIP-сетапы с уровнями, которые публикуем в канал\n"
        "• Курс по скальпингу и позиционке (10+ уроков и PDF)\n\n"
        "🚀 *Подключи GPT-Трейдера всего за $25 и получи доступ навсегда.*\n\n"
        "💰 Не плати каждый месяц — активируй один раз и используй сколько хочешь.\n\n"
        "💬 Задай вопрос 👉 [@zhbankov_alex](https://t.me/zhbankov_alex)\n"
        "👥 Чат для трейдеров 👉 [ai4traders_chat](https://t.me/ai4traders_chat)"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Получить доступ", url="https://t.me/Cripto_inter_bot")]
    ])

    try:
        chat_id = '@ai4traders'
        # Убираем старый закреп, если есть
        old_pins = await context.bot.get_chat(chat_id)
        if old_pins.pinned_message:
            await context.bot.unpin_chat_message(chat_id=chat_id, message_id=old_pins.pinned_message.message_id)

        # Публикуем новый пост
        with open(PHOTO_PATH, "rb") as photo:
            message = await context.bot.send_photo(
                chat_id=chat_id,
                photo=photo,
                caption=caption,
                parse_mode="Markdown",
                reply_markup=keyboard
            )

        # Закрепляем
        await context.bot.pin_chat_message(
            chat_id=chat_id,
            message_id=message.message_id,
            disable_notification=True
        )

        await update.message.reply_text("✅ Пост опубликован и закреплён в канале с кнопкой для перехода в твоего бота.")
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
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Эта команда доступна только админу.")
        return

    try:
        records = sheet.get_all_records()
        total_records = len(records)
        allowed_count = len(ALLOWED_USERS)

        last_entry = records[-1] if records else {}

        msg = (
            f"📊 Статистика:\n\n"
            f"• Подписчиков в ALLOWED_USERS: {allowed_count}\n"
            f"• Всего записей в Google Sheets: {total_records}\n\n"
            f"📝 Последняя запись:\n"
            f"{json.dumps(last_entry, ensure_ascii=False, indent=2)}"
        )
        await update.message.reply_text(msg)
    except Exception as e:
        logging.error(f"[STATS] Ошибка: {e}")
        await update.message.reply_text("⚠️ Не удалось получить статистику.")

async def export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Эта команда доступна только админу.")
        return

    try:
        records = sheet.get_all_records()

        from io import StringIO
        csv_file = StringIO()
        writer = csv.DictWriter(csv_file, fieldnames=["user_id", "username", "email", "date"])
        writer.writeheader()
        for row in records:
            writer.writerow({
                "user_id": row.get("user_id", ""),
                "username": row.get("username", ""),
                "email": row.get("email", ""),
                "date": row.get("date", "")
            })

        csv_file.seek(0)
        await update.message.reply_document(
            document=("users_export.csv", csv_file.getvalue()),
            filename="users_export.csv",
            caption="📥 Все пользователи и email из Google Sheets"
        )
    except Exception as e:
        logging.error(f"[EXPORT] Ошибка: {e}")
        await update.message.reply_text("⚠️ Не удалось выгрузить пользователей.")

async def unified_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    # ✅ Явная проверка на текстовые "/start" и "/restart"
    if text == "/start":
        await start(update, context)
        return
    elif text == "/restart":
        await restart(update, context)
        return

    # ✅ Блок обработки email
    if context.user_data.get("awaiting_email"):
        email = text
        if "@" in email and "." in email:
            try:
                sheet.append_row([
                    str(update.effective_user.id),
                    update.effective_user.username or "",
                    email
                ])
                await update.message.reply_text(
                    "✅ Email сохранён! Бонус придёт в ближайшее время."
                )
            except Exception as e:
                logging.error(f"[EMAIL_SAVE] {e}")
                await update.message.reply_text(
                    "⚠️ Не удалось сохранить. Попробуй позже."
                )
        else:
            await update.message.reply_text(
                "❌ Похоже, это не email. Попробуй снова."
            )
            return
        context.user_data.pop("awaiting_email", None)
        return

    # ✅ Остальные режимы
    if context.user_data.get("awaiting_potential"):
        await handle_potential(update, context)
    elif context.user_data.get("awaiting_definition_term"):
        await handle_definition(update, context)
    elif context.user_data.get("awaiting_invest_question"):
        await handle_invest_question(update, context)
    elif context.user_data.get("awaiting_teacher_question"):
        await teacher_response(update, context)
    else:
        await handle_main(update, context)

async def restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("🔄 Бот перезапущен. Выбери действие:", reply_markup=REPLY_MARKUP)
    return ConversationHandler.END

async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand("start", "Запустить бота"),
        BotCommand("restart", "🔁 Перезапустить бота")
    ])

def main():
    global global_bot

    # 🚀 Главный asyncio loop
    loop = asyncio.get_event_loop()

    # 🚀 Flask webhook (для CryptoCloud) в отдельном потоке
    threading.Thread(target=run_flask, args=(loop,)).start()

    # ✅ Инициализация Telegram бота
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    logging.info("🚀 GPT-Трейдер стартовал!")

    # ✅ Глобальный bot для уведомлений
    global_bot = app.bot

    # ✅ Глобальный error handler
    async def error_handler(update, context):
        logging.error(f"❌ Exception: {context.error}")
        if update and update.message:
            await update.message.reply_text("⚠️ Произошла внутренняя ошибка. Попробуйте позже.")
    app.add_error_handler(error_handler)

    # 🔄 Еженедельная рассылка
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

    # 🧘 GPT-Психолог
    therapy_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🧘 Спокойствие$"), start_therapy)],
        states={WAITING_FOR_THERAPY_INPUT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, gpt_psychologist_response)
        ]},
        fallbacks=[
            CommandHandler("start", start, block=False),
            CommandHandler("restart", restart, block=False),
            MessageHandler(filters.Regex("^🔄 Перезапустить бота$"), restart)
        ]
    )

    # 📏 Калькулятор риска
    risk_calc_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^📏 Калькулятор риска$"), start_risk_calc),
            CallbackQueryHandler(start_risk_calc, pattern="^start_risk_calc$")
        ],
        states={
            RISK_CALC_1: [MessageHandler(filters.TEXT & ~filters.COMMAND, risk_calc_deposit)],
            RISK_CALC_2: [MessageHandler(filters.TEXT & ~filters.COMMAND, risk_calc_risk_percent)],
            RISK_CALC_3: [MessageHandler(filters.TEXT & ~filters.COMMAND, risk_calc_stoploss)],
        },
        fallbacks=[
            CommandHandler("start", start, block=False),
            CommandHandler("restart", restart, block=False),
            MessageHandler(filters.Regex("^🔄 Перезапустить бота$"), restart)
        ]
    )

    # 📌 Сетап
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
            MessageHandler(filters.Regex("^🔄 Перезапустить бота$"), restart)
        ]
    )

    # ✅ Стандартные команды
    app.add_handler(CommandHandler("start", start, block=False))
    app.add_handler(CommandHandler("restart", restart, block=False))
    app.add_handler(CommandHandler("publish", publish_post, block=False))
    app.add_handler(CommandHandler("broadcast", broadcast, block=False))
    app.add_handler(CommandHandler("grant", grant, block=False))
    app.add_handler(CommandHandler("reload_users", reload_users, block=False))
    app.add_handler(CommandHandler("stats", stats, block=False))
    app.add_handler(CommandHandler("export", export, block=False))

    # ✅ ConversationHandlers
    app.add_handler(therapy_handler)
    app.add_handler(risk_calc_handler)
    app.add_handler(setup_handler)

    # ✅ CallbackQuery, фото и текстовые
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_main))

    # 🚀 Запуск polling
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
            [InlineKeyboardButton("🎯 Пригласить друга и получить бонус", url="https://твоя_реферальная_страница.com")]
        ])

        await global_bot.send_message(
            chat_id=user_id,
            text=(
                "✅ Оплата получена! Подписка активирована навсегда 🎉\n\n"
                "🤖 GPT-помощник доступен: задавай вопросы, загружай графики, получай прогнозы.\n\n"
                "📢 Также теперь тебе открыт доступ к закрытому каналу с VIP-сигналами:\n"
                "👉 [Перейти в VIP-канал](https://t.me/+your_invite_hash)\n\n"
                "🎁 Твой бонус — курс по скальпингу и позиционке:\n"
                "👉 [Открыть курс в Google Drive](https://drive.google.com/drive/folders/1EEryIr4RDtqM4WyiMTjVP1XiGYJVxktA?clckid=3f56c187)\n\n"
                "🎯 Поделись с другом и получи секретный PDF по стратегиям!"
            ),
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        logging.info(f"📩 Уведомление отправлено пользователю {user_id}")
    except Exception as e:
        logging.error(f"❌ Не удалось уведомить пользователя {user_id}: {e}")

if __name__ == '__main__':
    main()











