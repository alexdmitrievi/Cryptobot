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

# ✅ Подключение к Google Sheets
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds_dict = json.loads(os.getenv("GOOGLE_CREDS"))

# 🔐 Исправляем переносы строк в приватном ключе
creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")

creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
gc = gspread.authorize(creds)

SPREADSHEET_ID = "1s_KQLyekb-lQjt3fMlBO39CTBuq0ayOIeKkXEhDjhbs"
sheet = gc.open_by_key(SPREADSHEET_ID).sheet1

client = AsyncOpenAI(api_key=OPENAI_API_KEY)
logging.basicConfig(level=logging.INFO)

TON_WALLET = "UQC4nBKWF5sO2UIP9sKl3JZqmmRlsGC5B7xM7ArruA61nTGR"
ALLOWED_USERS = {407721399, 592270446}
PENDING_USERS = {}
RECEIVED_MEMOS = set()

reply_keyboard = [
    ["🔍 Потенциал монеты", "📊 Прогноз по активу", "🧠 Помощь профессионала"],
    ["📈 Получить сигнал", "🧘 Спокойствие"],
    ["📚 Объяснение термина", "📏 Калькулятор риска"],
    ["💰 Подключить за $25", "💵 О подписке"],
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
    user_inputs[update.effective_user.id]["actual"] = update.message.text.strip()
    data = user_inputs[update.effective_user.id]

    prompt = (
        f"Событие: {data['event']}\n"
        f"Прогноз: {data['forecast']}\n"
        f"Факт: {data['actual']}\n\n"
        "Ты — профессиональный трейдер с опытом более 10 лет на рынках криптовалют, форекса и акций. "
        "Твои прогнозы учитывают фундаментальные драйверы, ликвидность, поведение маркетмейкеров и психологию толпы.\n\n"
        "---\n\n"
        "📊 Проанализируй строго по шагам:\n\n"
        "1️⃣ Фундаментальный и технический фон:\n"
        "- Как событие влияет на ликвидность, волатильность и ожидания рынка?\n"
        "- Какие ключевые техуровни могут стать точками закупок или паники?\n"
        "- Есть ли признаки готовности к развороту или продолжению тренда?\n\n"
        "2️⃣ Дай два сценария:\n"
        "🟢 Положительный (bullish): где вероятно зайдёт толпа, где начнут фиксироваться крупные игроки, какие уровни будут триггером для роста?\n"
        "🔴 Негативный (bearish): где будут ставить стопы розничные, где маркетмейкер их выбьет, где цена может найти новый баланс?\n\n"
        "3️⃣ Спрогнозируй краткосрочную реакцию на 1–3 дня:\n"
        "- Что сделают розничные трейдеры?\n"
        "- Какие заголовки могут появиться в СМИ и Twitter?\n"
        "- Какие действия могут предпринять киты и фонды?\n\n"
        "4️⃣ Составь краткий торговый план:\n"
        "- СтОит ли входить? Если да:\n"
        "  🎯 Entry: $_____\n"
        "  🚨 StopLoss: $_____\n"
        "  💰 TakeProfit: $_____\n"
        "- Какой % капитала задействовать, исходя из риска?\n\n"
        "5️⃣ Что пользователь может упустить, если проигнорирует эту новость?\n"
        "- Объясни прямым текстом, почему это событие критично.\n\n"
        "✅ В конце дай итоговый сигнал для трейд-чата в 1–2 строках."
    )

    try:
        response = await client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}]
        )
        await update.message.reply_text(
            f"📚 GPT:\n{response.choices[0].message.content.strip()}",
            reply_markup=REPLY_MARKUP
        )
    except Exception as e:
        logging.error(f"[generate_interpretation] GPT error: {e}")
        await update.message.reply_text(
            "⚠️ Не удалось получить анализ. Попробуй позже."
        )

    return ConversationHandler.END

async def general_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()
    style = context.user_data.get("style", "трейдинг")
    tf = context.user_data.get("timeframe", "любом")
    market = context.user_data.get("market", "общий")

    prompt = (
        f"Ты — профессиональный трейдер с опытом более 10 лет на криптовалютных и фондовых рынках. "
        f"Отвечай строго, без воды, избегай слов 'возможно', 'по-видимому', 'скорее всего'. "
        f"Говори прямо, точно и обоснованно.\n\n"
        f"Контекст запроса:\n"
        f"- Стиль торговли: {style}\n"
        f"- Таймфрейм: {tf}\n"
        f"- Рынок: {market}\n"
        f"- Вопрос трейдера: {user_text}\n\n"
        "---\n\n"
        "📊 Проанализируй строго по шагам:\n\n"
        "1️⃣ **Ключевые факторы:**\n"
        "- Расставь их по степени важности для этого запроса.\n\n"
        "2️⃣ **Основной сценарий действий:**\n"
        "- Где входить, куда ставить стоп, какие цели.\n\n"
        "3️⃣ **Альтернативный сценарий:**\n"
        "- Если основной не сработает, что сделать и как быстро это станет понятно?\n\n"
        "4️⃣ **Риски и потенциал:**\n"
        "- Какие ключевые риски и каков потенциал прибыли (примерное R:R)?\n\n"
        "5️⃣ **Твой профессиональный вывод:**\n"
        "- Что бы ты сделал прямо сейчас, будь на месте трейдера?\n\n"
        "6️⃣ **Что ещё проверить?**\n"
        "- Какие отчёты, стакан, кластера, новости или уровни подтвердят этот сценарий?\n\n"
        "✅ В конце дай итоговый сигнал для трейд-чата в 1–2 строках."
    )

    try:
        response = await client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}]
        )
        await update.message.reply_text(
            f"📚 GPT:\n{response.choices[0].message.content.strip()}",
            reply_markup=REPLY_MARKUP,
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    except Exception as e:
        logging.error(f"[GENERAL_RESPONSE] GPT ошибка: {e}")
        await update.message.reply_text("⚠️ GPT не ответил. Попробуй позже.")
        return ConversationHandler.END

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

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
            "📈 *Smart Money Concepts (SMC)*\n\n"
            "📌 Для крипты включи LazyScalp Board в TradingView и убедись что DV > 200M.\n"
            "Для форекса DV не нужен.\n\n"
            "🖼 Пришли скрин — дам план входа, стоп и тейки."
            if market == "crypto"
            else "📈 *Smart Money Concepts (SMC)* для форекса.\n\n"
                 "DV не нужен. Пришли скрин — сделаю анализ SMC."
        )
        await query.edit_message_text(text_msg, parse_mode="Markdown")

    elif query.data == "style_swing":
        context.user_data["selected_strategy"] = "swing"
        market = context.user_data.get("selected_market")
        text_msg = (
            "📈 *Позиционка (Swing)* для крипты.\n\n"
            "📌 Включи LazyScalp Board, DV > 200M.\n"
            "Пришли скрин для анализа swing."
            if market == "crypto"
            else "📈 *Позиционка (Swing)* для форекса.\n\n"
                 "DV не нужен. Пришли скрин — дам сценарии."
        )
        await query.edit_message_text(text_msg, parse_mode="Markdown")

    elif query.data == "style_breakout":
        context.user_data["selected_strategy"] = "breakout"
        market = context.user_data.get("selected_market")
        text_msg = (
            "📈 *Пробой диапазона (Breakout)* для крипты.\n\n"
            "📌 Включи LazyScalp Board и убедись DV > 200M.\n"
            "Пришли скрин — найду зону флэта и дам сценарии."
            if market == "crypto"
            else "📈 *Пробой диапазона (Breakout)* для форекса.\n\n"
                 "DV не нужен. Пришли скрин для анализа breakout."
        )
        await query.edit_message_text(text_msg, parse_mode="Markdown")



async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    photo = update.message.photo[-1]
    file = await photo.get_file()
    original_photo_bytes = await file.download_as_bytearray()

    # Сжимаем для Vision
    image = Image.open(io.BytesIO(original_photo_bytes)).convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=80)
    image_base64 = base64.b64encode(buffer.getvalue()).decode()

    selected_market = context.user_data.get("selected_market")
    selected_style = context.user_data.get("selected_strategy")

    if selected_style == "smc":
        if selected_market == "crypto":
            prompt_text = (
                "Ты — профессиональный трейдер Smart Money Concepts с опытом более 10 лет на крипте.\n\n"
                "На скрине TradingView обязательно виден индикатор LazyScalp Board с DV.\n"
                "Если DV < 200M, напиши крупно 🚫, что сетап не строим. После этого остановись.\n"
                "Если DV ≥ 200M:\n"
                "- Найди зоны ликвидности (где стопы толпы).\n"
                "- Определи BOS, CHoCH и зоны OTE.\n"
                "- Построй торговый план:\n"
                "  🎯 Entry: $_____\n"
                "  🚨 StopLoss: $_____\n"
                "  💰 TakeProfit: $_____\n"
                "- Объясни почему именно здесь вход и что делает толпа.\n"
                "- В конце дай краткий итоговый сигнал для трейд-чата в 2 строках."
            )
        else:
            prompt_text = (
                "Ты — профессиональный трейдер Smart Money Concepts с опытом более 10 лет на форексе.\n\n"
                "Игнорируй DV, так как рынок внебиржевой.\n"
                "- Найди зоны ликвидности.\n"
                "- BOS, CHoCH и OTE.\n"
                "- Построй торговый план с Entry, StopLoss, TakeProfit.\n"
                "- Объясни почему вход здесь и как ведут себя институционалы.\n"
                "- В конце дай итоговый сигнал для трейд-чата."
            )
    elif selected_style == "swing":
        if selected_market == "crypto":
            prompt_text = (
                "Ты — опытный swing трейдер с опытом более 10 лет на крипте.\n\n"
                "На скрине TradingView должен быть LazyScalp Board для DV.\n"
                "Если DV < 200M, напиши 🚫 крупно и не делай анализ.\n"
                "Если DV ≥ 200M:\n"
                "- Найди ключевые уровни поддержки и сопротивления.\n"
                "- Зоны накоплений маркетмейкеров.\n"
                "- Построй план:\n"
                "  🎯 Entry: $_____\n"
                "  🚨 StopLoss: $_____\n"
                "  💰 TakeProfit: $_____\n"
                "- Напиши что трейдеру стоит проверить дополнительно: корреляцию, отчёты китов, новости.\n"
                "- В конце дай короткий итог как сигнал для трейд-чата."
            )
        else:
            prompt_text = (
                "Ты — опытный swing трейдер с опытом более 10 лет на форексе.\n\n"
                "DV не требуется. Сразу:\n"
                "- Найди ключевые уровни поддержки/сопротивления.\n"
                "- Зоны накоплений маркетмейкеров.\n"
                "- Построй план Entry, StopLoss, TakeProfit.\n"
                "- Посоветуй что проверить (корреляцию с индексами, отчёты крупных игроков).\n"
                "- В конце дай краткий итоговый сигнал для чата."
            )
    elif selected_style == "breakout":
        if selected_market == "crypto":
            prompt_text = (
                "Ты — скальпер и интрадей трейдер с опытом более 10 лет на крипте.\n\n"
                "На скрине TradingView должен быть LazyScalp Board с DV.\n"
                "Если DV < 200M, напиши 🚫 и закончи.\n"
                "Если DV ≥ 200M:\n"
                "- Найди диапазон консолидации (range).\n"
                "- Дай два сценария:\n"
                "  📈 Breakout вверх:\n"
                "    🎯 Entry: $_____\n"
                "    🚨 StopLoss: $_____\n"
                "    💰 TakeProfit: $_____\n"
                "  📉 Breakout вниз:\n"
                "    🎯 Entry: $_____\n"
                "    🚨 StopLoss: $_____\n"
                "    💰 TakeProfit: $_____\n"
                "- Посоветуй что проверить: дельту, кластера, стакан.\n"
                "- Заверши коротким сигналом для трейд-чата."
            )
        else:
            prompt_text = (
                "Ты — скальпер и интрадей трейдер с опытом более 10 лет на форексе.\n\n"
                "DV не нужен. Сразу:\n"
                "- Найди диапазон консолидации (range).\n"
                "- Дай два сценария breakout (вверх и вниз) с Entry, StopLoss, TakeProfit.\n"
                "- Укажи что проверить дополнительно: дельту, стакан, новости.\n"
                "- Заверши итоговым сигналом для трейд-чата в 2 строках."
            )
    else:
        prompt_text = (
            "Ты — трейдер с опытом более 10 лет на крипте и форексе.\n\n"
            "Если это крипта и DV < 200M (по LazyScalp Board), напиши 🚫 и закончи.\n"
            "Для крипты с DV ≥ 200M и для форекса:\n"
            "- Определи тренд.\n"
            "- Найди ключевые уровни поддержки/сопротивления.\n"
            "- Дай Entry, StopLoss, TakeProfit.\n"
            "- Закрой двумя строками как сигнал для трейд-чата."
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
        await update.message.reply_text(
            f"📉 Анализ графика по выбранной стратегии:\n\n{analysis}",
            reply_markup=REPLY_MARKUP
        )
    except Exception as e:
        logging.error(f"[handle_photo] Vision error: {e}")
        await update.message.reply_text(
            "⚠️ Не удалось проанализировать график. Попробуй позже или пришли другой скрин."
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
        "Ты — профессиональный криптотрейдер с опытом более 10 лет. "
        "На изображении представлен график криптовалюты на таймфрейме 4H.\n\n"
        "📊 Выполни строгий пошаговый анализ:\n\n"
        "1️⃣ **Основные рыночные факторы:**\n"
        "- Определи тренд (вверх, вниз или боковик).\n"
        "- Найди важные уровни поддержки и сопротивления.\n"
        "- Есть ли очевидные паттерны (double-top, двойное дно, флаг и т.д.)?\n"
        "- Что показывают объёмы относительно этих уровней?\n\n"
        "2️⃣ **Структура рынка:**\n"
        "- Есть ли признаки накопления перед движением?\n"
        "- Видны ли сигналы разворота или усиления текущего тренда?\n"
        "- Были ли похожие ситуации на истории графика этого актива?\n\n"
        f"🌐 **Учитывай фундаментальный фон:** {macro}\n\n"
        "3️⃣ **Построй два сценария:**\n"
        "🟢 **Пробой вверх:**\n"
        "- 🎯 Entry: $_____\n"
        "- 🚨 StopLoss: $_____\n"
        "- 💰 TakeProfit: $_____\n"
        "- Оцени вероятность успеха (коротко в %).\n\n"
        "🔴 **Пробой вниз:**\n"
        "- 🎯 Entry: $_____\n"
        "- 🚨 StopLoss: $_____\n"
        "- 💰 TakeProfit: $_____\n"
        "- Оцени вероятность успеха (коротко в %).\n\n"
        "4️⃣ **Что дополнительно стоит проверить для подтверждения сценария:**\n"
        "- Профиль объёмов, стакан (лимитные заявки), кластера крупных сделок, актуальные новости.\n\n"
        "✅ В конце дай краткий сигнал для трейд-чата в 1–2 строках, например:\n"
        "> LONG от $___ со стопом $___, цели $___ — вероятно накопление перед импульсом.\n\n"
        "Also add short bullet summary in English if needed for clarity."
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

        await update.message.reply_text(
            f"📊 Прогноз по графику + новости:\n\n"
            f"{response.choices[0].message.content.strip()}\n\n"
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
            "⚠️ Не удалось составить прогноз. Попробуй позже или загрузи другой скрин."
        )


def fetch_price_from_coingecko(coin_symbol: str) -> float | None:
    try:
        coin_map = {
            "BTC": "bitcoin",
            "ETH": "ethereum",
            "BNB": "binancecoin",
            "XRP": "ripple",
            "SOL": "solana",
            "TON": "the-open-network",
            "DOGE": "dogecoin",
            "ADA": "cardano",
            "TRX": "tron"
        }
        coin_id = coin_map.get(coin_symbol.upper())
        if not coin_id:
            return None

        url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd"
        response = requests.get(url, timeout=20)
        data = response.json()
        return data[coin_id]["usd"]
    except Exception as e:
        logging.warning(f"Ошибка при получении цены для {coin_symbol}: {e}")
        return None


async def handle_potential(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("awaiting_potential", None)
    coin = update.message.text.strip().upper()
    coin = re.sub(r'[^A-Z0-9]', '', coin)  # убираем / и прочее

    price = fetch_price_from_coingecko(coin)
    if price:
        price_line = f"Актуальная цена {coin} составляет ${price:.2f}.\n\n"
    else:
        price_line = f"(❗️ Цена {coin} не найдена автоматически. Уточни её на CoinMarketCap, Binance или TradingView.)\n\n"

    prompt = (
        price_line +
        f"Ты — профессиональный трейдер с опытом более 10 лет на крипторынке, форексе и фондовом рынке.\n\n"
        f"📊 Проанализируй {coin} строго по шагам:\n\n"
        f"1️⃣ **Фундаментальные и технические факторы для {coin}:**\n"
        f"- Определи что сейчас больше влияет: макро, регуляция, отчёты китов, объёмы на биржах, новости.\n"
        f"- Расставь эти факторы по важности.\n\n"
        f"2️⃣ **Ключевые уровни:**\n"
        f"- Найди уровни поддержки и сопротивления по истории цены и ликвидности.\n"
        f"- Приведи конкретные цифры (например Support $_____, Resistance $_____).\n\n"
        f"3️⃣ **Дай три сценария для {coin}:**\n"
        f"🟢 Скальпинг (1-2 дня):\n"
        f"- 🎯 Entry: $_____\n"
        f"- 🚨 StopLoss: $_____\n"
        f"- 💰 TakeProfit: $_____\n\n"
        f"🟠 Свинг (3-7 дней):\n"
        f"- 🎯 Entry: $_____\n"
        f"- 🚨 StopLoss: $_____\n"
        f"- 💰 TakeProfit: $_____\n\n"
        f"🔵 Инвестиция (1-3 месяца):\n"
        f"- 🎯 Entry: $_____\n"
        f"- 🚨 StopLoss: $_____\n"
        f"- 💰 TakeProfit: $_____\n\n"
        f"4️⃣ **Что трейдеру нужно проверить ДО входа:**\n"
        f"- Стакан, открытый интерес, отчёты китов, новости.\n\n"
        f"✅ **В конце дай краткий вывод для трейд-чата в 2 строках:**\n"
        f"> Например: LONG от $___ со стопом $___, цели $___ — рынок готов к импульсу.\n\n"
        f"😎 Обязательно добавь короткую эмоциональную реакцию трейдера через emoji (🚀, 🐻, 🤔, 😱).\n\n"
        f"Отвечай чётко, пошагово, только на русском языке."
    )

    try:
        response = await client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}]
        )
        await update.message.reply_text(
            f"📈 Потенциал {coin}:\n\n"
            f"{response.choices[0].message.content.strip()}\n\n"
            f"📰 Для чтения свежих новостей:\n"
            f"• [Forklog](https://t.me/forklog)\n"
            f"• [Bits.media](https://bits.media/news/)\n"
            f"• [RBC Crypto](https://www.rbc.ru/crypto/)\n"
            f"• [Investing](https://ru.investing.com/news/cryptocurrency-news/)",
            reply_markup=CHAT_DISCUSS_KEYBOARD,
            parse_mode="Markdown"
        )
    except Exception as e:
        logging.error(f"[POTENTIAL] GPT ошибка: {e}")
        await update.message.reply_text("⚠️ Не удалось проанализировать монету. Попробуй позже.")

async def handle_definition(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("awaiting_definition_term", None)
    term = update.message.text.strip()

    prompt = (
        f"Ты — профессиональный трейдер и преподаватель с опытом более 10 лет.\n"
        f"Объясни простыми словами, что такое '{term}'. "
        f"Объяснение должно быть так, чтобы понял даже новичок без опыта.\n\n"
        f"- Дай короткое, ясное определение в одном-двух предложениях.\n"
        f"- Приведи короткую аналогию (например с магазином, спортом или бытом), чтобы понятие стало интуитивно ясным.\n"
        f"- В конце дай конкретный пример из практики трейдинга, где используется этот термин.\n\n"
        f"Не используй лишнюю воду и профессиональный жаргон без пояснения. "
        f"Отвечай только на русском языке."
    )

    try:
        response = await client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}]
        )
        await update.message.reply_text(
            f"📘 Определение:\n{response.choices[0].message.content.strip()}",
            reply_markup=REPLY_MARKUP
        )
    except Exception as e:
        logging.error(f"[DEFINITION] GPT ошибка: {e}")
        await update.message.reply_text("⚠️ Не удалось объяснить термин. Попробуй позже.")

async def handle_forecast_by_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("awaiting_asset_name", None)
    coin = update.message.text.strip().upper()
    price = fetch_price_from_coingecko(coin)

    if price:
        price_line = f"Актуальная цена {coin} — ${price:.2f}.\n\n"
    else:
        price_line = f"(❗️ Цена {coin} не найдена. Уточни её на CoinMarketCap или Binance.)\n\n"

    prompt = (
        price_line +
        f"Ты — профессиональный трейдер с опытом более 10 лет на криптовалютном рынке.\n\n"
        "📊 Проанализируй {coin} строго по шагам:\n\n"
        "1️⃣ **Общая структура рынка:**\n"
        "- Определи тренд (вверх, вниз, боковик) и текущее настроение.\n"
        "- Расставь ключевые факторы по степени влияния (объёмы, ликвидность, новости).\n\n"
        "2️⃣ **Уровни:**\n"
        "- Укажи ближайшие уровни поддержки и сопротивления, напиши конкретные цифры.\n\n"
        "3️⃣ **Сценарии на 1–3 дня:**\n"
        "🟢 **Агрессивный:** Entry, StopLoss, TakeProfit, вероятность успеха.\n"
        "🟠 **Консервативный:** Entry, StopLoss, TakeProfit, вероятность успеха.\n\n"
        "4️⃣ **Риски и подходящий стиль входа:**\n"
        "- Какие риски здесь ключевые?\n"
        "- Что лучше подходит: скальпинг, интрадей или свинг?\n\n"
        "5️⃣ **Короткая торговая рекомендация:**\n"
        "- В 1–2 строках, как сигнал для трейд-чата, например: LONG от $___ со стопом $___, цели $___.\n\n"
        "6️⃣ **Дополнительно:**\n"
        "- Какие данные ещё стоит проверить трейдеру перед входом (например стакан, отчёты китов, открытый интерес)?\n\n"
        "Отвечай строго по пунктам и только на русском языке."
    )

    try:
        response = await client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}]
        )
        await update.message.reply_text(
            f"📊 Прогноз по активу {coin}:\n\n"
            f"{response.choices[0].message.content.strip()}",
            reply_markup=CHAT_DISCUSS_KEYBOARD,
            parse_mode="Markdown"
        )
    except Exception as e:
        logging.error(f"[FORECAST_BY_PRICE] GPT ошибка: {e}")
        await update.message.reply_text("⚠️ Не удалось получить прогноз. Попробуй позже.")

async def handle_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id
    username = update.effective_user.username

    reset_commands = [
        "📏 Калькулятор риска", "🧘 Спокойствие", "🧠 Помощь профессионала",
        "📚 Объяснение термина", "📈 Получить сигнал", "📊 Прогноз по активу",
        "💰 Подключить за $25", "💵 О подписке", "🔄 Перезапустить бота", "🔍 Потенциал монеты"
    ]
    if text in reset_commands:
        context.user_data.clear()

    if text == "🔍 Потенциал монеты":
        if user_id not in ALLOWED_USERS:
            await update.message.reply_text("🔒 Доступ только после активации подписки за $25.", reply_markup=REPLY_MARKUP)
            return
        context.user_data["awaiting_potential"] = True
        await update.message.reply_text("💡 Введи тикер криптовалюты (например: BTC):")
        return

    if text == "📏 Калькулятор риска":
        return

    if text == "🧘 Спокойствие":
        return await start_therapy(update, context)

    if text == "🧠 Помощь профессионала":
        if user_id not in ALLOWED_USERS:
            await update.message.reply_text("🔒 Доступ только после активации подписки за $25.", reply_markup=REPLY_MARKUP)
            return
        context.user_data.clear()
        context.user_data["awaiting_pro_question"] = True
        await update.message.reply_text("🧑‍💼 Напиши свой вопрос — GPT-аналитик ответит.", reply_markup=REPLY_MARKUP)
        return

    if text == "📚 Объяснение термина":
        context.user_data.clear()
        context.user_data["awaiting_definition_term"] = True
        await update.message.reply_text("✍️ Напиши термин, который нужно объяснить.")
        return

    if text == "📈 Получить сигнал":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💎 Crypto", callback_data="market_crypto")],
            [InlineKeyboardButton("💱 Forex", callback_data="market_forex")]
        ])
        await update.message.reply_text(
            "⚡ Для какого рынка сделать анализ?",
            reply_markup=keyboard
        )
        return

    if text == "📊 Прогноз по активу":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📷 Прислать скрин", callback_data="forecast_by_image")]
        ])
        await update.message.reply_text(
            "📈 Пришли скрин графика — я дам прогноз на основе теханализа.",
            reply_markup=keyboard
        )
        return

    if text == "💰 Подключить за $25":
        if user_id in ALLOWED_USERS:
            await update.message.reply_text("✅ У тебя уже активирована подписка!", reply_markup=REPLY_MARKUP)
        else:
            invoice_url = await create_cryptocloud_invoice(user_id, context)
            if invoice_url:
                await update.message.reply_text(
                    f"💸 Для оплаты нажми кнопку ниже и следуй инструкциям:",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("💰 Оплатить через CryptoCloud", url=invoice_url)]
                    ])
                )
            else:
                await update.message.reply_text("⚠️ Не удалось создать счёт. Попробуй позже.")
        return

    if text == "💵 О подписке":
        await update.message.reply_text(
            "Подписка активируется через CryptoCloud.\nНажми 💰 Подключить за $25 для получения ссылки на оплату.",
            reply_markup=REPLY_MARKUP
        )
        return

    if text == "🔄 Перезапустить бота":
        context.user_data.clear()
        await update.message.reply_text("🔄 Бот перезапущен. Выбери действие:", reply_markup=REPLY_MARKUP)
        return

    if text == "📌 Сетап":
        if user_id not in ADMIN_IDS:
            await update.message.reply_text("⛔️ Эта функция доступна только админу.")
            return
        context.user_data.clear()
        await update.message.reply_text("✍️ Укажи торговый инструмент (например: BTC/USDT):")
        return SETUP_1

    context.user_data.clear()
    await update.message.reply_text("🔄 Сброс всех ожиданий. Продолжай.", reply_markup=REPLY_MARKUP)

async def gpt_psychologist_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()

    if user_text == "↩️ Выйти в меню":
        await update.message.reply_text("🔁 Возвращаемся в главное меню!", reply_markup=REPLY_MARKUP)
        return ConversationHandler.END

    prompt = (
        "Ты — GPT-психолог для трейдеров, с доброй иронией и лёгким юмором. "
        "Помогаешь справляться с лудоманией, сериями убытков и эмоциональными качелями. "
        "Избегай гендерных обращений (например 'братан', 'подруга'), используй нейтральные: 'друг', 'коллега', 'трейдер'.\n\n"
        f"Сообщение пользователя:\n{user_text}\n\n"
        "📌 Ответь строго по структуре:\n\n"
        "1️⃣ **Эмпатично отреагируй**, но без жалости. Покажи, что понимаешь чувство проигрыша.\n\n"
        "2️⃣ **Дай метафору**, чтобы трейдер понял, что просадка — это не конец. Пример: 'это как натяжение рогатки перед выстрелом'.\n\n"
        "3️⃣ **Приведи факт или историю**, что даже у топовых трейдеров бывают серии минусов (например у Сороса или Друкенмиллера)."
        " Это даст уверенность, что неудачи бывают у всех.\n\n"
        "4️⃣ **Дай одно простое микро-действие**, чтобы почувствовать контроль прямо сейчас: например закрыть терминал, записать эмоции, выйти на воздух.\n\n"
        "5️⃣ **Закрой всё трейдинг-мемом или смешной короткой цитатой**, например: '— Ты держишь позицию? — Нет, я держу слёзы 😭'.\n\n"
        "⚠️ Не используй общие фразы типа 'не переживай' или 'всё будет хорошо'. Будь конкретным, тёплым и чуть ироничным. Отвечай только на русском языке."
    )

    try:
        response = await client.chat.completions.create(
            model="gpt-4",
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
            user_id = int(order_id.replace("user_", ""))
            ALLOWED_USERS.add(user_id)
            print(f"🎉 Пользователь {user_id} активирован через POS!")

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


async def unified_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting_potential"):
        await handle_potential(update, context)
    elif context.user_data.get("awaiting_macro_text"):
        await handle_macro_text(update, context)
    elif context.user_data.get("awaiting_asset_name"):
        await handle_forecast_by_price(update, context)
    elif context.user_data.get("awaiting_definition_term"):
        await handle_definition(update, context)
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

    # 🧠 Помощь профессионала (аналитика)
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

    # 📌 Сетап (только для админа)
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

    # ✅ Регистрируем все ConversationHandlers
    app.add_handler(help_conv_handler)
    app.add_handler(therapy_handler)
    app.add_handler(risk_calc_handler)
    app.add_handler(setup_handler)

    # ✅ Обычные обработчики команд
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("restart", restart))
    app.add_handler(CommandHandler("publish", publish_post))
    app.add_handler(CommandHandler("broadcast", broadcast))

    # ✅ Inline кнопки, фото и универсальный текст
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unified_text_handler))

    # 🚀 Запускаем Telegram polling
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
        await app.bot.send_message(
            chat_id=user_id,
            text=(
                "✅ Оплата получена! Подписка активирована навсегда 🎉\n\n"
                "🤖 GPT-помощник доступен: задавай вопросы, загружай графики, получай прогнозы.\n\n"
                "🎁 Твой бонус — курс по скальпингу и позиционке:\n"
                "👉 [Открыть курс в Google Drive](https://drive.google.com/drive/folders/1EEryIr4RDtqM4WyiMTjVP1XiGYJVxktA?clckid=3f56c187)"
            ),
            parse_mode="Markdown",
            reply_markup=REPLY_MARKUP
        )
    except Exception as e:
        print(f"❌ Не удалось уведомить пользователя {user_id}: {e}")

if __name__ == '__main__':
    main()











