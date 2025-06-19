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
    ["🧘 Спокойствие", "🏁 Тестовый период"],
    ["💰 Оплатить помощника", "💵 Тарифы /prices"]
]
REPLY_MARKUP = ReplyKeyboardMarkup(reply_keyboard, resize_keyboard=True)

INTERPRET_NEWS, ASK_EVENT, ASK_FORECAST, ASK_ACTUAL, GENERAL_QUESTION, FOLLOWUP_1, FOLLOWUP_2, FOLLOWUP_3 = range(8)
user_inputs = {}

WAITING_FOR_PHOTO = set()
WAITING_FOR_THERAPY_INPUT = 100


async def check_access(update: Update):
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USERS:
        await update.message.reply_text("🔒 Доступ ограничен. Активируй тест или оплати помощника.", reply_markup=REPLY_MARKUP)
        return False
    return True

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Привет! Выбери действие ниже:", reply_markup=REPLY_MARKUP)

async def help_pro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return ConversationHandler.END
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
        f"Факт: {data['actual']}\n"
        f"Проанализируй новость и дай торговую рекомендацию.\n"
        f"Ты — профессиональный трейдер с 10+ годами опыта. Отвечай уверенно, избегай фраз 'возможно', 'по-видимому'."
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
        await query.edit_message_text("📸 Пришли скрин графика (4H таймфрейм), и я дам прогноз на основе технического анализа.")

    elif query.data == "forecast_by_price":
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="Введи название актива (например, BTC, ETH, XRP и т.д.):"
        )
        context.user_data["awaiting_asset_name"] = True

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in WAITING_FOR_PHOTO:
        return  # Игнорируем случайные фото

    WAITING_FOR_PHOTO.discard(user_id)
    if not await check_access(update):
        return

    photo = update.message.photo[-1]
    file = await photo.get_file()
    photo_bytes = await file.download_as_bytearray()

    context.user_data["graph_image_base64"] = base64.b64encode(photo_bytes).decode("utf-8")
    await update.message.reply_text("🧠 Что сейчас происходит в мире? (например, новости, конфликты, решения центробанков и т.д.)")
    context.user_data["awaiting_macro_for_image"] = True

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
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}}
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
    text = update.message.text
    user_id = update.effective_user.id

    if text == "📊 Прогноз по активу":
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📷 Прислать скрин", callback_data="forecast_by_image"),
                InlineKeyboardButton("🔢 Ввести цену", callback_data="forecast_by_price")
            ]
        ])
        await update.message.reply_text("Выбери способ прогноза:", reply_markup=keyboard)
        return

    elif "awaiting_asset_name" in context.user_data:
        context.user_data["price_asset"] = update.message.text.strip().upper()
        del context.user_data["awaiting_asset_name"]
        context.user_data["awaiting_price_input"] = True
        await update.message.reply_text("Введи текущую цену актива:")
        return

    elif "awaiting_price_input" in context.user_data:
        context.user_data["price_value"] = update.message.text.strip()
        del context.user_data["awaiting_price_input"]
        context.user_data["awaiting_macro_input"] = True
        await update.message.reply_text("Что сейчас происходит в мире? (например, новости, конфликты, заявления ФРС и т.д.)")
        return

    elif "awaiting_macro_input" in context.user_data:
        asset = context.user_data.pop("price_asset")
        price = context.user_data.pop("price_value")
        macro = update.message.text.strip()
        del context.user_data["awaiting_macro_input"]

        prompt = (
            f"Ты — профессиональный трейдер с опытом более 10 лет в трейдинге.\n"
            f"Анализируй актив {asset}, текущая цена {price}.\n"
            f"Учитывай следующий фундаментальный фон: {macro}.\n\n"
            f"Дай краткий прогноз, укажи ближайшие уровни поддержки и сопротивления, текущий тренд и потенциальные действия трейдера.\n"
            f"Пиши уверенно, избегай фраз 'возможно', 'по-видимому'."
        )

        response = await client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}]
        )
        await update.message.reply_text(
            f"📊 Прогноз по {asset} с учетом фундамента:\n{response.choices[0].message.content.strip()}",
            reply_markup=REPLY_MARKUP
        )
        return

    elif text == "🏁 Тестовый период":
        if user_id in TEST_USERS or user_id in ALLOWED_USERS:
            await update.message.reply_text("⏳ Ты уже использовал тест.")
        else:
            ALLOWED_USERS.add(user_id)
            TEST_USERS.add(user_id)
            await update.message.reply_text("✅ Тестовый доступ активирован на 1 сессию.")

    elif text == "💰 Оплатить помощника":
        await update.message.reply_text(
            "Отправь USDT в сети TON на адрес:\n\n"
            "`UQC4nBKWF5sO2UIP9sKl3JZqmmRlsGC5B7xM7ArruA61nTGR`\n\n"
            "После оплаты пришли TX hash админу или сюда для активации.",
            reply_markup=REPLY_MARKUP
        )

    elif text == "💵 Тарифы /prices":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Оплатить TON", callback_data="show_wallet")]
        ])
        text = (
            "💰 Тарифы на подписку:\n\n"
            "• 1 месяц — $25\n"
            "• 3 месяца — $60 (экономия $15)\n"
            "• 6 месяцев — $100 (экономия $50)\n"
            "• 12 месяцев — $180 (экономия $120)\n"
            "• Пожизненно — $299\n"
        )
        await update.message.reply_text(text, reply_markup=keyboard)

async def gpt_psychologist_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()
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
    await update.message.reply_text(
        f"🧘 GPT-психолог:\n{response.choices[0].message.content.strip()}",
        reply_markup=REPLY_MARKUP
    )
    return ConversationHandler.END


async def start_therapy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "😵‍💫 Ну что, опять рынок побрил как барбер в пятницу? Бывает, дружище.\n\n"
        "Напиши, что случилось — GPT-психолог с доброй иронией выслушает, поддержит и вставит мем, чтобы ты снова почувствовал силу 💪",
        reply_markup=ReplyKeyboardRemove()
    )
    return WAITING_FOR_THERAPY_INPUT

async def post_init(app):
    await app.bot.set_my_commands([BotCommand("start", "Запуск бота")])

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
        "🚀 **GPT-Помощник для трейдинга по новостям — прямо в Telegram**\n\n"
        "💬 Индивидуальные консультации от опытных трейдеров\n"
        "📈 Мгновенные интерпретации макроэкономических новостей\n"
        "🎯 Точки входа для скальпинга и позиционной торговли\n"
        "📚 Еженедельные обзоры и обучающие материалы\n"
        "🌍 Без VPN, без ChatGPT — всё внутри Telegram\n"
        "🤝 Ты также получаешь доступ к сильному комьюнити трейдеров\n\n"
        "🔥 Это не просто подписка на GPT — это инструмент + поддержка + опыт"
    )

    message = await context.bot.send_message(chat_id='@Cripto_inter_bot', text=text, reply_markup=keyboard)
    await context.bot.pin_chat_message(chat_id='@Cripto_inter_bot', message_id=message.message_id, disable_notification=True)

async def unified_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting_macro_for_image"):
        await handle_macro_for_image(update, context)
    else:
        await handle_main(update, context)

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^📊 Помощь профессионала$"), help_pro),
            MessageHandler(filters.Regex("^🧘 Спокойствие$"), start_therapy)
        ],
        states={
            INTERPRET_NEWS: [MessageHandler(filters.TEXT & ~filters.COMMAND, interpret_decision)],
            ASK_EVENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_forecast)],
            ASK_FORECAST: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_actual)],
            ASK_ACTUAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, generate_interpretation)],
            FOLLOWUP_1: [MessageHandler(filters.TEXT & ~filters.COMMAND, followup_strategy)],
            FOLLOWUP_2: [MessageHandler(filters.TEXT & ~filters.COMMAND, followup_timeframe)],
            FOLLOWUP_3: [MessageHandler(filters.TEXT & ~filters.COMMAND, followup_market)],
            GENERAL_QUESTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, general_response)],
            WAITING_FOR_THERAPY_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, gpt_psychologist_response)]
        },
        fallbacks=[CommandHandler("start", start)]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("publish", publish_post))
    app.add_handler(conv_handler)

    # 📌 Универсальный хендлер для всего текстового ввода
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unified_text_handler))

    # 📍 Обработка кнопок и изображений
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    app.post_init = post_init
    app.run_polling()

if __name__ == '__main__':
    main()











