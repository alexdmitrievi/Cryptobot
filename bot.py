import logging
import asyncio
import re
import requests
from telegram import Update, BotCommand, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters, ConversationHandler
from config import TELEGRAM_TOKEN, OPENAI_API_KEY
from openai import AsyncOpenAI
from PIL import Image
import io
import base64

client = AsyncOpenAI(api_key=OPENAI_API_KEY)
logging.basicConfig(level=logging.INFO)

ALLOWED_USERS = {407721399, 592270446}  # сюда вручную добавляй user_id оплативших
TEST_USERS = set()

reply_keyboard = [
    ["📊 Прогноз по активу", "🧠 Помощь профессионала"],
    ["📈 График с уровнями", "🧘 Спокойствие"],
    ["📚 Объяснение термина", "📏 Калькулятор риска"],
    ["💰 Подключить за $25", "💵 О подписке"],
    ["🔄 Перезапустить бота"]
]
REPLY_MARKUP = ReplyKeyboardMarkup(reply_keyboard, resize_keyboard=True)

INTERPRET_NEWS, ASK_EVENT, ASK_FORECAST, ASK_ACTUAL, GENERAL_QUESTION, FOLLOWUP_1, FOLLOWUP_2, FOLLOWUP_3 = range(8)
user_inputs = {}

WAITING_FOR_PHOTO = set()
WAITING_FOR_THERAPY_INPUT = 100

RISK_CALC_1, RISK_CALC_2, RISK_CALC_3 = range(101, 104)

async def start_risk_calc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📊 Введи размер депозита в $:")
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
        await update.message.reply_text("🔒 Доступ ограничен. Активируй тест или оплати помощника.", reply_markup=REPLY_MARKUP)
        return False
    return True

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("👋 Привет! Выбери действие ниже:", reply_markup=REPLY_MARKUP)
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
        f"Проанализируй эту новость с точки зрения её влияния на криптовалюты: курс биткоина, альткоинов и общее настроение на рынке.\n"
        f"Дай краткий торговый вывод: тенденция, уровни, риски и рекомендации по действиям трейдера.\n"
        f"Пиши уверенно, избегай фраз 'возможно', 'по-видимому'."
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
        f"Ты — профессиональный трейдер с 10+ годами опыта. Отвечай уверенно, избегай фраз 'возможно', 'по-видимому'.\n"
        f"Пользователь торгует: {style}. Таймфрейм: {tf}. Рынок: {market}.\n"
        f"Вопрос: {user_text}\n"
        f"Дай конкретную, практичную рекомендацию."
    )
    response = await client.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "user", "content": prompt}]
    )
    await update.message.reply_text(f"📚 GPT:\n{response.choices[0].message.content.strip()}", reply_markup=REPLY_MARKUP)
    return ConversationHandler.END

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    if query.data == "show_wallet":
        await query.edit_message_text(
            "💸 Отправь USDT (TON) на адрес:\n\n"
            "`UQC4nBKWF5sO2UIP9sKl3JZqmmRlsGC5B7xM7ArruA61nTGR`\n\n"
            "После оплаты отправь TX hash админу или прямо сюда."
        )

    elif query.data == "forecast_by_image":
        WAITING_FOR_PHOTO.add(user_id)
        context.user_data["awaiting_macro_for_image"] = True
        await query.edit_message_text(
            "📸 Пришли скрин графика (4H таймфрейм), и я дам прогноз на основе технического анализа."
        )

    elif query.data == "forecast_by_price":
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="Введи название актива (например, BTC, ETH, XRP и т.д.):"
        )
        context.user_data["awaiting_asset_name"] = True

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    photo = update.message.photo[-1]
    file = await photo.get_file()
    photo_bytes = await file.download_as_bytearray()

    # 📈 График с уровнями
    if context.user_data.get("awaiting_chart"):
        context.user_data.pop("awaiting_chart")
        try:
            vision_response = await client.chat.completions.create(
                model="gpt-4o",
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "На изображении график криптовалюты. Определи уровни поддержки и сопротивления, тренд и действия трейдера."},
                        {"type": "image_url", "image_url": {
                            "url": f"data:image/jpeg;base64,{base64.b64encode(photo_bytes).decode()}"
                        }}
                    ]
                }],
                max_tokens=600
            )
            analysis = vision_response.choices[0].message.content.strip()
            await update.message.reply_text(f"📉 Анализ графика:\n{analysis}", reply_markup=REPLY_MARKUP)
        except Exception as e:
            logging.error(f"[awaiting_chart] Ошибка анализа графика: {e}")
            await update.message.reply_text("⚠️ Не удалось проанализировать график. Попробуй позже.")
        return

    # 📊 Прогноз по скрину графика (через кнопку)
    if context.user_data.get("awaiting_macro_for_image"):
        context.user_data["graph_image_base64"] = base64.b64encode(photo_bytes).decode("utf-8")
        context.user_data["awaiting_macro_for_image"] = True
        await update.message.reply_text(
            "🧠 Что сейчас происходит в мире? (например, новости, конфликты, решения центробанков и т.д.)"
        )
        return

    # Если ни один режим не был активен
    await update.message.reply_text("🤖 Не понял, что делать с изображением. Попробуй выбрать действие из меню.")

async def handle_macro_for_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_macro_for_image"):
        return

    del context.user_data["awaiting_macro_for_image"]
    macro = update.message.text.strip()
    image_base64 = context.user_data.pop("graph_image_base64", None)

    if not image_base64:
        await update.message.reply_text("⚠️ Ошибка: изображение не найдено.")
        return

    prompt = (
        "Ты — профессиональный трейдер с опытом более 10 лет.\n"
        "На изображении представлен график криптовалюты на 4H таймфрейме.\n"
        "Проанализируй его с точки зрения технического анализа: уровни, тренды, фигуры и индикаторы.\n\n"
        f"Также учти фундаментальный фон:\n{macro}\n\n"
        "Сделай краткий торговый вывод: возможное направление, риски и рекомендации для трейдера.\n"
        "Отвечай уверенно, избегай фраз 'возможно', 'по-видимому'."
    )

    try:
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/jpeg;base64,{image_base64}"
                    }}
                ]}
            ],
            max_tokens=600
        )

        await update.message.reply_text(
            f"📈 Прогноз по графику с учётом фундамента:\n{response.choices[0].message.content.strip()}",
            reply_markup=REPLY_MARKUP
        )

    except Exception as e:
        logging.error(f"[MACRO_GRAPH] Ошибка анализа: {e}")
        await update.message.reply_text("⚠️ Произошла ошибка при анализе. Попробуй позже.")

async def handle_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id

    # 📍 Кнопки, которые сбрасывают состояния
    known_buttons = [
        "📊 Прогноз по активу", "🧠 Помощь профессионала",
        "📈 График с уровнями", "🧘 Спокойствие",
        "📚 Объяснение термина", "📏 Калькулятор риска",
        "💰 Подключить за $25", "💵 О подписке", "🔄 Перезапустить бота"
    ]
    if text in known_buttons:
        context.user_data.clear()
        await update.message.reply_text("🔄 Сброс всех ожиданий. Продолжай.", reply_markup=REPLY_MARKUP)
        return ConversationHandler.END

    # 🔄 Перезапуск
    if text == "🔄 Перезапустить бота":
        context.user_data.clear()
        await update.message.reply_text("🔄 Бот перезапущен. Выбери действие:", reply_markup=REPLY_MARKUP)
        return ConversationHandler.END

    # 📚 Объяснение термина
    if text == "📚 Объяснение термина":
        await update.message.reply_text("✍️ Напиши термин, который нужно объяснить. Пример: шорт")
        return

    # 📈 График с уровнями
    if text == "📈 График с уровнями":
        await update.message.reply_text("📷 Пришли скрин графика — я найду уровни и прокомментирую ситуацию на рынке")
        context.user_data["awaiting_chart"] = True
        return

    # 📊 Прогноз по активу
    if text == "📊 Прогноз по активу":
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📷 Прислать скрин", callback_data="forecast_by_image"),
                InlineKeyboardButton("🔢 Ввести цену", callback_data="forecast_by_price")
            ]
        ])
        await update.message.reply_text("Выбери способ прогноза:", reply_markup=keyboard)
        return

    # 🧠 Помощь профессионала (GPT-аналитик)
    if text == "🧠 Помощь профессионала":
        if user_id not in ALLOWED_USERS:
            await update.message.reply_text("🔒 Доступ только после активации подписки за $25.", reply_markup=REPLY_MARKUP)
            return
        await update.message.reply_text(
            "🧑‍💼 Напиши свой вопрос по трейдингу, инвестициям или анализу — GPT-аналитик ответит.",
            reply_markup=REPLY_MARKUP
        )
        context.user_data["awaiting_pro_question"] = True
        return

    # 🧘 Спокойствие
    if text == "🧘 Спокойствие":
        await update.message.reply_text(
            "😵 Ну что, опять рынок побрил как барбер в пятницу? Бывает, дружище.\n\n"
            "Напиши, что случилось — GPT-психолог с доброй иронией выслушает, подбодрит и вставит мем.\n\n"
            "Когда захочешь вернуться к аналитике — просто нажми «⏩ Выйти в меню».",
            reply_markup=ReplyKeyboardMarkup([["⏩ Выйти в меню"]], resize_keyboard=True)
        )
        context.user_data["awaiting_therapy"] = True
        return

    # 💰 Подключить за $25
    if "Подключить" in text or "Оплатить" in text:
        await update.message.reply_text(
            "💸 Стоимость подписки: **навсегда за $25**\n\n"
            "Отправь USDT в сети TON на адрес:\n\n"
            "`UQC4nBKWF5sO2UIP9sKl3JZqmmRlsGC5B7xM7ArruA61nTGR`\n\n"
            "После оплаты пришли TX hash админу или сюда для активации.",
            reply_markup=REPLY_MARKUP,
            parse_mode="Markdown"
        )
        return

    # 💵 О подписке
    if "О подписке" in text:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Оплатить через TON", callback_data="show_wallet")]
        ])
        await update.message.reply_text(
            "🔐 **Открытие доступа**\n\n"
            "• Подписка: **навсегда за $25**\n"
            "• Оплата: USDT (TON)\n"
            "• Доступ ко всем функциям бота\n\n"
            "После оплаты — просто отправь хеш транзакции сюда.",
            reply_markup=keyboard
        )
        return

    # 📏 Калькулятор риска (ввод по шагам)
    if text == "📏 Калькулятор риска":
        context.user_data["awaiting_deposit"] = True
        await update.message.reply_text("📊 Введи размер депозита в $:")
        return

    if context.user_data.get("awaiting_deposit"):
        try:
            deposit = float(text.replace(",", "."))
            context.user_data["deposit"] = deposit
            context.user_data["awaiting_deposit"] = False
            context.user_data["awaiting_risk"] = True
            await update.message.reply_text("📉 Введи риск на сделку в %:")
        except:
            await update.message.reply_text("❗ Введи число. Пример: 1000")
        return

    if context.user_data.get("awaiting_risk"):
        try:
            risk_percent = float(text.replace(",", "."))
            context.user_data["risk_percent"] = risk_percent
            context.user_data["awaiting_risk"] = False
            context.user_data["awaiting_sl"] = True
            await update.message.reply_text("🛑 Введи размер стоп-лосса в $:")
        except:
            await update.message.reply_text("❗ Введи число. Пример: 1000")
        return

    if context.user_data.get("awaiting_sl"):
        try:
            sl = float(text.replace(",", "."))
            deposit = context.user_data.pop("deposit")
            risk_percent = context.user_data.pop("risk_percent")
            context.user_data.pop("awaiting_sl")

            risk_usd = deposit * risk_percent / 100
            position_size = risk_usd / sl

            await update.message.reply_text(
                f"📏 Размер позиции: `{position_size:.2f}$`\n"
                f"(риск {risk_percent:.2f}%, стоп {sl}$, депозит {deposit}$)",
                reply_markup=REPLY_MARKUP,
                parse_mode="Markdown"
            )
        except:
            await update.message.reply_text("❗ Введи число. Пример: 1000")
        return

    # 🧠 Ответ на вопрос по трейдингу
    if context.user_data.get("awaiting_pro_question"):
        context.user_data.pop("awaiting_pro_question")
        prompt = (
            f"Ты — опытный трейдер. Ответь на вопрос начинающего:\n\n"
            f"{text}\n\n"
            f"Объясни кратко, по существу, избегай воды и общих фраз. Стиль — профессиональный, но дружелюбный."
        )
        response = await client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}]
        )
        await update.message.reply_text(
            f"📘 Ответ:\n{response.choices[0].message.content.strip()}",
            reply_markup=REPLY_MARKUP
        )
        return

    # 🖼️ Прогноз по скрину
    if context.user_data.get("awaiting_macro_for_image"):
        await handle_macro_for_image(update, context)
        return

    # 📘 Автообъяснение термина
    if 2 <= len(text) <= 40 and len(text.split()) <= 3 and all(c.isalnum() or c in "-_ " for c in text):
        prompt = (
            f"Ты — крипто-трейдер и аналитик. Объясни термин из мира криптовалют и трейдинга:\n"
            f"{text.strip()}\n\n"
            f"🔸 Объясни просто, без академичности.\n"
            f"🔸 Приведи пример из крипторынка.\n"
            f"🔸 Избегай слишком общего стиля — делай упор на практику трейдера."
        )
        response = await client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}]
        )
        await update.message.reply_text(
            f"📘 Объяснение:\n{response.choices[0].message.content.strip()}",
            reply_markup=REPLY_MARKUP
        )
        return

    # 🤖 Нераспознанный ввод
    await update.message.reply_text("🤖 Я не понял запрос. Попробуй выбрать действие из меню.", reply_markup=REPLY_MARKUP)


async def gpt_psychologist_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()

    if user_text == "↩️ Выйти в меню":
        await update.message.reply_text("🔁 Возвращаемся в главное меню!", reply_markup=REPLY_MARKUP)
        return ConversationHandler.END

    prompt = (
        "Ты — GPT-психолог с юмором. Ты поддерживаешь трейдеров после неудач, лудомании и паники. "
        "Общайся легко, с доброй иронией, используй эмодзи, не бойся подколоть — но всегда поддерживай и подбадривай. "
        "Избегай обращения по полу: не используй слова типа 'братан', 'девочка', 'мужик' — говори нейтрально: 'друг', 'трейдер', 'коллега'.\n\n"
        f"Сообщение от пользователя:\n{user_text}\n\n"
        "Добавь в конце подходящий текстовый мем (не картинку), связанный с трейдингом. Пример мема: '— Ты держишь позицию? — Нет, я держу слёзы 😭'"
    )

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

# 👇 ВСТАВЬ ЗДЕСЬ:
ADMIN_IDS = {407721399}  # замени на свой user_id

async def publish_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔️ У тебя нет прав на публикацию.")
        return

    logging.info(f"[COMMAND] /publish от {user_id}")
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🤖 Открыть GPT-помощника", url="https://t.me/Parser_newbot")]
    ])
    
    text = (
        "🧠 **GPT-Помощник для трейдера** — твой личный аналитик, наставник и психолог в Telegram\n\n"
        "🔍 Хватает гадать по графику? Смотри, что ты получаешь:\n"
        "• 📈 Прогноз по скрину графика за 10 секунд\n"
        "• 📰 Интерпретация макроэкономических новостей с торговыми идеями\n"
        "• 💬 Ответы под твой стиль: скальпинг, позиционка или инвестиции\n"
        "• 🧘 GPT-психолог с мемами для восстановления после просадки\n\n"
        "🔥 Всё это — в одном боте, без VPN, ChatGPT или заморочек\n"
        "💰 Всего от $1 в день. Уже 500+ трейдеров подключились.\n\n"
        "👤 Хочешь индивидуальную консультацию? [@zhbankov_alex](https://t.me/zhbankov_alex)\n"
        "🎁 Новичкам: [Бесплатный гайд по основам трейдинга](https://t.me/zhbankov_alex/33)"
    )

    message = await context.bot.send_message(chat_id='@Cripto_inter_bot', text=text, reply_markup=keyboard)
    await context.bot.pin_chat_message(chat_id='@Cripto_inter_bot', message_id=message.message_id, disable_notification=True)

async def unified_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting_macro_for_image"):
        await handle_macro_for_image(update, context)
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
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # ⛑️ Хендлер "🧘 Спокойствие" через ConversationHandler
    therapy_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🧘 Спокойствие$"), start_therapy)],
        states={
            WAITING_FOR_THERAPY_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, gpt_psychologist_response)]
        },
        fallbacks=[
            CommandHandler("start", start),
            CommandHandler("restart", restart),
            MessageHandler(filters.Regex("^🔄 Перезапустить бота$"), restart)
        ]
    )

    # 📏 Калькулятор риска через ConversationHandler
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

    # 📈 Хендлер для старта и перезапуска
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("restart", restart))
    app.add_handler(CommandHandler("publish", publish_post))

    # 🧠 Помощь профессионала (новости/вопросы) через ConversationHandler
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

    # 🖼️ Обработка фото
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    # 📥 Кнопки с callback_data
    app.add_handler(CallbackQueryHandler(button_handler))

    # 📲 Unified текстовый обработчик (остальной текст)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unified_text_handler))

    # 🔁 Register conversation flows
    app.add_handler(therapy_handler)
    app.add_handler(risk_calc_handler)
    app.add_handler(help_conv_handler)

    # 📌 Команды в меню
    app.post_init = post_init

    # ▶️ Запуск
    app.run_polling()

if __name__ == '__main__':
    main()











