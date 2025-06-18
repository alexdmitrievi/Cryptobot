import logging
import asyncio
import re
import requests
from telegram import Update, BotCommand, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters, ConversationHandler
from config import TELEGRAM_TOKEN, OPENAI_API_KEY
from openai import AsyncOpenAI

client = AsyncOpenAI(api_key=OPENAI_API_KEY)
logging.basicConfig(level=logging.INFO)

ALLOWED_USERS = {407721399}  # сюда вручную добавляй user_id оплативших
TEST_USERS = set()

reply_keyboard = [
    ["📊 Помощь профессионала"],
    ["📉 Прогноз по BTC", "📉 Прогноз по ETH"],
    ["🏁 Тестовый период", "💰 Оплатить помощника"],
    ["💵 Тарифы /prices"]
],
    ["📉 Прогноз по BTC", "📉 Прогноз по ETH"],
    ["🏁 Тестовый период", "💰 Оплатить помощника"],
    ["💵 Тарифы /prices"]
],
    ["📉 Прогноз по BTC", "📉 Прогноз по ETH"],
    ["📊 Оценить альтсезон"],
    ["🏁 Тестовый период", "💰 Оплатить помощника"],
    ["💵 Тарифы /prices"]
]
REPLY_MARKUP = ReplyKeyboardMarkup(reply_keyboard, resize_keyboard=True)

INTERPRET_NEWS, ASK_EVENT, ASK_FORECAST, ASK_ACTUAL, GENERAL_QUESTION, FOLLOWUP_1, FOLLOWUP_2, FOLLOWUP_3 = range(8)
user_inputs = {}

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
        "Проанализируй новость и дай торговую рекомендацию кратко: влияние на доллар, фондовый рынок и криптовалюты."
    )
    response = await client.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "user", "content": prompt}]
    )
    await update.message.reply_text(f"📊 GPT:\n{response.choices[0].message.content.strip()}", reply_markup=REPLY_MARKUP)
    return ConversationHandler.END

async def general_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()
    style = context.user_data.get("style", "трейдинг")
    tf = context.user_data.get("timeframe", "любом")
    market = context.user_data.get("market", "общий")
    prompt = (
        f"Ты — профессиональный трейдер с 10+ годами опыта. Отвечай уверенно, точно и без лишней неопределённости. "
        f"Избегай фраз вроде 'по-видимому', 'возможно', 'может быть'. Формулируй выводы чётко и по существу. "
        f"Стиль общения — уверенный наставник. Пользователь торгует: {style}. Таймфрейм: {tf}. Рынок: {market}.

"
        f"Вопрос: {user_text}

"
        f"Дай конкретную, практичную рекомендацию. Если вопрос неконкретный — уточни, что нужно."
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

async def handle_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id
    # Обработка прогноза после ввода цены
    if "price_asset" in context.user_data:
        asset = context.user_data["price_asset"]
        price = update.message.text.strip()

        prompt = (
            f"Ты — профессиональный трейдер. Дай краткий прогноз по {asset} при текущей цене {price}.
"
            f"Укажи ближайшие уровни поддержки и сопротивления, а также обоснование, куда может пойти цена.
"
            f"Пиши уверенно, избегай фраз 'возможно', 'по-видимому'."
        )
        response = await client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}]
        )
        await update.message.reply_text(f"📊 GPT-прогноз по {asset}:
{response.choices[0].message.content.strip()}", reply_markup=REPLY_MARKUP)
        context.user_data.pop("price_asset")
        return

        context.user_data["price_asset"] = "BTC"
        await update.message.reply_text("Введите текущую цену BTC:")
    elif text == "📉 Прогноз по ETH":
        context.user_data["price_asset"] = "ETH"
        await update.message.reply_text("Введите текущую цену ETH:")
    
    elif text == "🏁 Тестовый период":
        if user_id in TEST_USERS or user_id in ALLOWED_USERS:
            await update.message.reply_text("⏳ Ты уже использовал тест.")
        else:
            ALLOWED_USERS.add(user_id)
            TEST_USERS.add(user_id)
            await update.message.reply_text("✅ Тестовый доступ активирован на 1 сессию.")
    elif text == "💰 Оплатить помощника":
        await update.message.reply_text("Отправь USDT в сети TON на адрес:\n\n`UQC4nBKWF5sO2UIP9sKl3JZqmmRlsGC5B7xM7ArruA61nTGR`\n\nПосле оплаты пришли TX hash админу или сюда для активации.", reply_markup=REPLY_MARKUP)
    elif text == "💵 Тарифы /prices":
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("💳 Оплатить TON", callback_data="show_wallet")]])
        text = (
            "💰 Тарифы на подписку:\n\n"
            "• 1 месяц — $25\n"
            "• 3 месяца — $60 (экономия 15$)\n"
            "• 6 месяцев — $100 (экономия 50$)\n"
            "• 12 месяцев — $180 (экономия 120$)\n"
            "• Пожизненно — $299\n\n"
        )
        await update.message.reply_text(text, reply_markup=keyboard)

async def post_init(app):
    await app.bot.set_my_commands([BotCommand("start", "Запуск бота")])

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
    app.add_handler(conv_handler)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_main))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.post_init = post_init
    app.run_polling()

if __name__ == '__main__':
    main()








