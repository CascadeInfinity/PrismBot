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
DATABASE_URL = os.getenv("DATABASE_URL") # Provided by Render PostgreSQL
PORT = int(os.getenv("PORT", 8080)) # Provided by Render

if not BOT_TOKEN or not BTC_ADDRESS or not DATABASE_URL:
    logging.error("Missing environment variables: BOT_TOKEN, BTC_ADDRESS, or DATABASE_URL.")
    exit(1)

SUBSCRIPTION_PRICE_USD = 9.99
BLOCKSTREAM_API = "https://blockstream.info/api"
COINGECKO_API = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"

# Conversation states
AWAITING_TXID = 1

# Logging
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- WEB SERVER (KEEP-ALIVE) ---
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Prism Bot is alive!", 200

def run_flask():
    app.run(host='0.0.0.0', port=PORT)

# --- DATABASE SETUP (PostgreSQL) ---
def get_db_connection():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users 
                 (telegram_id BIGINT PRIMARY KEY, username TEXT, expiry_date TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS orders 
                 (id SERIAL PRIMARY KEY, user_id BIGINT, amount_btc NUMERIC, txid TEXT UNIQUE, status TEXT, created_at TIMESTAMP)""")
    conn.commit()
    c.close()
    conn.close()

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
    
    if expiry:
        new_expiry = max(expiry, now) + timedelta(days=30)
    else:
        new_expiry = now + timedelta(days=30)
    
    c.execute("INSERT INTO users (telegram_id, username, expiry_date) VALUES (%s, %s, %s) "
              "ON CONFLICT (telegram_id) DO UPDATE SET username=EXCLUDED.username, expiry_date=EXCLUDED.expiry_date",
              (telegram_id, username, new_expiry))
    conn.commit()
    c.close()
    conn.close()
    return new_expiry

# --- BTC HELPERS ---
def get_btc_price():
    try:
        response = requests.get(COINGECKO_API).json()
        return response["bitcoin"]["usd"]
    except Exception as e:
        logger.error(f"Error fetching BTC price: {e}")
        return None

def verify_btc_tx(txid, expected_address, expected_amount_btc):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT id FROM orders WHERE txid=%s", (txid,))
        if c.fetchone():
            c.close()
            conn.close()
            return False, "This Transaction ID has already been used."
        c.close()
        conn.close()

        resp = requests.get(f"{BLOCKSTREAM_API}/tx/{txid}")
        if resp.status_code != 200:
            return False, "Transaction ID not found on the blockchain."
        
        tx_data = resp.json()
        found_output = False
        actual_amount_sat = 0
        for output in tx_data.get("vout", []):
            if output.get("scriptpubkey_address") == expected_address:
                actual_amount_sat += output.get("value", 0)
                found_output = True
        
        if not found_output:
            return False, "Transaction does not send funds to the correct address."
        
        actual_amount_btc = actual_amount_sat / 100_000_000
        if actual_amount_btc < expected_amount_btc * 0.99:
            return False, f"Insufficient amount. Expected ~{expected_amount_btc:.8f} BTC."
        
        return True, "Success"
    except Exception as e:
        logger.error(f"Error verifying TX {txid}: {e}")
        return False, "An error occurred during verification."

# --- BOT HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    expiry = get_user_expiry(user.id)
    
    msg = f"Welcome to **Prism**, {user.first_name}!\n\n"
    msg += f"Price: **${SUBSCRIPTION_PRICE_USD} / month**\n\n"
    
    if expiry:
        if expiry > datetime.now():
            msg += f"✅ Active until: `{expiry.strftime('%Y-%m-%d %H:%M')}`"
        else:
            msg += "❌ Expired."
    else:
        msg += "No active subscription."

    keyboard = [
        [InlineKeyboardButton("💎 Buy / Renew Subscription", callback_query_data='buy')],
        [InlineKeyboardButton("📊 My Status", callback_query_data='status')]
    ]
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == 'buy':
        btc_price = get_btc_price()
        if not btc_price:
            await query.edit_message_text("Error fetching BTC price. Try again later.")
            return
        
        btc_amount = SUBSCRIPTION_PRICE_USD / btc_price
        context.user_data['expected_btc'] = btc_amount
        
        msg = "💎 **Purchase Prism Subscription**\n\n"
        msg += f"Send exactly: `{btc_amount:.8f}` BTC\n"
        msg += f"To: `{BTC_ADDRESS}`\n\n"
        msg += "Submit your TXID after payment."
        
        keyboard = [[InlineKeyboardButton("✅ I've Sent the Payment", callback_query_data='submit_txid')]]
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        
    elif query.data == 'status':
        user = update.effective_user
        expiry = get_user_expiry(user.id)
        if expiry:
            await query.edit_message_text(f"Valid until: `{expiry}`", parse_mode='Markdown')
        else:
            await query.edit_message_text("No active subscription.")

async def ask_for_txid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Please paste your **Transaction ID (TXID)**:")
    return AWAITING_TXID

async def handle_txid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txid = update.message.text.strip()
    expected_btc = context.user_data.get('expected_btc')
    
    if not expected_btc:
        await update.message.reply_text("Session expired. Start over with /start.")
        return ConversationHandler.END

    await update.message.reply_text("🔍 Verifying...")
    success, message = verify_btc_tx(txid, BTC_ADDRESS, expected_btc)
    
    if success:
        user = update.effective_user
        new_expiry = update_user_subscription(user.id, user.username)
        
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("INSERT INTO orders (user_id, amount_btc, txid, status, created_at) VALUES (%s, %s, %s, %s, %s)",
                  (user.id, expected_btc, txid, 'confirmed', datetime.now()))
        conn.commit()
        c.close()
        conn.close()
        
        await update.message.reply_text(f"✅ Verified! New Expiry: `{new_expiry.strftime('%Y-%m-%d %H:%M')}`", parse_mode='Markdown')
    else:
        await update.message.reply_text(f"❌ Failed: {message}", parse_mode='Markdown')
    
    return ConversationHandler.END

def main():
    init_db()
    # Start web server in a separate thread
    threading.Thread(target=run_flask, daemon=True).start()
    
    application = Application.builder().token(BOT_TOKEN).build()
    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(ask_for_txid, pattern='^submit_txid$')],
        states={AWAITING_TXID: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_txid)]},
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler, pattern='^(buy|status)$'))
    application.add_handler(conv_handler)
    
    logger.info("Prism Bot is running...")
    application.run_polling()

if __name__ == '__main__':
    main()
