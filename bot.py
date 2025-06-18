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

reply_keyboard = [["📊 Помощь профессионала"], ["📉 Прогноз по BTC", "📉 Прогноз по ETH"], ["📊 Оценить альтсезон"], ["📢 Опубликовать пост"], ["🎁 Тестовый период", "💰 Оплатить помощника"], ["💵 Тарифы /prices"], ["🔁 Перезапустить бота"]]
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
        await update.message.reply_text("Хорошо. Для точной консультации ответь на несколько вопросов.\n\n1. Твой стиль торговли? (скальпинг, позиционка или инвестици)")
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
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": prompt}]
    )
    await update.message.reply_text(f"📊 GPT:\n{response.choices[0].message.content.strip()}", reply_markup=REPLY_MARKUP)
    return ConversationHandler.END

async def general_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()
    style = context.user_data.get("style", "трейдинг")
    tf = context.user_data.get("timeframe", "любом")
    market = context.user_data.get("market", "общий")
    prompt = f"Пользователь торгует: {style}. Таймфрейм: {tf}. Рынок: {market}.\nВопрос: {user_text}\n\nОтветь как опытный профессиональный трейдер, адаптируя советы под стиль, рынок и уровень подготовки."
    response = await client.chat.completions.create(
        model="gpt-3.5-turbo",
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

    if text == "📉 Прогноз по BTC":
        context.user_data["price_asset"] = "BTC"
        await update.message.reply_text("Введите текущую цену BTC:")
    elif text == "📉 Прогноз по ETH":
        context.user_data["price_asset"] = "ETH"
        await update.message.reply_text("Введите текущую цену ETH:")
    elif text == "📊 Оценить альтсезон":
        if not await check_access(update): return
        data = requests.get("https://api.coingecko.com/api/v3/global").json()
        btc_d = round(data["data"]["market_cap_percentage"]["btc"], 2)
        eth_d = round(data["data"]["market_cap_percentage"]["eth"], 2)
        eth_btc = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=btc").json()["ethereum"]["btc"]
        prompt = f"BTC Dominance: {btc_d}%\nETH Dominance: {eth_d}%\nETH/BTC: {eth_btc}\nОцени вероятность альтсезона."
        response = await client.chat.completions.create(model="gpt-3.5-turbo", messages=[{"role": "user", "content": prompt}])
        await update.message.reply_text(response.choices[0].message.content.strip(), reply_markup=REPLY_MARKUP)
    elif text == "📢 Опубликовать пост":
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔥 Перейти к боту", url="https://t.me/Parser_newbot")]])
        post = (
            "🧠 Ты тоже сливал депозиты на роботе, сигналах и ручной торговле?\n"
            "Я был там. Тратил деньги на иллюзии, торговал без знаний и риска — и всё терял.\n\n"
            "Сегодня я создаю сообщество тех, кто хочет **жить с рынка**:\n"
            "📈 стабильно инвестировать\n"
            "💼 грамотно управлять капиталом\n"
            "🧭 получать поддержку и реальные инструменты, а не 'волшебную кнопку бабло'.\n\n"
            "Я не обещаю чудо. Но я даю всё, что помогает **зарабатывать стабильно**:\n\n"
            "✅ Обучение\n"
            "✅ GPT-помощник трейдера\n"
            "✅ Поддержка в любой момент\n"
            "✅ Торговые идеи\n"
            "✅ Интерпретации новостей\n"
            "✅ 📚 База знаний — бесплатно для подписчиков\n"
            "✅ 🗓 Еженедельные обзоры\n"
            "✅ 🤝 Сильное сообщество единомышленников\n"
            "✅ 📟 Бесплатный калькулятор для расчёта рисков\n\n"
            "Всё это — уже входит в подписку.\n"
            "Готов перейти от хаоса к системе?\n\n"
            "👇 Жми, и присоединяйся.\n"
            "🧩 Жить с рынка — реально."
        )
        await update.message.reply_text(post, reply_markup=keyboard)
    elif text == "🎁 Тестовый период":
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
            "Для активации отправь TX hash после оплаты."
        )
        await update.message.reply_text(text, reply_markup=keyboard)

async def post_init(app):
    await app.bot.set_my_commands([BotCommand("start", "Перезапустить бота")])

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
    app.run_polling()

if __name__ == '__main__':
    main()


