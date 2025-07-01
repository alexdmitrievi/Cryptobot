import os
import logging
import asyncio
import threading
import time
import re
import json
import requests
from datetime import datetime
import io
import base64

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
    CRYPTOCLOUD_API_KEY, CRYPTOCLOUD_SHOP_ID
)
from openai import AsyncOpenAI
from PIL import Image

# 📊 Google Sheets API
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# 🔥 Flask для webhook от CryptoCloud
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
    ["📈 График с уровнями", "🧘 Спокойствие"],
    ["📚 Объяснение термина", "📏 Калькулятор риска"],
    ["💰 Подключить за $25", "💵 О подписке"],
    ["📌 Сетап"]  # 👈 новая кнопка
]
REPLY_MARKUP = ReplyKeyboardMarkup(reply_keyboard, resize_keyboard=True)

CHAT_DISCUSS_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("💬 Обсудить в чате", url="https://t.me/ai4traders_chat")]
])

# Фоновая проверка платежей по username
RECEIVED_MEMOS = set()

async def check_ton_payments_periodically(application):
    try:
        while True:
            try:
                response = requests.get(
                    f"https://tonapi.io/v2/blockchain/accounts/{TON_WALLET}/transactions",
                    headers={"Authorization": f"Bearer {TON_API_TOKEN}"},
                    timeout=10
                )
                if response.status_code == 200:
                    data = response.json()
                    for tx in data.get("transactions", []):
                        if tx.get("in_msg", {}).get("value", 0) >= 3_400_000_000:
                            memo = tx["in_msg"].get("comment", "").strip()
                            if memo.startswith("@") and memo not in RECEIVED_MEMOS:
                                RECEIVED_MEMOS.add(memo)
                                username = memo[1:]
                                logging.info(f"✅ Найдена транзакция от @{username} на {tx['in_msg']['value']/1e9} TON")

                                for user_id, name in PENDING_USERS.items():
                                    if name.lower() == username.lower():
                                        if user_id not in ALLOWED_USERS:
                                            ALLOWED_USERS.add(user_id)
                                            log_payment(user_id, username)
                                            logging.info(f"✅ @{username} получил доступ")
                                        try:
                                            await application.bot.send_message(
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
                                            logging.error(f"❌ Ошибка уведомления {user_id}: {e}")
            except Exception as e:
                logging.error(f"❌ Ошибка при проверке TON-платежей: {e}")

            await asyncio.sleep(60)
    except asyncio.CancelledError:
        logging.info("✅ Задача check_ton_payments_periodically остановлена (бот завершает работу).")

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
        f"Ты — профессиональный трейдер с 10+ годами опыта именно на крипторынке.\n"
        f"1. Проанализируй фундамент и технику: как событие повлияет на ликвидность, волатильность и краткосрочные тренды BTC/ETH?\n"
        f"2. Разложи оба сценария: положительный и негативный. В каждом — укажи ключевые уровни и поведение толпы/институционалов.\n"
        f"3. Спрогнозируй последствия на 1–3 дня вперёд. Какую реакцию вызовет у розничных игроков и СМИ?\n"
        f"4. Заверши кратким торговым планом: стоит ли входить, где, каким объёмом и с каким стопом.\n"
        f"5. Что пользователь может пожалеть, если проигнорирует эту новость?\n\n"
        f"Пиши логично, структурно. В конце — резюме в стиле чата трейдеров (1-2 строки)."
    )
    
    response = await client.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "user", "content": prompt}]
    )
    await update.message.reply_text(f"📚 GPT:\n{response.choices[0].message.content.strip()}", reply_markup=REPLY_MARKUP)
    return ConversationHandler.END


async def general_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()
    style = context.user_data.get("style", "трейдинг")
    tf = context.user_data.get("timeframe", "любом")
    market = context.user_data.get("market", "общий")

    prompt = (
        f"Ты — профессиональный криптотрейдер и фондовый трейдер с опытом более 10 лет.\n"
        f"Отвечай чётко, без воды, избегай слов 'возможно', 'по-видимому', 'скорее всего'. Говори прямо и обоснованно.\n\n"
        f"Контекст:\n"
        f"• Стиль торговли: {style}\n"
        f"• Таймфрейм: {tf}\n"
        f"• Рынок: {market}\n"
        f"• Вопрос: {user_text}\n\n"
        f"Ответь по шагам:\n"
        f"1. Расставь ключевые факторы по степени важности.\n"
        f"2. Опиши основной сценарий действий.\n"
        f"3. Если он не сработает — что делать? Альтернативный вариант.\n"
        f"4. Какие риски и потенциал выгоды?\n"
        f"5. Что бы ты сделал прямо сейчас, будь на месте трейдера?\n"
        f"6. Напиши, какие данные ещё стоит проверить для подтверждения твоего сценария.\n\n"
        f"Отвечай подробно и исключительно на русском языке."
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

    if query.data == "show_wallet":
        await query.edit_message_text(
            "💸 Отправь USDT (TON) на адрес:\n\n"
            "`UQC4nBKWF5sO2UIP9sKl3JZqmmRlsGC5B7xM7ArruA61nTGR`\n\n"
            "После оплаты отправь TX hash админу или прямо сюда.",
            parse_mode="Markdown"
        )

    elif query.data == "forecast_by_image":
        WAITING_FOR_PHOTO.add(user_id)
        # ❌ не очищаем context.user_data.clear()
        context.user_data["awaiting_macro_for_image"] = True
        await query.edit_message_text(
            "📸 Пришли скрин графика (4H таймфрейм), и я дам прогноз на основе технического анализа."
        )

    elif query.data == "forecast_by_price":
        # ❌ не очищаем context.user_data.clear()
        context.user_data["awaiting_asset_name"] = True
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="🔢 Введи тикер актива (например: BTC, ETH, XRP):"
        )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    photo = update.message.photo[-1]
    file = await photo.get_file()
    original_photo_bytes = await file.download_as_bytearray()

    # Сжимаем через PIL для стабильного Vision
    image = Image.open(io.BytesIO(original_photo_bytes)).convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=80)
    compressed_photo_bytes = buffer.getvalue()

    image_base64 = base64.b64encode(compressed_photo_bytes).decode()

    # 📊 Прогноз по активу (по кнопке)
    if context.user_data.get("awaiting_macro_for_image"):
        context.user_data["graph_image_base64"] = image_base64
        await update.message.reply_text(
            "🧠 Какие новости или события сейчас влияют на рынок? (ФРС, ETF, геополитика, хардфорки и т.д.)"
        )
        context.user_data["awaiting_macro_text"] = True
        return

    # 📈 График с уровнями (по кнопке)
    if context.user_data.get("awaiting_chart"):
        context.user_data.pop("awaiting_chart")
        try:
            vision_response = await client.chat.completions.create(
                model="gpt-4o",
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": (
                            "Ты — профессиональный криптотрейдер с 10+ лет опыта.\n"
                            "Analyze the 4H crypto chart strictly by:\n"
                            "1) Trend direction (up/down/sideways)\n"
                            "2) Key support and resistance levels\n"
                            "3) Any reversal or continuation patterns\n"
                            "4) Short trade plan: entry, stop, target.\n\n"
                            "В конце скажи, что ещё стоит проверить для подтверждения (объёмы, стакан, новости)."
                        )},
                        {"type": "image_url", "image_url": {
                            "url": f"data:image/jpeg;base64,{image_base64}"
                        }}
                    ]
                }],
                max_tokens=700
            )
            analysis = vision_response.choices[0].message.content.strip()
            await update.message.reply_text(
                f"📉 Анализ графика:\n{analysis}",
                reply_markup=REPLY_MARKUP
            )
        except Exception as e:
            logging.error(f"[awaiting_chart] Vision error: {e}")
            await update.message.reply_text(
                "⚠️ Не удалось проанализировать график. Попробуй позже или пришли другой скрин."
            )
        return

    # 🆕 Если пользователь просто прислал скрин без кнопки
    context.user_data["graph_image_base64"] = image_base64
    await update.message.reply_text(
        "📸 Понял, ты прислал скрин графика.\n\n"
        "🧠 Какие новости или события сейчас влияют на рынок? (ФРС, ETF, геополитика, хардфорки и т.д.)"
    )
    context.user_data["awaiting_macro_text"] = True


async def setup_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo = update.message.photo[-1]
    file = await photo.get_file()
    photo_bytes = await file.download_as_bytearray()

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

    await context.bot.send_photo(
        chat_id='@ai4traders',
        photo=photo_bytes,
        caption=caption,
        parse_mode="Markdown"
    )

    await update.message.reply_text("✅ Сетап опубликован в канал!", reply_markup=REPLY_MARKUP)
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
        "Ты — опытный криптотрейдер с 10+ лет на рынке.\n"
        "На изображении — график криптовалюты на 4H таймфрейме.\n\n"
        "📊 Разбери строго по пунктам:\n"
        "1) Основные факторы: тренд, уровни, паттерны, объёмы.\n"
        "2) Есть ли признаки накопления, разворота или импульса?\n"
        "3) Похожие паттерны на истории графика?\n"
        f"🌐 Учитывай фундаментальный фон: {macro}\n"
        "🔁 Дай два сценария: пробой вверх и пробой вниз (уровни входа, стопа, целей, вероятность).\n"
        "📌 В конце посоветуй, что ещё проверить (объёмы, стакан, новости).\n\n"
        "Also add short English bullet summary if needed for accuracy."
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

    price = fetch_price_from_coingecko(coin)
    if price:
        price_line = f"Актуальная цена {coin} составляет ${price:.2f}.\n"
    else:
        price_line = f"(❗️ Цена {coin} не найдена автоматически. Уточни её на CoinMarketCap, Binance или TradingView.)\n"

    prompt = (
        price_line +
        f"Ты — опытный криптотрейдер с 10+ годами опыта.\n"
        f"1. Проанализируй перспективы роста {coin}: какие фундаментальные и технические сигналы подтверждают или опровергают потенциал? "
        f"Расставь факторы по степени важности.\n"
        f"2. Какие уровни важны: поддержка, сопротивление, объём?\n"
        f"3. Дай два сценария: агрессивный и консервативный. Для каждого — риск/потенциал и короткий вывод.\n"
        f"4. Сформулируй сценарий на ближайшие 3–7 дней. "
        f"Если видишь похожие недавние паттерны на рынке — укажи.\n"
        f"5. Напиши, какие данные трейдеру стоит проверить дополнительно (например: объёмы, открытый интерес).\n\n"
        f"Отвечай подробно и исключительно на русском языке."
    )

    try:
        response = await client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}]
        )
        await update.message.reply_text(
            f"📈 Потенциал монеты {coin}:\n\n"
            f"{response.choices[0].message.content.strip()}\n\n"
            f"📰 Для чтения свежих новостей на русском:\n"
            f"• [Forklog](https://t.me/forklog)\n"
            f"• [Bits.media](https://bits.media/news/)\n"
            f"• [RBC Crypto](https://www.rbc.ru/crypto/)\n"
            f"• [Investing](https://ru.investing.com/news/cryptocurrency-news/)\n\n"
            f"Подписывайся на [Forklog в Telegram](https://t.me/forklog), чтобы всегда быть в курсе.",
            reply_markup=CHAT_DISCUSS_KEYBOARD,
            parse_mode="Markdown"
        )
    except Exception as e:
        logging.error(f"[POTENTIAL] GPT ошибка: {e}")
        await update.message.reply_text("⚠️ Не удалось проанализировать монету. Попробуй позже.")



async def handle_definition(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("awaiting_definition_term", None)
    term = update.message.text.strip()

    prompt = f"Объясни кратко и понятно, что такое: {term}. Приведи пример. Стиль — как для начинающего трейдера."

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
        price_line = f"Актуальная цена {coin} — ${price:.2f}.\n"
    else:
        price_line = f"(❗️ Цена {coin} не найдена. Уточни её на CoinMarketCap или Binance.)\n"

    prompt = (
        price_line +
        f"Ты — профессиональный криптотрейдер с опытом более 10 лет.\n"
        f"1. Определи текущую рыночную структуру и тренд, расставь факторы по степени важности.\n"
        f"2. Укажи ближайшие уровни сопротивления и поддержки.\n"
        f"3. Дай два сценария движения на 1–3 дня (агрессивный и консервативный).\n"
        f"4. Определи риски и подходящий стиль входа (интрадей / свинг).\n"
        f"5. Заверши краткой торговой рекомендацией в 1-2 строках.\n"
        f"Если видишь похожие ситуации на истории — напомни о них.\n"
        f"В конце напиши, какие данные ещё проверить трейдеру.\n\n"
        f"Отвечай подробно и исключительно на русском языке."
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

    # Команды сброса
    reset_commands = [
        "📏 Калькулятор риска", "🧘 Спокойствие", "🧠 Помощь профессионала",
        "📚 Объяснение термина", "📈 График с уровнями", "📊 Прогноз по активу",
        "💰 Подключить за $25", "💵 О подписке", "🔄 Перезапустить бота", "🔍 Потенциал монеты"
    ]
    if text in reset_commands:
        context.user_data.clear()

    # 🔍 Потенциал монеты
    if text == "🔍 Потенциал монеты":
        if user_id not in ALLOWED_USERS:
            await update.message.reply_text("🔒 Доступ только после активации подписки за $25.", reply_markup=REPLY_MARKUP)
            return
        context.user_data["awaiting_potential"] = True
        await update.message.reply_text("💡 Введи тикер криптовалюты (например: BTC):")
        return

    if text == "📏 Калькулятор риска":
        return  # ConversationHandler

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

    if text == "📈 График с уровнями":
        context.user_data.clear()
        context.user_data["awaiting_chart"] = True
        await update.message.reply_text("📷 Пришли скрин графика — я проанализирую.")
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
            invoice_url = create_cryptocloud_invoice(user_id)
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

    # Всё остальное — сброс
    context.user_data.clear()
    await update.message.reply_text("🔄 Сброс всех ожиданий. Продолжай.", reply_markup=REPLY_MARKUP)


async def gpt_psychologist_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()

    if user_text == "↩️ Выйти в меню":
        await update.message.reply_text("🔁 Возвращаемся в главное меню!", reply_markup=REPLY_MARKUP)
        return ConversationHandler.END

    prompt = (
        "Ты — GPT-психолог, который помогает трейдерам после неудач, лудомании и эмоциональных срывов. "
        "Общайся легко, с доброй иронией, не бойся подколоть — но всегда будь на стороне трейдера. "
        "Избегай гендерных слов (братан, девочка и т.д.) — говори нейтрально: друг, трейдер, коллега.\n\n"
        f"Сообщение пользователя:\n{user_text}\n\n"
        "1. Отреагируй с эмпатией, но без жалости. Покажи, что ты понимаешь боль.\n"
        "2. Объясни, как текущая просадка может стать точкой роста — через метафору (например: 'это как протяжка пружины перед выстрелом').\n"
        "3. Напомни, что даже у топовых трейдеров бывают серии неудач. Приведи ободряющий факт или пример.\n"
        "4. Предложи 1 микро-действие, чтобы почувствовать контроль: закрыть терминал, записать эмоции, выйти на 3 минуты.\n"
        "5. Заверши мемом на тему трейдинга. Пример: '— Ты держишь позицию? — Нет, я держу слёзы 😭'.\n\n"
        "⚠️ Не используй фразы 'всё будет хорошо', 'не переживай'. Лучше конкретика и юмор."
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

# 🚀 Функция создания счёта через CryptoCloud
def create_cryptocloud_invoice(user_id):
    url = "https://api.cryptocloud.plus/v1/invoice/create"
    payload = {
        "shop_id": CRYPTOCLOUD_SHOP_ID,
        "amount": 25,
        "currency": "USDT",
        "order_id": f"user_{user_id}",
        "description": "Подписка GPT Trader Bot"
    }
    headers = {"Authorization": f"Token {CRYPTOCLOUD_API_KEY}"}
    response = requests.post(url, json=payload, headers=headers)
    data = response.json()
    return data["result"]["url"] if "result" in data else None

# 🚀 Flask webhook
app_flask = Flask(__name__)

@app_flask.route("/cryptocloud_webhook", methods=["POST"])
def cryptocloud_webhook():
    data = request.json
    print("Webhook от CryptoCloud:", data)

    if data.get("status") == "paid":
        order_id = data.get("order_id")
        if order_id and order_id.startswith("user_"):
            user_id = int(order_id.replace("user_", ""))
            ALLOWED_USERS.add(user_id)
            print(f"✅ Пользователь {user_id} активирован через CryptoCloud!")

    return jsonify({"ok": True})

# Отдельный поток для Flask
def run_flask():
    app_flask.run(port=5000)

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
        "🚀 *GPT-Трейдер для Telegram* — твой аналитик, VIP-сигналы и психолог в одном боте.\n\n"
        "🔍 На рынке:\n"
        "• BlackRock, Fidelity, Grayscale скупают BTC и ETH через ETF\n"
        "• Московская биржа запускает криптофьючерсы\n"
        "• TON становится стандартом для крупных расчётов в СНГ\n\n"
        "💰 Доходность крипты кратно выше:\n"
        "• Недвижимость ~8%\n"
        "• Голубые фишки РФ ~12–15%\n"
        "• BTC, ETH, TON +30–100% за сделки с чётким планом\n\n"
        "📈 GPT-Трейдер:\n"
        "• Прогнозы по скринам за 10 сек\n"
        "• Макро-аналитика и сценарии\n"
        "• VIP-сигналы (входы, стопы, фиксации)\n"
        "• GPT-психолог: поддержка и юмор после минусов\n\n"
        "🎁 Подключись сейчас и получи бонус: курс по скальпингу и позиционке (10+ уроков и PDF-гайды).\n\n"
        "🚀 *Доступ навсегда всего за $25 (~3.4 TON)*.\n\n"
        "👤 Задай вопрос 👉 [@zhbankov_alex](https://t.me/zhbankov_alex)\n"
        "💬 Обсуди рынок и сетапы в чате 👉 [ai4traders_chat](https://t.me/ai4traders_chat)"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Получить доступ", url="https://t.me/Cripto_inter_bot")]
    ])

    try:
        chat_id = '@ai4traders'
        old_pins = await context.bot.get_chat(chat_id)
        if old_pins.pinned_message:
            await context.bot.unpin_chat_message(chat_id=chat_id, message_id=old_pins.pinned_message.message_id)

        with open(PHOTO_PATH, "rb") as photo:
            message = await context.bot.send_photo(
                chat_id=chat_id,
                photo=photo,
                caption=caption,
                parse_mode="Markdown",
                reply_markup=keyboard
            )

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
    # ✅ Запускаем фоновую задачу безопасно внутри event loop
    asyncio.create_task(check_ton_payments_periodically(app))

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

if __name__ == '__main__':
    main()











