import logging
import os
import time

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

AWAY_TIMEOUT = 900

last_owner_activity = 0
BOT_ENABLED = True

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
USER_ID_ENV = os.getenv("YOUR_USER_ID")

if BOT_TOKEN is None:
    raise ValueError("BOT_TOKEN is missing.")

if USER_ID_ENV is None:
    raise ValueError("YOUR_USER_ID is missing.")

YOUR_USER_ID = int(USER_ID_ENV)


class SafeFormatter(logging.Formatter):
    def format(self, record):
        message = super().format(record)

        if BOT_TOKEN:
            message = message.replace(BOT_TOKEN, "********")

        return message


handler = logging.StreamHandler()
handler.setFormatter(
    SafeFormatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
)

logging.getLogger().handlers.clear()
logging.getLogger().addHandler(handler)
logging.getLogger().setLevel(logging.INFO)

logger = logging.getLogger(__name__)

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


def format_sender_info(user) -> str:
    name = user.full_name or "Unknown"
    username = f"@{user.username}" if user.username else "no username"
    return (
        f"📩 New Message\n\n"
        f"From: {name} ({username})\n"
        f"ID: {user.id}"
    )


async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    text = (
        "🤖 *Welcome!*\n\n"
        "I am an AI assistant responding on behalf of Agni Roybar.\n\n"
        "Leave a message here and I will make sure it reaches them directly."
    )

    await update.message.reply_text(  # type: ignore
        text,
        parse_mode=ParseMode.MARKDOWN,
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if update.effective_user and is_owner(update.effective_user.id):
        text = (
            "*Available Commands*\n\n"
            "/start\n"
            "/help\n"
            "/status\n"
            "/toggle\n"
            "/stop\n"
            "/resume\n"
            "/faq\n"
            "/contact"
        )
    else:
        text = (
            "*Available Commands*\n\n"
            "/start\n"
            "/help\n"
            "/status\n"
            "/faq\n"
            "/contact"
        )

    await update.message.reply_text(  # type: ignore
        text,
        parse_mode=ParseMode.MARKDOWN,
    )


async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if is_available():
        status = "🟢 Agni Roybar is available."
    else:
        status = "🔴 Agni Roybar is currently busy."

    await update.message.reply_text(status)  # type: ignore


async def toggle_status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    global last_owner_activity

    if not update.effective_user or not is_owner(update.effective_user.id):
        return

    if is_available():
        last_owner_activity = 0
        await update.message.reply_text(  # type: ignore
            "🔴 Status manually set to *Busy*.",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        last_owner_activity = time.time()
        await update.message.reply_text(  # type: ignore
            "🟢 Status manually set to *Available*.",
            parse_mode=ParseMode.MARKDOWN,
        )


async def faq_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    text = (
        "*Frequently Asked Questions*\n\n"
        "📌 Response time: Usually a few hours.\n\n"
        "📌 Messages are forwarded directly."
    )

    await update.message.reply_text(  # type: ignore
        text,
        parse_mode=ParseMode.MARKDOWN,
    )


async def contact_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    text = (
        "*Alternative contacts*\n\n"
        "📧 Email: agniroybar@gmail\\.com\n"
        "💼 LinkedIn:\n"
        "linkedin\\.com/in/agni\\-roybar\\-6600983b0"
    )

    await update.message.reply_text(  # type: ignore
        text,
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
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

    # try:
    #     info_message = await context.bot.send_message(
    #         chat_id=YOUR_USER_ID,
    #         text=format_sender_info(sender),
    #     )

    #     forwarded_message = await context.bot.copy_message(
    #         chat_id=YOUR_USER_ID,
    #         from_chat_id=message.chat_id,
    #         message_id=message.message_id,
    #     )

    # except Exception as e:
    #     logger.exception(f"Message forwarding error: {e}")

    #     await message.reply_text(
    #         "👋 Thanks for reaching out. This is a bot \n\n"
    #         "I'm currently unavailable, but your message has been noted. "
    #         "I'll get back to you as soon as possible."
    #     )

    #     return
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
    except Exception as e:
        import traceback

        print(traceback.format_exc())

        await message.reply_text(
            f"Internal server error:\n{e}"
        )

        return

    message_map[forwarded_message.message_id] = sender_id

    auto_reply = AUTO_REPLY_AVAILABLE if is_available() else AUTO_REPLY_BUSY

    await message.reply_text(
        auto_reply,
        parse_mode=ParseMode.MARKDOWN,
    )

async def stop_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    global BOT_ENABLED

    if not update.effective_user:
        return

    if not is_owner(update.effective_user.id):
        return

    BOT_ENABLED = False

    await update.message.reply_text( # type: ignore
        "🛑 Assistant mode disabled."
    )

async def resume_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    global BOT_ENABLED

    if not update.effective_user:
        return

    if not is_owner(update.effective_user.id):
        return

    BOT_ENABLED = True

    await update.message.reply_text( # type: ignore
        "✅ Assistant mode enabled."
    )

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    logger.exception("Unexpected error occurred.")


def main():

    request = HTTPXRequest(
        connect_timeout=30,
        read_timeout=30,
    )

    app = (
        Application.builder()
        .token(BOT_TOKEN)  # type: ignore
        .request(request)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("toggle", toggle_status_command))
    app.add_handler(CommandHandler("faq", faq_command))
    app.add_handler(CommandHandler("contact", contact_command))
    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(CommandHandler("resume", resume_command))

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message,
        )
    )

    app.add_error_handler(error_handler)

    print("Bot is running...")

    app.run_polling()


if __name__ == "__main__":
    main()