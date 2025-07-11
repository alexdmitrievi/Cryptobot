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

# ✅ Подключение к Google Sheets
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds_dict = json.loads(os.getenv("GOOGLE_CREDS"))

# 🔐 Исправляем переносы строк в приватном ключе
creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")

creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
gc = gspread.authorize(creds)

SPREADSHEET_ID = "1s_KQLyekb-lQjt3fMlBO39CTBuq0ayOIeKkXEhDjhbs"
sheet = gc.open_by_key(SPREADSHEET_ID).sheet1

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

# 🚀 Загружаем пользователей с подпиской при старте
ALLOWED_USERS = load_allowed_users()

client = AsyncOpenAI(api_key=OPENAI_API_KEY)
logging.basicConfig(level=logging.INFO)

TON_WALLET = "UQC4nBKWF5sO2UIP9sKl3JZqmmRlsGC5B7xM7ArruA61nTGR"
PENDING_USERS = {}
RECEIVED_MEMOS = set()

reply_keyboard = [
    ["💡 Стратегия", "🚀 Сигнал", "🔍 Анализ"],
    ["📖 Обучение", "🌱 Психолог"],
    ["📚 Термин", "🎯 Риск"],
    ["💰 Купить", "ℹ️ О боте"],
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
    context.user_data.clear()
    await update.message.reply_text(
        "📊 Введи размер депозита в $:",
        reply_markup=REPLY_MARKUP
    )
    return RISK_CALC_1

async def risk_calc_deposit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["deposit"] = float(update.message.text.strip())
        await update.message.reply_text("💡 Теперь введи процент риска на сделку (%):")
        return RISK_CALC_2
    except ValueError:
        await update.message.reply_text("❗️ Введи число. Пример: 1000")
        return RISK_CALC_1

async def risk_calc_risk_percent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["risk_percent"] = float(update.message.text.strip())
        await update.message.reply_text("⚠️ Введи стоп-лосс по сделке (%):")
        return RISK_CALC_3
    except ValueError:
        await update.message.reply_text("❗️ Введи число. Пример: 2")
        return RISK_CALC_2

async def risk_calc_stoploss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        stoploss_percent = float(update.message.text.strip())
        deposit = context.user_data["deposit"]
        risk_percent = context.user_data["risk_percent"]

        risk_amount = deposit * risk_percent / 100
        position_size = risk_amount / (stoploss_percent / 100)

        await update.message.reply_text(
            f"✅ Результат:\n"
            f"• Депозит: ${deposit:.2f}\n"
            f"• Риск на сделку: {risk_percent:.2f}% (${risk_amount:.2f})\n"
            f"• Стоп-лосс: {stoploss_percent:.2f}%\n\n"
            f"📌 Рекомендуемый объём позиции: **${position_size:.2f}**",
            reply_markup=REPLY_MARKUP,
            parse_mode="Markdown"
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


async def help_pro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return ConversationHandler.END
    context.user_data.clear()  # <— добавь это
    await update.message.reply_text("Ты хочешь интерпретировать новость? (да/нет)", reply_markup=ReplyKeyboardRemove())
    return INTERPRET_NEWS

async def interpret_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    if text == "да":
        await update.message.reply_text("Что за новость?")
        return ASK_EVENT
    elif text == "нет":
        await update.message.reply_text("Хорошо. Для точной консультации ответь на несколько вопросов.\n\n1. Твой стиль торговли? (скальпинг, позиционка или инвестиции)")
        return FOLLOWUP_1
    else:
        await update.message.reply_text("Пожалуйста, ответь 'да' или 'нет'")
        return INTERPRET_NEWS

async def followup_strategy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["style"] = update.message.text.strip()
    await update.message.reply_text("2. На каком таймфрейме ты чаще всего открываешь сделки?")
    return FOLLOWUP_2

async def followup_timeframe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["timeframe"] = update.message.text.strip()
    await update.message.reply_text("3. На каком рынке ты торгуешь? (крипта, форекс, фондовый, сырьё и т.д.)")
    return FOLLOWUP_3

async def followup_market(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["market"] = update.message.text.strip()
    await update.message.reply_text("Отлично. Теперь можешь задать свой вопрос:")
    return GENERAL_QUESTION

async def ask_forecast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_inputs[update.effective_user.id] = {"event": update.message.text.strip()}
    await update.message.reply_text("Какой был прогноз?")
    return ASK_FORECAST

async def ask_actual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_inputs[update.effective_user.id]["forecast"] = update.message.text.strip()
    await update.message.reply_text("Какой факт? (результат)")
    return ASK_ACTUAL

async def generate_interpretation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_data = user_inputs.get(update.effective_user.id)
        if not user_data or not all(k in user_data for k in ("event", "forecast")):
            await update.message.reply_text(
                "⚠️ Не хватает данных для анализа. Попробуй заново начать с ввода события."
            )
            return ConversationHandler.END

        user_data["actual"] = update.message.text.strip()

        prompt = (
            f"Event: {user_data['event']}\n"
            f"Forecast: {user_data['forecast']}\n"
            f"Actual: {user_data['actual']}\n\n"
            "You are a professional trader with over 10 years of experience in cryptocurrency, forex, and stock markets. "
            "Your analysis considers fundamental drivers, liquidity flows, market maker behavior, and crowd psychology.\n\n"
            "---\n\n"
            "📊 Analyze step by step:\n\n"
            "1️⃣ Fundamental and technical context:\n"
            "- How does this event impact liquidity, volatility, and market expectations?\n"
            "- What key technical levels could become zones of accumulation or panic selling?\n"
            "- Are there any signs of a possible reversal or continuation of the trend?\n\n"
            "2️⃣ Provide two scenarios:\n"
            "🟢 Bullish: where is the crowd likely to enter, where will large players start taking profits, "
            "and what levels could trigger further growth?\n"
            "🔴 Bearish: where are retail stop losses likely to be placed, where might market makers hunt them, "
            "and where could the price find a new balance?\n\n"
            "3️⃣ Forecast the short-term reaction for the next 1–3 days:\n"
            "- How might retail traders react?\n"
            "- What headlines could appear in the media or on Twitter?\n"
            "- How might whales and funds respond?\n\n"
            "4️⃣ Build a concise trading plan:\n"
            "- Should the trader enter a position? If yes, specify:\n"
            "  🎯 Entry: $_____\n"
            "  🚨 StopLoss: $_____\n"
            "  💰 TakeProfit: $_____\n"
            "- What percentage of capital to allocate given the risk?\n\n"
            "5️⃣ What might the trader miss if they ignore this event?\n"
            "- Explain in simple terms why this event is critical.\n\n"
            "✅ Finally, give a short signal in 1–2 lines suitable for a trader's chat.\n\n"
            "Answer strictly in Russian."
        )

        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}]
        )

        answer = response.choices[0].message.content.strip()
        await update.message.reply_text(
            f"📚 GPT:\n{answer}",
            reply_markup=REPLY_MARKUP
        )

    except Exception as e:
        logging.error(f"[generate_interpretation] Ошибка: {e}")
        await update.message.reply_text(
            "⚠️ GPT временно недоступен. "
            "Могу подсказать общий план на глаз:\n"
            "- Если событие положительное и ликвидность растёт — ищи зону отката для лонга.\n"
            "- Если факт хуже прогноза — рынок может собрать стопы внизу перед разворотом.\n"
            "Детали после восстановления сервиса!"
        )

    return ConversationHandler.END

async def general_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()
    style = context.user_data.get("style", "trading")
    tf = context.user_data.get("timeframe", "any")
    market = context.user_data.get("market", "general")

    prompt = (
        f"You are a professional trader with over 10 years of experience in cryptocurrency and stock markets. "
        "Always answer precisely, avoid vague words like 'maybe', 'probably', 'seems'. "
        "Speak directly, clearly, and justify your reasoning.\n\n"
        f"Context of the question:\n"
        f"- Trading style: {style}\n"
        f"- Timeframe: {tf}\n"
        f"- Market: {market}\n"
        f"- Trader's question: {user_text}\n\n"
        "---\n\n"
        "📊 Analyze step by step:\n\n"
        "1️⃣ **Key factors:**\n"
        "- List them in order of importance for this specific case.\n\n"
        "2️⃣ **Main action scenario:**\n"
        "- Where to enter, where to place stop loss, what are the targets.\n\n"
        "3️⃣ **Alternative scenario:**\n"
        "- If the main scenario fails, what to do and how quickly will it be clear?\n\n"
        "4️⃣ **Risks and potential:**\n"
        "- What are the key risks and approximate profit potential (estimated R:R)?\n\n"
        "5️⃣ **Your professional conclusion:**\n"
        "- What would you do right now if you were in the trader's position?\n\n"
        "6️⃣ **What else to check?**\n"
        "- Which reports, order book data, cluster analysis, news, or levels would confirm this scenario?\n\n"
        "✅ At the end, give a short signal for the trader's chat in 1–2 lines.\n\n"
        "Answer strictly in Russian."
    )

    try:
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}]
        )
        answer = response.choices[0].message.content.strip()
        await update.message.reply_text(
            f"📚 GPT:\n{answer}",
            reply_markup=REPLY_MARKUP,
            parse_mode="Markdown"
        )

    except Exception as e:
        logging.error(f"[GENERAL_RESPONSE] GPT error: {e}")
        await update.message.reply_text(
            "⚠️ GPT временно недоступен. "
            "Могу дать общий сценарий на глаз:\n"
            "- Если рынок растёт, ищи откаты для входа.\n"
            "- Если падает, ищи паттерн остановки.\n"
            "Подробнее после восстановления сервиса."
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
            [InlineKeyboardButton("Smart Money", callback_data="style_smc")],
            [InlineKeyboardButton("Позиционка", callback_data="style_swing")],
            [InlineKeyboardButton("Пробой", callback_data="style_breakout")]
        ])
        await query.edit_message_text("📈 Отлично, выбери стратегию для крипты:", reply_markup=keyboard)

    elif query.data == "market_forex":
        context.user_data["selected_market"] = "forex"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Smart Money", callback_data="style_smc")],
            [InlineKeyboardButton("Позиционка", callback_data="style_swing")],
            [InlineKeyboardButton("Пробой", callback_data="style_breakout")]
        ])
        await query.edit_message_text("📈 Отлично, выбери стратегию для форекса:", reply_markup=keyboard)

    elif query.data == "style_smc":
        context.user_data["selected_strategy"] = "smc"
        market = context.user_data.get("selected_market")
        text_msg = (
            "📈 *Smart Money Concepts (SMC) для крипты*\n\n"
            "📌 Включи на графике:\n"
            "- Smart Money Concepts (SMC) Lux Algo\n"
            "- LazyScalp Board (DV > 200M)\n\n"
            "Пришли скрин — дам план входа, стоп и тейки."
            if market == "crypto"
            else "📈 *Smart Money Concepts (SMC) для форекса*\n\n"
                 "📌 Убедись, что включён Smart Money Concepts (SMC) Lux Algo.\n"
                 "DV не нужен.\n\n"
                 "Пришли скрин — сделаю анализ SMC."
        )
        await query.edit_message_text(text_msg, parse_mode="Markdown")

    elif query.data == "style_swing":
        context.user_data["selected_strategy"] = "swing"
        market = context.user_data.get("selected_market")
        text_msg = (
            "📈 *Позиционка (Swing) для крипты*\n\n"
            "📌 Включи на графике:\n"
            "- Lux Algo Levels\n"
            "- LazyScalp Board (DV > 200M)\n"
            "- Volume Profile\n\n"
            "Пришли скрин для анализа swing."
            if market == "crypto"
            else "📈 *Позиционка (Swing) для форекса*\n\n"
                 "📌 Убедись, что включены:\n"
                 "- Lux Algo Levels или Auto Support & Resistance\n"
                 "- RSI / Stochastic\n\n"
                 "Пришли скрин — дам сценарий swing."
        )
        await query.edit_message_text(text_msg, parse_mode="Markdown")

    elif query.data == "style_breakout":
        context.user_data["selected_strategy"] = "breakout"
        market = context.user_data.get("selected_market")
        text_msg = (
            "📈 *Пробой диапазона (Breakout) для крипты*\n\n"
            "📌 Включи на графике:\n"
            "- Range Detection\n"
            "- LazyScalp Board (DV > 200M)\n\n"
            "Пришли скрин — найду диапазон и дам сценарии."
            if market == "crypto"
            else "📈 *Пробой диапазона (Breakout) для форекса*\n\n"
                 "📌 Убедись, что включены:\n"
                 "- Range Detection или Lux Algo Levels\n"
                 "- RSI / Stochastic\n\n"
                 "Пришли скрин — построю два сценария breakout."
        )
        await query.edit_message_text(text_msg, parse_mode="Markdown")

    elif query.data == "forecast_by_image":
        await query.message.reply_text(
            "📸 Пришли скриншот графика — я сделаю технический разбор и прогноз."
        )

    # ✅ Новый блок для сбора email
    elif query.data == "get_email":
        context.user_data["awaiting_email"] = True
        await query.message.reply_text(
            "✉️ Напиши свой email для получения секретного PDF со стратегиями:"
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
    selected_style = context.user_data.get("selected_strategy")

    if not selected_market or not selected_style:
        await update.message.reply_text(
            "📝 Сначала выбери стратегию и рынок через кнопки в меню, чтобы я знал, какой анализ тебе нужен."
        )
        return

    # Подготовим prompt_text в зависимости от выбранной стратегии
    if selected_style == "smc":
        if selected_market == "crypto":
            prompt_text = (
                "You are a professional Smart Money Concepts (SMC) trader with over 10 years of experience in cryptocurrency markets.\n\n"
                "Ensure the TradingView chart includes:\n"
                "- Smart Money Concepts (SMC) Lux Algo\n"
                "- LazyScalp Board for DV.\n\n"
                "If DV < 200M, write 🚫 and stop.\n"
                "If DV ≥ 200M:\n"
                "- Identify BOS, CHoCH, liquidity zones and OTE.\n"
                "- Build a detailed trading plan:\n"
                "  🎯 Entry: $_____\n"
                "  🚨 StopLoss: $_____\n"
                "  💰 TakeProfit: $_____\n\n"
                "✅ Finally, generate a concise 2-line summary in Russian suitable for a trader's chat.\n"
                "Answer strictly in Russian."
            )
        else:
            prompt_text = (
                "You are a Smart Money Concepts (SMC) trader on Forex with over 10 years of experience.\n\n"
                "Ensure Smart Money Concepts (SMC) Lux Algo is active.\n"
                "- Identify BOS, CHoCH, OTE and liquidity zones.\n"
                "- Build a detailed plan:\n"
                "  🎯 Entry: $_____\n"
                "  🚨 StopLoss: $_____\n"
                "  💰 TakeProfit: $_____\n\n"
                "✅ Finish with a short 2-line summary in Russian for a trader's chat.\n"
                "Answer strictly in Russian."
            )
    elif selected_style == "swing":
        if selected_market == "crypto":
            prompt_text = (
                "You are an experienced swing trader in cryptocurrency markets.\n\n"
                "Ensure the chart includes:\n"
                "- Auto Support & Resistance or Lux Algo Levels\n"
                "- Volume Profile\n"
                "- LazyScalp Board.\n"
                "If DV < 200M, write 🚫 and skip analysis.\n"
                "If DV ≥ 200M:\n"
                "- Identify accumulation zones and key levels.\n"
                "- Build a detailed plan:\n"
                "  🎯 Entry: $_____\n"
                "  🚨 StopLoss: $_____\n"
                "  💰 TakeProfit: $_____\n\n"
                "✅ Conclude with a concise 2-line summary in Russian for a trader's chat.\n"
                "Answer strictly in Russian."
            )
        else:
            prompt_text = (
                "You are a swing trader on Forex.\n\n"
                "Ensure the chart includes:\n"
                "- Auto Support & Resistance or Lux Algo Levels\n"
                "- Volume Profile if available\n"
                "- RSI or Stochastic indicators.\n"
                "- Identify accumulation zones and levels.\n"
                "- Build a detailed plan:\n"
                "  🎯 Entry: $_____\n"
                "  🚨 StopLoss: $_____\n"
                "  💰 TakeProfit: $_____\n\n"
                "✅ Finish with a concise 2-line summary in Russian for a trader's chat.\n"
                "Answer strictly in Russian."
            )
    elif selected_style == "breakout":
        if selected_market == "crypto":
            prompt_text = (
                "You are a scalper and intraday trader in cryptocurrency markets.\n\n"
                "Ensure the chart includes:\n"
                "- Range Detection or Lux Algo\n"
                "- LazyScalp Board for volumes.\n"
                "If DV < 200M, write 🚫 and stop.\n"
                "If DV ≥ 200M:\n"
                "- Find the consolidation range.\n"
                "- Provide two breakout scenarios:\n"
                "  📈 Up:\n"
                "    🎯 Entry: $_____\n"
                "    🚨 StopLoss: $_____\n"
                "    💰 TakeProfit: $_____\n"
                "  📉 Down:\n"
                "    🎯 Entry: $_____\n"
                "    🚨 StopLoss: $_____\n"
                "    💰 TakeProfit: $_____\n\n"
                "✅ Conclude with a short 2-line summary in Russian for a trader's chat.\n"
                "Answer strictly in Russian."
            )
        else:
            prompt_text = (
                "You are a scalper and intraday trader on Forex.\n\n"
                "Ensure the chart includes Range Detection or Lux Algo Levels and Volume Profile.\n"
                "- Identify the consolidation range.\n"
                "- Provide two breakout scenarios up and down:\n"
                "  📈 Up:\n"
                "    🎯 Entry: $_____\n"
                "    🚨 StopLoss: $_____\n"
                "    💰 TakeProfit: $_____\n"
                "  📉 Down:\n"
                "    🎯 Entry: $_____\n"
                "    🚨 StopLoss: $_____\n"
                "    💰 TakeProfit: $_____\n\n"
                "✅ End with a concise 2-line summary in Russian for a trader's chat.\n"
                "Answer strictly in Russian."
            )
    else:
        prompt_text = (
            "You are a trader with over 10 years of experience in crypto and Forex markets.\n\n"
            "For crypto ensure LazyScalp Board and Lux Algo Levels are enabled.\n"
            "If DV < 200M, write 🚫.\n"
            "For crypto with DV ≥ 200M and for Forex:\n"
            "- Determine trend and accumulation zones.\n"
            "- Build a detailed plan:\n"
            "  🎯 Entry: $_____\n"
            "  🚨 StopLoss: $_____\n"
            "  💰 TakeProfit: $_____\n\n"
            "✅ Conclude with a concise 2-line summary in Russian for a trader's chat.\n"
            "Answer strictly in Russian."
        )

    try:
        vision_response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt_text},
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/jpeg;base64,{image_base64}"
                    }}
                ]
            }],
            max_tokens=700
        )

        analysis = vision_response.choices[0].message.content.strip()
        if not analysis:
            await update.message.reply_text(
                "⚠️ GPT не дал ответа. Попробуй снова или пришли другой скрин."
            )
            return

        await update.message.reply_text(
            f"📉 Анализ графика по выбранной стратегии:\n\n{analysis}",
            reply_markup=REPLY_MARKUP
        )

    except Exception as e:
        logging.error(f"[handle_photo] Vision error: {e}")
        await update.message.reply_text(
            "⚠️ GPT временно недоступен. "
            "На глаз по таким графикам:\n"
            "- Если рынок растёт, ищи консолидацию и объём для входа.\n"
            "- Если падает, смотри реакцию на старые уровни спроса.\n"
            "Подробный сценарий дам после восстановления сервиса!"
        )

async def setup_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Получаем фото от пользователя
    photo = update.message.photo[-1]
    file = await photo.get_file()
    photo_bytes = await file.download_as_bytearray()

    # Преобразуем в BytesIO для Telegram API
    image_stream = BytesIO(photo_bytes)
    image_stream.name = "setup.jpg"  # важно для Telegram

    # Собираем описание из context.user_data
    instrument = context.user_data.get("instrument", "Не указано")
    risk_area = context.user_data.get("risk_area", "Не указано")
    targets = context.user_data.get("targets", "Не указано")
    stoploss = context.user_data.get("stoploss", "Не указано")

    caption = (
        f"🚀 *Новый сетап от админа*\n\n"
        f"• 📌 *Инструмент:* {instrument}\n"
        f"• 💰 *Область риска:* {risk_area}\n"
        f"• 🎯 *Цели:* {targets}\n"
        f"• 🚨 *Стоп-лосс:* {stoploss}\n\n"
        f"🧮 [Рассчитать позицию](https://t.me/ai4traders_bot)"
    )

    try:
        # Отправляем в канал
        chat_id = '@ai4traders'
        message = await context.bot.send_photo(
            chat_id=chat_id,
            photo=image_stream,
            caption=caption,
            parse_mode="Markdown"
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

async def handle_macro_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_macro_text"):
        return

    macro = update.message.text.strip()
    image_base64 = context.user_data.pop("graph_image_base64", None)
    context.user_data.pop("awaiting_macro_text")

    if not image_base64:
        await update.message.reply_text("⚠️ Ошибка: изображение не найдено. Сначала пришли скрин графика.")
        return

    prompt = (
        "You are a professional crypto trader with over 10 years of experience. "
        "Analyze the provided chart carefully and perform a structured step-by-step analysis.\n\n"
        "📊 Step-by-step:\n\n"
        "1️⃣ **Core market factors:**\n"
        "- Identify the trend (uptrend, downtrend, sideways).\n"
        "- Find major support and resistance levels.\n"
        "- Are there clear patterns (double top, double bottom, flag, etc.)?\n"
        "- What do volumes show near these levels?\n\n"
        "2️⃣ **Market structure:**\n"
        "- Are there signs of accumulation before a move?\n"
        "- Any signals of a reversal or strengthening of the current trend?\n"
        "- Have similar situations occurred in this asset's history?\n\n"
        f"🌐 **Also consider this fundamental background:** {macro}\n\n"
        "3️⃣ **Build two scenarios:**\n"
        "🟢 **Breakout upwards:**\n"
        "- 🎯 Entry: $_____\n"
        "- 🚨 StopLoss: $_____\n"
        "- 💰 TakeProfit: $_____\n"
        "- Briefly estimate probability of success (in %).\n\n"
        "🔴 **Breakdown downwards:**\n"
        "- 🎯 Entry: $_____\n"
        "- 🚨 StopLoss: $_____\n"
        "- 💰 TakeProfit: $_____\n"
        "- Briefly estimate probability of success (in %).\n\n"
        "4️⃣ **What else should the trader check to confirm scenarios:**\n"
        "- Volume Profile, order book (limit orders), large cluster trades, and latest news.\n\n"
        "✅ Finish with a concise 2-line signal for a trader's chat, e.g.:\n"
        "> LONG from $___, stop at $___, targets $___ — likely accumulation before impulse.\n\n"
        "Also provide a short bullet summary in English if needed for clarity.\n"
        "Answer everything strictly in Russian."
    )

    try:
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/jpeg;base64,{image_base64}"
                    }}
                ]
            }],
            max_tokens=700
        )

        answer = response.choices[0].message.content.strip()
        await update.message.reply_text(
            f"📊 Прогноз по графику + новости:\n\n"
            f"{answer}\n\n"
            f"📰 Полезные ссылки:\n"
            f"• [Forklog](https://t.me/forklog)\n"
            f"• [Bits.media](https://bits.media/news/)\n"
            f"• [RBC Crypto](https://www.rbc.ru/crypto/)\n"
            f"• [Investing](https://ru.investing.com/news/cryptocurrency-news/)",
            reply_markup=CHAT_DISCUSS_KEYBOARD,
            parse_mode="Markdown"
        )

    except Exception as e:
        logging.error(f"[MACRO_GRAPH] Vision error: {e}")
        await update.message.reply_text(
            "⚠️ GPT временно недоступен. "
            "На глаз по таким кейсам обычно:\n"
            "- Смотри реакцию цены на ключевые уровни + объём.\n"
            "- При положительных новостях часто выбивают стопы вниз перед ростом.\n"
            "Детальнее после восстановления сервиса!"
        )

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

    prompt = (
        "You are a professional investment strategist with over 20 years of experience across equities, bonds, ETFs, commodities, and cryptocurrencies. "
        "You have managed portfolios for HNWI (High-Net-Worth Individuals) and institutional clients. "
        "Your task is to develop a deep, structured, and personalized investment strategy based on the client's request.\n\n"
        f"Client request: {user_text}\n\n"
        "🔍 Work step by step in detail. Avoid generic phrases, clichés, or meaningless advice. "
        "Your answer must be precise and look like the work of a professional consultant.\n\n"
        "---\n\n"
        "📊 Structure your answer as follows:\n\n"
        "1️⃣ **Client profile diagnostics:**\n"
        "- Determine the investment horizon (short, medium, long-term) based on the request.\n"
        "- Determine the risk profile: aggressive, moderate, or conservative (and explain why).\n"
        "- Formulate the main goal (capital growth, capital preservation, dividend income, etc.).\n\n"
        "2️⃣ **Optimal portfolio structure:**\n"
        "- Provide specific asset classes (stocks, ETFs, bonds, crypto, commodities) with approximate percentage allocations.\n"
        "- For each asset class, give a brief rationale for why this proportion.\n\n"
        "3️⃣ **Macroeconomic and market context:**\n"
        "- List the key economic risks and trends currently impacting this strategy.\n"
        "- Explain how this portfolio is protected from or exposed to these factors.\n\n"
        "4️⃣ **Detailed step-by-step plan:**\n"
        "- What actions should be taken now (e.g., open an investment account, set up auto-deposits).\n"
        "- How often to review the portfolio (quarterly, semi-annually, and why).\n"
        "- Which indicators or reports to monitor for rebalancing.\n\n"
        "5️⃣ **Scenario analysis:**\n"
        "- Provide 2 scenarios: 'Market rises' and 'Market declines'. Describe what to do in each case.\n\n"
        "6️⃣ **Final summary:**\n"
        "- Formulate a concise 2-3 line conclusion suitable for a trader's chat, for example: "
        "'🚀 Strategy fits a 3+ year horizon, moderate risk, portfolio review every 6 months.'\n\n"
        "Answer everything strictly in Russian. Be succinct, professional, and avoid unnecessary fluff."
    )

    try:
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}]
        )
        answer = response.choices[0].message.content.strip()
        await update.message.reply_text(
            f"📈 Инвестиционная стратегия:\n\n{answer}",
            reply_markup=REPLY_MARKUP
        )
        context.user_data.clear()

    except Exception as e:
        logging.error(f"[handle_invest_question] GPT error: {e}")
        await update.message.reply_text(
            "⚠️ GPT временно недоступен. "
            "На глаз: для умеренного риска часто берут примерно\n"
            "- 50% акции/ETF,\n"
            "- 30% облигации,\n"
            "- 10% золото и сырьё,\n"
            "- 10% крипта.\n"
            "Пересмотри портфель раз в 6 месяцев. Детальнее после восстановления сервиса!"
        )
        context.user_data.clear()

async def teacher_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.strip() == "↩️ Выйти из обучения":
        context.user_data.pop("awaiting_teacher_question", None)
        await update.message.reply_text(
            "🔙 Ты вышел из режима обучения. Возвращаемся в главное меню.",
            reply_markup=REPLY_MARKUP
        )
        return

    user_text = update.message.text.strip()

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

        education_keyboard = [["↩️ Выйти из обучения"]]
        reply_markup = ReplyKeyboardMarkup(education_keyboard, resize_keyboard=True)

        await update.message.reply_text(
            f"📖 Обучение:\n\n{response.choices[0].message.content.strip()}",
            reply_markup=reply_markup
        )

    except Exception as e:
        logging.error(f"[TEACHER_RESPONSE] GPT error: {e}")
        await update.message.reply_text(
            "⚠️ GPT временно недоступен. Попробуй позже.",
            reply_markup=REPLY_MARKUP
        )

async def handle_definition(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("awaiting_definition_term", None)
    term = update.message.text.strip()

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
        await update.message.reply_text(
            f"📘 Определение:\n{response.choices[0].message.content.strip()}",
            reply_markup=REPLY_MARKUP
        )
    except Exception as e:
        logging.error(f"[DEFINITION] GPT error: {e}")
        await update.message.reply_text("⚠️ Не удалось объяснить термин. Попробуй позже.")

async def handle_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id

    logging.info(f"[handle_main] Пользователь {user_id} нажал кнопку: {text}")

    if user_id not in ALLOWED_USERS and text not in ["💰 Купить", "ℹ️ О боте"]:
        await update.message.reply_text(
            "🔒 Доступ только после активации подписки за $25.",
            reply_markup=REPLY_MARKUP
        )
        return

    reset_commands = [
        "🎯 Риск", "🌱 Психолог", "🔍 Анализ",
        "💡 Стратегия", "📚 Термин",
        "🚀 Сигнал", "📖 Обучение",
        "💰 Купить", "ℹ️ О боте", "📌 Сетап"
    ]
    if text in reset_commands:
        context.user_data.clear()

    if text == "💡 Стратегия":
        await help_invest(update, context)
        return

    if text == "🎯 Риск":
        return await start_risk_calc(update, context)

    if text == "🌱 Психолог":
        return await start_therapy(update, context)

    if text == "🔍 Анализ":
        return await help_pro(update, context)  # теперь анализ = интерпретация новостей

    if text == "📖 Обучение":
        context.user_data["awaiting_teacher_question"] = True
        await update.message.reply_text(
            "✍️ Напиши свой вопрос — я отвечу как преподаватель с 20+ годами опыта в трейдинге и инвестициях."
        )
        return

    if text == "📚 Термин":
        context.user_data["awaiting_definition_term"] = True
        await update.message.reply_text("✍️ Напиши термин, который нужно объяснить.")
        return

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

    if text == "💰 Купить":
        if user_id in ALLOWED_USERS:
            await update.message.reply_text(
                "✅ У тебя уже активирована подписка!",
                reply_markup=REPLY_MARKUP
            )
        else:
            await send_payment_link(update, context)
        return

    if text == "ℹ️ О боте":
        await update.message.reply_text(
            "Подписка активируется через CryptoCloud.\n"
            "Нажми 💰 Купить для получения ссылки на оплату.",
            reply_markup=REPLY_MARKUP
        )
        return

    if text == "📌 Сетап":
        if user_id not in ADMIN_IDS:
            await update.message.reply_text("⛔️ Эта функция доступна только админу.")
            return
        await update.message.reply_text("✍️ Укажи торговый инструмент (например: BTC/USDT):")
        return SETUP_1

    # 🔥 Умный сброс
    if not any([
        context.user_data.get("awaiting_potential"),
        context.user_data.get("awaiting_macro_text"),
        context.user_data.get("awaiting_definition_term"),
        context.user_data.get("awaiting_email"),
        context.user_data.get("awaiting_invest_question"),
        context.user_data.get("awaiting_pro_question"),
        context.user_data.get("awaiting_teacher_question"),
    ]):
        context.user_data.clear()
        await update.message.reply_text(
            "🔄 Сброс всех ожиданий. Продолжай.",
            reply_markup=REPLY_MARKUP
        )

async def gpt_psychologist_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()

    if user_text == "↩️ Выйти в меню":
        await update.message.reply_text("🔁 Возвращаемся в главное меню!", reply_markup=REPLY_MARKUP)
        return ConversationHandler.END

    prompt = (
        "You are a GPT-psychologist for traders. "
        "You respond with warm irony and light humor, helping them cope with gambling addiction tendencies, losing streaks, and emotional swings. "
        "Avoid gender-specific words like 'bro' or 'girl', use neutral terms such as 'friend', 'colleague', or 'trader'.\n\n"
        f"User's message:\n{user_text}\n\n"
        "📌 Follow this exact structure:\n\n"
        "1️⃣ **React empathetically**, but without pity. Show you understand the feeling of losses.\n\n"
        "2️⃣ **Provide a metaphor** to help the trader realize that a drawdown isn't the end. "
        "For example: 'it's like pulling back a slingshot before it fires.'\n\n"
        "3️⃣ **Give a fact or story** showing that even top traders have losing streaks (like Soros or Druckenmiller). "
        "This builds confidence that everyone experiences losses.\n\n"
        "4️⃣ **Suggest one simple micro-action** to feel in control right now, like closing the terminal, journaling emotions, or stepping outside.\n\n"
        "5️⃣ **Finish with a trading meme or funny short quote**, e.g.: '— Are you holding a position? — No, I'm holding back tears 😭.'\n\n"
        "⚠️ Avoid generic phrases like 'don't worry' or 'everything will be fine'. Be specific, warm, and slightly ironic.\n"
        "Answer everything strictly in Russian."
    )

    try:
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}]
        )

        therapy_keyboard = [["↩️ Выйти в меню"]]
        reply_markup = ReplyKeyboardMarkup(therapy_keyboard, resize_keyboard=True)

        await update.message.reply_text(
            f"🧘 GPT-психолог:\n{response.choices[0].message.content.strip()}",
            reply_markup=reply_markup
        )
        return WAITING_FOR_THERAPY_INPUT

    except Exception as e:
        logging.error(f"[GPT_PSYCHOLOGIST] Ошибка при ответе: {e}")
        await update.message.reply_text("⚠️ Произошла ошибка. Попробуйте ещё раз позже.")
        return WAITING_FOR_THERAPY_INPUT

async def start_therapy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    therapy_keyboard = [
        ["↩️ Выйти в меню"]
    ]
    reply_markup = ReplyKeyboardMarkup(therapy_keyboard, resize_keyboard=True)

    await update.message.reply_text(
        "😵‍💫 Ну что, опять рынок побрил как барбер в пятницу? Бывает, дружище.\n\n"
        "Напиши, что случилось — GPT-психолог с доброй иронией выслушает, подбодрит и вставит мем.\n\n"
        "Когда захочешь вернуться к аналитике — просто нажми *«↩️ Выйти в меню»*.",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    return WAITING_FOR_THERAPY_INPUT

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

@app_flask.route("/cryptocloud_webhook", methods=["POST"])
def cryptocloud_webhook():
    body = request.get_data()
    signature = request.headers.get("X-Signature-SHA256")

    calc_sig = hmac.new(API_SECRET.encode(), body, hashlib.sha256).hexdigest()
    if signature != calc_sig:
        print(f"⚠ Неверная подпись IPN: {signature} != {calc_sig}")
        return jsonify({"status": "invalid signature"})

    data = request.json
    print(f"✅ IPN от CryptoCloud:\n{json.dumps(data, indent=2, ensure_ascii=False)}")

    if data.get("status") == "paid":
        order_id = data.get("order_id")
        if order_id and order_id.startswith("user_"):
            parts = order_id.split("_")
            try:
                user_id = int(parts[1])
            except (IndexError, ValueError):
                print(f"❌ Ошибка парсинга user_id в order_id: {order_id}")
                return jsonify({"status": "bad order_id"})

            username = parts[2] if len(parts) > 2 else ""

            ALLOWED_USERS.add(user_id)
            log_payment(user_id, username)
            print(f"🎉 Пользователь {user_id} ({username}) активирован через POS!")

            asyncio.run_coroutine_threadsafe(
                notify_user_payment(user_id),
                app.loop
            )

    return jsonify({"ok": True})

# 🚀 Запуск Flask в отдельном потоке
def run_flask():
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
    # ✅ Блок обработки email
    if context.user_data.get("awaiting_email"):
        email = update.message.text.strip()
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
            return  # оставим ожидание email
        context.user_data.pop("awaiting_email", None)
        return

    # ✅ Остальные блоки
    if context.user_data.get("awaiting_potential"):
        await handle_potential(update, context)
    elif context.user_data.get("awaiting_macro_text"):
        await handle_macro_text(update, context)
    elif context.user_data.get("awaiting_definition_term"):
        await handle_definition(update, context)
    elif context.user_data.get("awaiting_invest_question"):
        await handle_invest_question(update, context)
    elif context.user_data.get("awaiting_pro_question"):
        await general_response(update, context)
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
    # 🚀 Запускаем Flask webhook в отдельном потоке
    threading.Thread(target=run_flask).start()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    logging.info("🚀 GPT-Трейдер стартовал!")

    # 🔄 Еженедельная рассылка
    @aiocron.crontab('0 12 * * mon')
    async def weekly_broadcast():
        message_text = (
            "🚀 Еженедельный обзор:\n"
            "• BTC сейчас около $108,700 — зона интереса $108,000–109,000, следи за реакцией на объёмах.\n"
            "• ETH держится на $2,576 — ищем покупки в диапазоне $2,520–2,600.\n"
            "• Стопы держи коротко, цели фиксируй по R:R ~2:1."
        )
        success, fails = 0, []
        for vip_id in ALLOWED_USERS:
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
            CommandHandler("start", start),
            CommandHandler("restart", restart),
            MessageHandler(filters.Regex("^🔄 Перезапустить бота$"), restart)
        ]
    )

    # 🧠 Помощь профессионала
    help_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🧠 Помощь профессионала$"), help_pro)],
        states={
            INTERPRET_NEWS: [MessageHandler(filters.TEXT & ~filters.COMMAND, interpret_decision)],
            ASK_EVENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_forecast)],
            ASK_FORECAST: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_actual)],
            ASK_ACTUAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, generate_interpretation)],
            FOLLOWUP_1: [MessageHandler(filters.TEXT & ~filters.COMMAND, followup_strategy)],
            FOLLOWUP_2: [MessageHandler(filters.TEXT & ~filters.COMMAND, followup_timeframe)],
            FOLLOWUP_3: [MessageHandler(filters.TEXT & ~filters.COMMAND, followup_market)],
            GENERAL_QUESTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, general_response)],
        },
        fallbacks=[
            CommandHandler("start", start),
            CommandHandler("restart", restart),
            MessageHandler(filters.Regex("^🔄 Перезапустить бота$"), restart)
        ]
    )

    # 📏 Калькулятор риска
    risk_calc_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📏 Калькулятор риска$"), start_risk_calc)],
        states={
            RISK_CALC_1: [MessageHandler(filters.TEXT & ~filters.COMMAND, risk_calc_deposit)],
            RISK_CALC_2: [MessageHandler(filters.TEXT & ~filters.COMMAND, risk_calc_risk_percent)],
            RISK_CALC_3: [MessageHandler(filters.TEXT & ~filters.COMMAND, risk_calc_stoploss)],
        },
        fallbacks=[
            CommandHandler("start", start),
            CommandHandler("restart", restart),
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
            CommandHandler("start", start),
            CommandHandler("restart", restart),
            MessageHandler(filters.Regex("^🔄 Перезапустить бота$"), restart)
        ]
    )

    # ✅ Регистрируем ConversationHandlers
    app.add_handler(help_conv_handler)
    app.add_handler(therapy_handler)
    app.add_handler(risk_calc_handler)
    app.add_handler(setup_handler)

    # ✅ Регистрируем команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("restart", restart))
    app.add_handler(CommandHandler("publish", publish_post))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("grant", grant))
    app.add_handler(CommandHandler("reload_users", reload_users))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("export", export))

    # ✅ Фото, inline кнопки и универсальный текст
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unified_text_handler))

    # 🚀 Стартуем polling
    app.run_polling()

def log_payment(user_id, username):
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sheet.append_row([str(user_id), username, timestamp])
        logging.info(f"🧾 Записано в Google Sheets: {user_id}, {username}, {timestamp}")
    except Exception as e:
        logging.error(f"❌ Ошибка при записи в Google Sheets: {e}")

async def notify_user_payment(user_id):
    try:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚀 Перейти в меню", callback_data="start_menu")],
            [InlineKeyboardButton("🎯 Пригласить друга и получить бонус", url="https://твоя_реферальная_страница.com")]
        ])

        await app.bot.send_message(
            chat_id=user_id,
            text=(
                "✅ Оплата получена! Подписка активирована навсегда 🎉\n\n"
                "🤖 GPT-помощник доступен: задавай вопросы, загружай графики, получай прогнозы.\n\n"
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











