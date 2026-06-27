import logging
import psycopg2
import requests
import asyncio
import os
import threading
import sys
from datetime import datetime, timedelta
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes, ConversationHandler

# --- CONFIGURATION ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
BTC_ADDRESS = os.getenv("BTC_ADDRESS")
DATABASE_URL = os.getenv("DATABASE_URL")
PORT = int(os.getenv("PORT", 8080))

# Logging to stdout for Render
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

logger.info("--- PRISM BOT STARTING UP ---")

if not BOT_TOKEN: logger.error("MISSING BOT_TOKEN")
if not BTC_ADDRESS: logger.error("MISSING BTC_ADDRESS")
if not DATABASE_URL: logger.error("MISSING DATABASE_URL")

if not all([BOT_TOKEN, BTC_ADDRESS, DATABASE_URL]):
    sys.exit(1)

SUBSCRIPTION_PRICE_USD = 9.99
BLOCKSTREAM_API = "https://blockstream.info/api"
COINGECKO_API = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"

AWAITING_TXID = 1

# --- WEB SERVER ---
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Prism Bot is alive and healthy!", 200

def run_flask():
    logger.info(f"Flask starting on port {PORT}")
    app.run(host='0.0.0.0', port=PORT)

# --- DATABASE ---
def get_db_connection():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_db():
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS users (telegram_id BIGINT PRIMARY KEY, username TEXT, expiry_date TIMESTAMP)")
        c.execute("CREATE TABLE IF NOT EXISTS orders (id SERIAL PRIMARY KEY, user_id BIGINT, amount_btc NUMERIC, txid TEXT UNIQUE, status TEXT, created_at TIMESTAMP)")
        conn.commit()
        c.close()
        conn.close()
        logger.info("DB Initialized")
    except Exception as e:
        logger.error(f"DB Error: {e}")

def get_user_expiry(tid):
    try:
        conn = get_db_connection(); c = conn.cursor()
        c.execute("SELECT expiry_date FROM users WHERE telegram_id=%s", (tid,))
        res = c.fetchone(); c.close(); conn.close()
        return res[0] if res else None
    except: return None

# --- HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Start command from {update.effective_user.id}")
    user = update.effective_user
    expiry = get_user_expiry(user.id)
    msg = f"Welcome to **Prism**, {user.first_name}!\nPrice: **${SUBSCRIPTION_PRICE_USD}/mo**\n\n"
    msg += f"✅ Active until: `{expiry}`" if expiry and expiry > datetime.now() else "❌ No active subscription."
    keyboard = [[InlineKeyboardButton("💎 Buy / Renew", callback_query_data='buy')], [InlineKeyboardButton("📊 Status", callback_query_data='status')]]
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == 'buy':
        try:
            price = requests.get(COINGECKO_API).json()["bitcoin"]["usd"]
            amt = SUBSCRIPTION_PRICE_USD / price
            context.user_data['expected_btc'] = amt
            msg = f"💎 **Purchase**\nSend: `{amt:.8f}` BTC\nTo: `{BTC_ADDRESS}`"
            await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ I Sent It", callback_query_data='submit_txid')]]), parse_mode='Markdown')
        except: await query.edit_message_text("Price error.")

async def main_async():
    init_db()
    # Start Flask in thread
    threading.Thread(target=run_flask, daemon=True).start()
    
    logger.info("Building Telegram Application...")
    application = Application.builder().token(BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler, pattern='^(buy|status)$'))
    
    logger.info("Starting Polling...")
    async with application:
        await application.initialize()
        await application.start()
        await application.updater.start_polling()
        logger.info("POLLING STARTED SUCCESSFULLY")
        # Keep alive
        while True:
            await asyncio.sleep(3600)

if __name__ == '__main__':
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.error(f"Fatal error: {e}")
