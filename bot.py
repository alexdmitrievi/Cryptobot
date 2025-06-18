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
    ["\ud83d\udcca Помощь профессионала"],
    ["\ud83d\udcc9 Прогноз по BTC", "\ud83d\udcc9 Прогноз по ETH"],
    ["\ud83d\udcca Оценить альтсезон"],
    ["\ud83c\udff1 Тестовый период", "\ud83d\udcb0 Оплатить помощника"],
    ["\ud83d\udcb5 Тарифы /prices"]
]
REPLY_MARKUP = ReplyKeyboardMarkup(reply_keyboard, resize_keyboard=True)

INTERPRET_NEWS, ASK_EVENT, ASK_FORECAST, ASK_ACTUAL, GENERAL_QUESTION, FOLLOWUP_1, FOLLOWUP_2, FOLLOWUP_3 = range(8)
user_inputs = {}

async def check_access(update: Update):
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USERS:
        await update.message.reply_text("\ud83d\udd12 Доступ ограничен. Активируй тест или оплати помощника.", reply_markup=REPLY_MARKUP)
        return False
    return True

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("\ud83d\udc4b Привет! Выбери действие ниже:", reply_markup=REPLY_MARKUP)

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
    await update.message.reply_text(f"\ud83d\udcca GPT:\n{response.choices[0].message.content.strip()}", reply_markup=REPLY_MARKUP)
    return ConversationHandler.END

async def general_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()
    style = context.user_data.get("style", "трейдинг")
    tf = context.user_data.get("timeframe", "любом")
    market = context.user_data.get("market", "общий")
    prompt = (
        f"Ты — опытный трейдер с 10+ годами практики. Объясни понятным, простым языком, избегая жаргона, "
        f"но оставайся профессиональным. Пользователь торгует: {style}. Таймфрейм: {tf}. Рынок: {market}.\n\n"
        f"Вопрос: {user_text}\n\n"
        f"Дай чёткий, практичный и полезный ответ, как будто ты обучаешь новичка или среднего уровня трейдера. "
        f"Если есть риски — предупреди. Если вопрос неконкретный — задай уточняющий."
    )
    response = await client.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "user", "content": prompt}]
    )
    await update.message.reply_text(f"\ud83d\udcda GPT:\n{response.choices[0].message.content.strip()}", reply_markup=REPLY_MARKUP)
    return ConversationHandler.END

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "show_wallet":
        await query.edit_message_text(
            "\ud83d\udcb8 Отправь USDT (TON) на адрес:\n\n`UQC4nBKWF5sO2UIP9sKl3JZqmmRlsGC5B7xM7ArruA61nTGR`\n\nПосле оплаты отправь TX hash админу или прямо сюда."
        )

async def handle_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id

    if text == "\ud83d\udcc9 Прогноз по BTC":
        context.user_data["price_asset"] = "BTC"
        await update.message.reply_text("Введите текущую цену BTC:")
    elif text == "\ud83d\udcc9 Прогноз по ETH":
        context.user_data["price_asset"] = "ETH"
        await update.message.reply_text("Введите текущую цену ETH:")
    elif text == "\ud83d\udcca Оценить альтсезон":
        if not await check_access(update): return
        data = requests.get("https://api.coingecko.com/api/v3/global").json()
        btc_d = round(data["data"]["market_cap_percentage"]["btc"], 2)
        eth_d = round(data["data"]["market_cap_percentage"]["eth"], 2)
        eth_btc = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=btc").json()["ethereum"]["btc"]
        prompt = f"BTC Dominance: {btc_d}%\nETH Dominance: {eth_d}%\nETH/BTC: {eth_btc}\nОцени вероятность альтсезона."
        response = await client.chat.completions.create(model="gpt-4", messages=[{"role": "user", "content": prompt}])
        await update.message.reply_text(response.choices[0].message.content.strip(), reply_markup=REPLY_MARKUP)
    elif text == "\ud83c\udff1 Тестовый период":
        if user_id in TEST_USERS or user_id in ALLOWED_USERS:
            await update.message.reply_text("\u23f3 Ты уже использовал тест.")
        else:
            ALLOWED_USERS.add(user_id)
            TEST_USERS.add(user_id)
            await update.message.reply_text("\u2705 Тестовый доступ активирован на 1 сессию.")
    elif text == "\ud83d\udcb0 Оплатить помощника":
        await update.message.reply_text("Отправь USDT в сети TON на адрес:\n\n`UQC4nBKWF5sO2UIP9sKl3JZqmmRlsGC5B7xM7ArruA61nTGR`\n\nПосле оплаты пришли TX hash админу или сюда для активации.", reply_markup=REPLY_MARKUP)
    elif text == "\ud83d\udcb5 Тарифы /prices":
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("\ud83d\udcb3 Оплатить TON", callback_data="show_wallet")]])
        text = (
            "\ud83d\udcb0 Тарифы на подписку:\n\n"
            "\u2022 1 месяц — $25\n"
            "\u2022 3 месяца — $60 (экономия 15$)\n"
            "\u2022 6 месяцев — $100 (экономия 50$)\n"
            "\u2022 12 месяцев — $180 (экономия 120$)\n"
            "\u2022 Пожизненно — $299\n\n"
        )
        await update.message.reply_text(text, reply_markup=keyboard)

async def post_init(app):
    await app.bot.set_my_commands([BotCommand("start", "Запуск бота")])

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^\ud83d\udcca Помощь профессионала$"), help_pro)],
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



