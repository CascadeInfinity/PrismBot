import logging
import psycopg2
import requests
import asyncio
import os
import threading
from datetime import datetime, timedelta
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes, ConversationHandler

# --- CONFIGURATION ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
BTC_ADDRESS = os.getenv("BTC_ADDRESS")
DATABASE_URL = os.getenv("DATABASE_URL")
PORT = int(os.getenv("PORT", 8080))

# Logging
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

if not BOT_TOKEN or not BTC_ADDRESS or not DATABASE_URL:
    logger.error("CRITICAL: Missing environment variables!")
    exit(1)

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
    logger.info(f"Starting Flask server on port {PORT}...")
    app.run(host='0.0.0.0', port=PORT)

# --- DATABASE ---
def get_db_connection():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_db():
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS users 
                     (telegram_id BIGINT PRIMARY KEY, username TEXT, expiry_date TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS orders 
                     (id SERIAL PRIMARY KEY, user_id BIGINT, amount_btc NUMERIC, txid TEXT UNIQUE, status TEXT, created_at TIMESTAMP)""")
        conn.commit()
        c.close()
        conn.close()
        logger.info("Database initialized successfully.")
    except Exception as e:
        logger.error(f"Database init error: {e}")

def get_user_expiry(telegram_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT expiry_date FROM users WHERE telegram_id=%s", (telegram_id,))
    result = c.fetchone()
    c.close()
    conn.close()
    return result[0] if result else None

def update_user_subscription(telegram_id, username):
    conn = get_db_connection()
    c = conn.cursor()
    now = datetime.now()
    expiry = get_user_expiry(telegram_id)
    new_expiry = (max(expiry, now) if expiry else now) + timedelta(days=30)
    c.execute("INSERT INTO users (telegram_id, username, expiry_date) VALUES (%s, %s, %s) "
              "ON CONFLICT (telegram_id) DO UPDATE SET username=EXCLUDED.username, expiry_date=EXCLUDED.expiry_date",
              (telegram_id, username, new_expiry))
    conn.commit()
    c.close()
    conn.close()
    return new_expiry

# --- BTC ---
def get_btc_price():
    try:
        return requests.get(COINGECKO_API).json()["bitcoin"]["usd"]
    except: return None

def verify_btc_tx(txid, expected_address, expected_amount_btc):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT id FROM orders WHERE txid=%s", (txid,))
        if c.fetchone(): return False, "Already used."
        c.close()
        conn.close()

        resp = requests.get(f"{BLOCKSTREAM_API}/tx/{txid}")
        if resp.status_code != 200: return False, "Not found."
        
        tx_data = resp.json()
        actual_amount_sat = sum(o.get("value", 0) for o in tx_data.get("vout", []) if o.get("scriptpubkey_address") == expected_address)
        
        if actual_amount_sat == 0: return False, "Wrong address."
        if (actual_amount_sat / 100_000_000) < expected_amount_btc * 0.98: return False, "Insufficient amount."
        
        return True, "Success"
    except: return False, "Error."

# --- HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        price = get_btc_price()
        if not price: return await query.edit_message_text("Price error.")
        amt = SUBSCRIPTION_PRICE_USD / price
        context.user_data['expected_btc'] = amt
        msg = f"💎 **Purchase**\nSend: `{amt:.8f}` BTC\nTo: `{BTC_ADDRESS}`"
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ I Sent It", callback_query_data='submit_txid')]]), parse_mode='Markdown')
    elif query.data == 'status':
        expiry = get_user_expiry(update.effective_user.id)
        await query.edit_message_text(f"Status: `{expiry or 'None'}`", parse_mode='Markdown')

async def ask_for_txid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("Paste your **TXID**:")
    return AWAITING_TXID

async def handle_txid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txid = update.message.text.strip()
    amt = context.user_data.get('expected_btc')
    if not amt: return ConversationHandler.END
    await update.message.reply_text("🔍 Verifying...")
    success, msg = verify_btc_tx(txid, BTC_ADDRESS, amt)
    if success:
        new_expiry = update_user_subscription(update.effective_user.id, update.effective_user.username)
        conn = get_db_connection(); c = conn.cursor()
        c.execute("INSERT INTO orders (user_id, amount_btc, txid, status, created_at) VALUES (%s, %s, %s, %s, %s)", (update.effective_user.id, amt, txid, 'confirmed', datetime.now()))
        conn.commit(); c.close(); conn.close()
        await update.message.reply_text(f"✅ Success! New Expiry: `{new_expiry}`", parse_mode='Markdown')
    else:
        await update.message.reply_text(f"❌ {msg}")
    return ConversationHandler.END

def main():
    init_db()
    threading.Thread(target=run_flask, daemon=True).start()
    app_bot = Application.builder().token(BOT_TOKEN).build()
    app_bot.add_handler(CommandHandler("start", start))
    app_bot.add_handler(CallbackQueryHandler(button_handler, pattern='^(buy|status)$'))
    app_bot.add_handler(ConversationHandler(entry_points=[CallbackQueryHandler(ask_for_txid, pattern='^submit_txid$')], states={AWAITING_TXID: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_txid)]}, fallbacks=[]))
    logger.info("Bot is starting polling...")
    app_bot.run_polling()

if __name__ == '__main__':
    main()
