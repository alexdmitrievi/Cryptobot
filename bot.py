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
    ["📊 Помощь профессионала"],
    ["📉 Прогноз по BTC", "📉 Прогноз по ETH"],
    ["📷 Прогноз по скрину"],
    ["🏁 Тестовый период", "💰 Оплатить помощника"],
    ["💵 Тарифы /prices"]
]
REPLY_MARKUP = ReplyKeyboardMarkup(reply_keyboard, resize_keyboard=True)

INTERPRET_NEWS, ASK_EVENT, ASK_FORECAST, ASK_ACTUAL, GENERAL_QUESTION, FOLLOWUP_1, FOLLOWUP_2, FOLLOWUP_3 = range(8)
user_inputs = {}

WAITING_FOR_PHOTO = set()

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
    if query.data == "show_wallet":
        await query.edit_message_text(
            "💸 Отправь USDT (TON) на адрес:\n\n`UQC4nBKWF5sO2UIP9sKl3JZqmmRlsGC5B7xM7ArruA61nTGR`\n\nПосле оплаты отправь TX hash админу или прямо сюда."
        )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in WAITING_FOR_PHOTO:
        return  # игнорируем случайные фото

    WAITING_FOR_PHOTO.discard(user_id)
    if not await check_access(update): return

    photo = update.message.photo[-1]
    file = await photo.get_file()
    photo_bytes = await file.download_as_bytearray()

    image_base64 = base64.b64encode(photo_bytes).decode("utf-8")

    prompt = (
        "На изображении представлен график криптовалюты на 4-часовом таймфрейме.\n"
        "Проанализируй его с точки зрения технического анализа: найди уровни поддержки/сопротивления, тренды, фигуры и индикаторы, если они видны.\n"
        "Сделай краткий торговый вывод: возможное направление, риски и подходящие действия трейдера.\n"
        "Не используй фундаментальный анализ и новости."
    )

    response = await client.chat.completions.create(
        model="gpt-4-vision-preview",
        messages=[
            {"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}}
            ]}
        ],
        max_tokens=500
    )

    await update.message.reply_text(f"📈 Прогноз по графику:\n{response.choices[0].message.content.strip()}", reply_markup=REPLY_MARKUP)

async def handle_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id

    # Обработка кнопок
    if text == "📉 Прогноз по BTC":
        context.user_data["price_asset"] = "BTC"
        await update.message.reply_text("Введите текущую цену BTC:")
        return

    elif text == "📉 Прогноз по ETH":
        context.user_data["price_asset"] = "ETH"
        await update.message.reply_text("Введите текущую цену ETH:")
        return

    elif "price_asset" in context.user_data:
        asset = context.user_data.pop("price_asset")
        price = update.message.text.strip()

        prompt = (
            f"Ты — профессиональный трейдер. Дай краткий прогноз по {asset} при текущей цене {price}.\n"
            f"Укажи ближайшие уровни поддержки и сопротивления, а также обоснование, куда может пойти цена.\n"
            f"Пиши уверенно, избегай фраз 'возможно', 'по-видимому'."
        )
        response = await client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}]
        )
        await update.message.reply_text(
            f"📊 GPT-прогноз по {asset}:\n{response.choices[0].message.content.strip()}",
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

    elif text == "📷 Прогноз по скрину":
        WAITING_FOR_PHOTO.add(user_id)
        await update.message.reply_text("📸 Пришли скрин графика на 4H таймфрейме.")
        return



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

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📊 Помощь профессионала$"), help_pro)],
        states={
            INTERPRET_NEWS: [MessageHandler(filters.TEXT & ~filters.COMMAND, interpret_decision)],
            ASK_EVENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_forecast)],
            ASK_FORECAST: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_actual)],
            ASK_ACTUAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, generate_interpretation)],
            FOLLOWUP_1: [MessageHandler(filters.TEXT & ~filters.COMMAND, followup_strategy)],
            FOLLOWUP_2: [MessageHandler(filters.TEXT & ~filters.COMMAND, followup_timeframe)],
            FOLLOWUP_3: [MessageHandler(filters.TEXT & ~filters.COMMAND, followup_market)],
            GENERAL_QUESTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, general_response)]
        },
        fallbacks=[CommandHandler("start", start)]
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("publish", publish_post))
    app.add_handler(conv_handler)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_main))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.post_init = post_init
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.run_polling()

if __name__ == '__main__':
    main()











