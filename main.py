import asyncio
import logging
import os
import time
from flask import Flask, request
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

AWAY_TIMEOUT = 900
last_owner_activity = 0
BOT_ENABLED = True

BOT_TOKEN = os.getenv("BOT_TOKEN")
USER_ID_ENV = os.getenv("YOUR_USER_ID")

if not BOT_TOKEN or not USER_ID_ENV:
    raise ValueError("BOT_TOKEN or YOUR_USER_ID missing from environment variables.")

YOUR_USER_ID = int(USER_ID_ENV)

# Logging configuration
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Global message tracking map
message_map = {}

AUTO_REPLY_AVAILABLE = (
    "✅ *Message Received*\n\n"
    "Thank you for reaching out. Agni Roybar has been notified and is currently "
    "available, so your message will be reviewed shortly.\n\n"
    "You'll receive a personal reply here directly."
)

AUTO_REPLY_BUSY = (
    "📥 *Message Received*\n\n"
    "Thank you for your message. Agni Roybar is currently unavailable, but your "
    "message has been securely delivered and will be reviewed as soon as possible.\n\n"
    "You'll receive a personal reply here directly."
)

def is_owner(user_id: int) -> bool:
    return user_id == YOUR_USER_ID

def is_available() -> bool:
    return (time.time() - last_owner_activity) < AWAY_TIMEOUT

# --- HANDLER FUNCTIONS ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 *Welcome!*\n\n"
        "I am an AI assistant responding on behalf of Agni Roybar.\n\n"
        "Leave a message here and I will make sure it reaches them directly."
    )
    if update.message:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and is_owner(update.effective_user.id):
        text = (
            "*Available Commands*\n\n"
            "/start\n/help\n/status\n/toggle\n/stop\n/resume\n/faq\n/contact"
        )
    else:
        text = (
            "*Available Commands*\n\n"
            "/start\n/help\n/status\n/faq\n/contact"
        )
    if update.message:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = "🟢 Agni Roybar is available." if is_available() else "🔴 Agni Roybar is currently busy."
    if update.message:
        await update.message.reply_text(status)

async def toggle_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_owner_activity
    if not update.effective_user or not is_owner(update.effective_user.id):
        return

    if is_available():
        last_owner_activity = 0
        text = "🔴 Status manually set to *Busy*."
    else:
        last_owner_activity = time.time()
        text = "🟢 Status manually set to *Available*."

    if update.message:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def faq_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "*Frequently Asked Questions*\n\n"
        "📌 Response time: Usually a few hours.\n\n"
        "📌 Messages are forwarded directly."
    )
    if update.message:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def contact_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "*Alternative contacts*\n\n"
        "📧 Email: agniroybar@gmail\\.com\n"
        "💼 LinkedIn:\n"
        "linkedin\\.com/in/agni\\-roybar\\-6600983b0"
    )
    if update.message:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_ENABLED
    if update.effective_user and is_owner(update.effective_user.id):
        BOT_ENABLED = False
        if update.message:
            await update.message.reply_text("🛑 Assistant mode disabled.")

async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_ENABLED
    if update.effective_user and is_owner(update.effective_user.id):
        BOT_ENABLED = True
        if update.message:
            await update.message.reply_text("✅ Assistant mode enabled.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_owner_activity
    if update.effective_message is None or update.effective_user is None:
        return

    message = update.effective_message
    sender = update.effective_user
    sender_id = sender.id

    if not BOT_ENABLED and not is_owner(sender_id):
        return

    if is_owner(sender_id):
        last_owner_activity = time.time()
        if message.reply_to_message:
            original_message_id = message.reply_to_message.message_id
            target_user_id = message_map.get(original_message_id)

            if target_user_id:
                await context.bot.copy_message(
                    chat_id=target_user_id,
                    from_chat_id=message.chat_id,
                    message_id=message.message_id,
                )
                await message.reply_text("✅ Reply delivered.")
            else:
                await message.reply_text("❌ User information not found.")
        return

    try:
        sender_name = sender.full_name or "Unknown"
        sender_username = f"@{sender.username}" if sender.username else "No username"
        sender_text = message.text or "[Empty message]"

        complete_message = (
            f"📩 New message\n\n"
            f"👤 Name: {sender_name}\n"
            f"🔗 Username: {sender_username}\n"
            f"🆔 ID: {sender.id}\n\n"
            f"💬 Message:\n"
            f"{sender_text}"
        )

        forwarded_message = await context.bot.send_message(
            chat_id=YOUR_USER_ID,
            text=complete_message,
        )
        message_map[forwarded_message.message_id] = sender_id
    except Exception as e:
        logger.exception(f"Message forwarding error: {e}")
        await message.reply_text(
            "👋 Thanks for reaching out. I'm currently unavailable, but your message has been noted."
        )
        return

    auto_reply = AUTO_REPLY_AVAILABLE if is_available() else AUTO_REPLY_BUSY
    await message.reply_text(auto_reply, parse_mode=ParseMode.MARKDOWN)

# --- BOT BUILDER ---
telegram_app = Application.builder().token(BOT_TOKEN).build()
telegram_app.add_handler(CommandHandler("start", start_command))
telegram_app.add_handler(CommandHandler("help", help_command))
telegram_app.add_handler(CommandHandler("status", status_command))
telegram_app.add_handler(CommandHandler("toggle", toggle_status_command))
telegram_app.add_handler(CommandHandler("faq", faq_command))
telegram_app.add_handler(CommandHandler("contact", contact_command))
telegram_app.add_handler(CommandHandler("stop", stop_command))
telegram_app.add_handler(CommandHandler("resume", resume_command))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# --- VERCEL FLASK APP SERVER ---
app = Flask(__name__)

async def process_telegram_update(update_json):
    async with telegram_app:
        await telegram_app.initialize()
        update = Update.de_json(update_json, telegram_app.bot)
        await telegram_app.process_update(update)

@app.route('/', methods=['POST', 'GET'])
def webhook():
    if request.method == 'POST':
        update_data = request.get_json(force=True)
        asyncio.run(process_telegram_update(update_data))
        return "OK", 200
    return "Bot running on Vercel Serverless!", 200