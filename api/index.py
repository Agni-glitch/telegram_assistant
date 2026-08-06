import asyncio
import json
import logging
import os
import time
import urllib.request
import urllib.error
import urllib.parse

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

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

AWAY_TIMEOUT = 900

BOT_TOKEN = os.getenv("BOT_TOKEN")
USER_ID_ENV = os.getenv("YOUR_USER_ID")

# Optional: Upstash Redis REST API, used to persist state across serverless
# invocations. If these are not set, the bot falls back to in-memory state
# (which will reset unpredictably between invocations on Vercel).
UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")

if not BOT_TOKEN or not USER_ID_ENV:
    raise ValueError("BOT_TOKEN or YOUR_USER_ID missing from environment variables.")

YOUR_USER_ID = int(USER_ID_ENV)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# In-memory fallback (used only if Upstash is not configured)
_mem_last_owner_activity = 0.0
_mem_bot_enabled = True
_mem_message_map = {}

REDIS_KEY_LAST_ACTIVITY = "bot:last_owner_activity"
REDIS_KEY_BOT_ENABLED = "bot:enabled"
REDIS_KEY_MESSAGE_MAP = "bot:message_map"  # stored as JSON blob: {msg_id: user_id}


# ---------------------------------------------------------------------------
# STATE STORE (Upstash Redis REST, with in-memory fallback)
# ---------------------------------------------------------------------------

def _redis_available() -> bool:
    return bool(UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN)


def _redis_request(path_segments):
    """
    Calls the Upstash Redis REST API. path_segments is a list of command
    parts, e.g. ["GET", "bot:enabled"] or ["SET", "bot:enabled", "1"].
    Returns the parsed 'result' field, or None on failure.
    """
    assert UPSTASH_REDIS_REST_URL is not None
    url = UPSTASH_REDIS_REST_URL.rstrip("/") + "/" + "/".join(
        urllib.parse.quote(str(p), safe="") for p in path_segments
    )
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("result")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        logger.warning(f"Redis request failed ({path_segments[0]}): {e}")
        return None


def get_last_owner_activity() -> float:
    if _redis_available():
        result = _redis_request(["GET", REDIS_KEY_LAST_ACTIVITY])
        return float(result) if result is not None else 0.0
    return _mem_last_owner_activity


def set_last_owner_activity(value: float):
    global _mem_last_owner_activity
    if _redis_available():
        _redis_request(["SET", REDIS_KEY_LAST_ACTIVITY, str(value)])
    else:
        _mem_last_owner_activity = value


def get_bot_enabled() -> bool:
    if _redis_available():
        result = _redis_request(["GET", REDIS_KEY_BOT_ENABLED])
        if result is None:
            return True  # default enabled
        return result == "1"
    return _mem_bot_enabled


def set_bot_enabled(value: bool):
    global _mem_bot_enabled
    if _redis_available():
        _redis_request(["SET", REDIS_KEY_BOT_ENABLED, "1" if value else "0"])
    else:
        _mem_bot_enabled = value


def get_message_map_entry(message_id: int):
    if _redis_available():
        result = _redis_request(["HGET", REDIS_KEY_MESSAGE_MAP, str(message_id)])
        return int(result) if result is not None else None
    return _mem_message_map.get(message_id)


def set_message_map_entry(message_id: int, user_id: int):
    if _redis_available():
        _redis_request(["HSET", REDIS_KEY_MESSAGE_MAP, str(message_id), str(user_id)])
    else:
        _mem_message_map[message_id] = user_id


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def is_owner(user_id: int) -> bool:
    return user_id == YOUR_USER_ID


def is_available() -> bool:
    return (time.time() - get_last_owner_activity()) < AWAY_TIMEOUT


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


# ---------------------------------------------------------------------------
# HANDLER FUNCTIONS
# ---------------------------------------------------------------------------

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
    if not update.effective_user or not is_owner(update.effective_user.id):
        return

    if is_available():
        set_last_owner_activity(0)
        text = "🔴 Status manually set to *Busy*."
    else:
        set_last_owner_activity(time.time())
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
    if update.effective_user and is_owner(update.effective_user.id):
        set_bot_enabled(False)
        if update.message:
            await update.message.reply_text("🛑 Assistant mode disabled.")


async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and is_owner(update.effective_user.id):
        set_bot_enabled(True)
        if update.message:
            await update.message.reply_text("✅ Assistant mode enabled.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_message is None or update.effective_user is None:
        return

    message = update.effective_message
    sender = update.effective_user
    sender_id = sender.id

    if not get_bot_enabled() and not is_owner(sender_id):
        return

    if is_owner(sender_id):
        set_last_owner_activity(time.time())
        if message.reply_to_message:
            original_message_id = message.reply_to_message.message_id
            target_user_id = get_message_map_entry(original_message_id)

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
        set_message_map_entry(forwarded_message.message_id, sender_id)
    except Exception as e:
        logger.exception(f"Message forwarding error: {e}")
        await message.reply_text(
            "👋 Thanks for reaching out. I'm currently unavailable, but your message has been noted."
        )
        return

    auto_reply = AUTO_REPLY_AVAILABLE if is_available() else AUTO_REPLY_BUSY
    await message.reply_text(auto_reply, parse_mode=ParseMode.MARKDOWN)


# ---------------------------------------------------------------------------
# BOT BUILDER
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# FLASK APP (Vercel entrypoint)
# ---------------------------------------------------------------------------

app = Flask(__name__)


async def process_telegram_update(update_json):
    async with telegram_app:
        update = Update.de_json(update_json, telegram_app.bot)
        await telegram_app.process_update(update)


@app.route('/', methods=['POST', 'GET'])
def webhook():
    if request.method == 'POST':
        update_data = request.get_json(force=True)
        asyncio.run(process_telegram_update(update_data))
        return "OK", 200
    return "Bot running on Vercel Serverless!", 200