"""
Telegram assistant bot for Vercel (stateless, no database).

- Other people message the bot -> it replies on your behalf (AI model, or a
  hard-coded message) and notifies you: who, when, what, and what it replied.
- You reply (Telegram "reply") to a notification -> your message goes to them.
- You message the bot directly -> you chat with the AI model.

State without a database:
- Reply routing: the sender's ID is written into each notification, so a reply
  to a notification tells the bot who to send it to.
- Assistant mode (ai / away / available): stored as a pinned message in your
  chat with the bot, read back with getChat.
"""

import asyncio
import hmac
import logging
import os
import re
import sys
from datetime import datetime, timezone

from flask import Flask, request
from telegram import BotCommand, Message, Update, User
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import llm  # noqa: E402

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN")
USER_ID_ENV = os.getenv("YOUR_USER_ID")
if not BOT_TOKEN or not USER_ID_ENV:
    raise ValueError("BOT_TOKEN or YOUR_USER_ID missing from environment variables.")
YOUR_USER_ID = int(USER_ID_ENV)

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
OWNER_NAME = os.getenv("OWNER_NAME", "Agni Roybar")
OWNER_ABOUT = os.getenv("OWNER_ABOUT", "")  # facts the AI may share with others
TIMEZONE = os.getenv("TIMEZONE", "UTC")

# Default models (from Vercel). The owner can override them in chat with /model.
AI_MODELS = llm.parse_models(os.getenv("AI_MODELS"))
OWNER_AI_MODELS = llm.parse_models(os.getenv("OWNER_AI_MODELS"))

MODES = {
    "ai": "AI replies to people on your behalf (hard-coded message if the AI fails).",
    "away": "Hard-coded 'unavailable' message, no AI.",
    "available": "Short acknowledgement only; you reply yourself.",
}
DEFAULT_MODE = os.getenv("DEFAULT_MODE", "ai").strip().lower()
if DEFAULT_MODE not in MODES:
    DEFAULT_MODE = "ai"

AWAY_MESSAGE = os.getenv("AWAY_MESSAGE") or (
    f"📥 Message received\n\n"
    f"Thanks for your message. {OWNER_NAME} is currently unavailable, but your message "
    f"has been delivered and will be reviewed as soon as possible.\n\n"
    f"You'll get a personal reply here."
)
AVAILABLE_MESSAGE = os.getenv("AVAILABLE_MESSAGE") or (
    f"✅ Message received\n\n{OWNER_NAME} has been notified and will reply to you here shortly."
)
FAQ_TEXT = os.getenv("FAQ_TEXT") or (
    "Frequently asked questions\n\n"
    "📌 Response time: usually a few hours.\n"
    "📌 Messages are delivered directly."
)
CONTACT_TEXT = os.getenv("CONTACT_TEXT") or (
    "Alternative contacts\n\n"
    "📧 Email: agniroybar@gmail.com\n"
    "💼 LinkedIn: linkedin.com/in/agni-roybar-6600983b0"
)

TG_LIMIT = 4000
ID_RE = re.compile(r"🆔 ID: (\d+)")
MODE_RE = re.compile(r"Assistant mode: (\w+)")
OTHERS_MODELS_RE = re.compile(r"Models for others: (.+)")
OWNER_MODELS_RE = re.compile(r"Models for you: (.+)")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def is_owner(user: User | None) -> bool:
    return bool(user) and user.id == YOUR_USER_ID


def clip(text: str, limit: int = TG_LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def fmt_time(dt: datetime | None) -> str:
    dt = dt or datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        dt = dt.astimezone(ZoneInfo(TIMEZONE))
    except Exception:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M %Z")


def media_kind(msg: Message) -> str | None:
    for kind in ("photo", "video", "voice", "audio", "document", "sticker",
                 "animation", "video_note", "location", "contact", "poll"):
        if getattr(msg, kind, None):
            return kind.replace("_", " ")
    return None


def describe(msg: Message) -> str:
    text = msg.text or msg.caption or ""
    kind = media_kind(msg)
    if kind:
        return f"[{kind}] {text}".strip()
    return text or "[empty message]"


def with_reply_context(msg: Message, text: str, who: str) -> str:
    """Stateless 'memory': include the message being replied to, if any."""
    quoted = msg.reply_to_message
    if quoted and (quoted.text or quoted.caption):
        return f"(Replying to {who} earlier message: \"{clip(quoted.text or quoted.caption, 1500)}\")\n\n{text}"
    return text


async def ai_generate(models, system: str, user_text: str) -> tuple[str, str]:
    return await asyncio.to_thread(llm.generate, models, system, user_text)


# ---------------------------------------------------------------------------
# SETTINGS (mode + model overrides, stored as one pinned message in the
# owner's chat; Telegram's getChat only returns the latest pinned message)
# ---------------------------------------------------------------------------

async def get_settings(bot) -> dict:
    s = {"mode": DEFAULT_MODE, "others": None, "me": None, "msg_id": None}
    try:
        chat = await bot.get_chat(YOUR_USER_ID)
        pinned = chat.pinned_message
        text = (pinned.text or "") if pinned else ""
        m = MODE_RE.search(text)
        if not (m and m.group(1) in MODES):
            return s
        s["mode"], s["msg_id"] = m.group(1), pinned.message_id
        for key, regex in (("others", OTHERS_MODELS_RE), ("me", OWNER_MODELS_RE)):
            found = regex.search(text)
            if found and found.group(1).strip() != "default" and llm.parse_models(found.group(1)):
                s[key] = found.group(1).strip()
    except TelegramError as e:
        logger.warning(f"Could not read settings: {e}")
    return s


def others_models(s: dict) -> list[tuple[str, str]]:
    return llm.parse_models(s["others"]) or AI_MODELS


def owner_models(s: dict) -> list[tuple[str, str]]:
    return llm.parse_models(s["me"]) or OWNER_AI_MODELS or others_models(s)


def fmt_models(models) -> str:
    return ", ".join(f"{p}:{m}" for p, m in models) or "none"


async def save_settings(bot, s: dict):
    text = (
        f"📌 Assistant settings (pinned so the bot remembers them, please don't unpin)\n\n"
        f"Assistant mode: {s['mode']}\n{MODES[s['mode']]}\n\n"
        f"Models for others: {s['others'] or 'default'}\n"
        f"Models for you: {s['me'] or 'default'}\n\n"
        f"In use for others: {fmt_models(others_models(s))}\n"
        f"In use for you: {fmt_models(owner_models(s))}"
    )
    msg = await bot.send_message(YOUR_USER_ID, text)
    if s["msg_id"]:
        try:
            await bot.unpin_chat_message(YOUR_USER_ID, message_id=s["msg_id"])
        except TelegramError:
            pass
    await bot.pin_chat_message(YOUR_USER_ID, msg.message_id, disable_notification=True)


async def set_mode(bot, mode: str):
    s = await get_settings(bot)
    s["mode"] = mode
    await save_settings(bot, s)


# ---------------------------------------------------------------------------
# PROMPTS
# ---------------------------------------------------------------------------

def others_system_prompt(sender: User) -> str:
    about = f"\nAbout {OWNER_NAME} (you may share this):\n{OWNER_ABOUT}\n" if OWNER_ABOUT else ""
    return (
        f"You are the personal AI assistant of {OWNER_NAME}, replying on their behalf in Telegram "
        f"while they are not available. You are talking to {sender.full_name}. "
        f"Current time: {fmt_time(None)}.\n{about}\n"
        f"Rules:\n"
        f"- Be friendly, natural and brief (1-4 sentences). Reply in the sender's language.\n"
        f"- Make clear you're {OWNER_NAME}'s assistant, and that {OWNER_NAME} will see the message and can reply personally.\n"
        f"- Answer simple questions using only the information above. Never invent facts, promises, "
        f"meetings, prices, availability or personal details about {OWNER_NAME}.\n"
        f"- Don't share private information and don't agree to anything on {OWNER_NAME}'s behalf.\n"
        f"- Plain text only, no markdown."
    )


def owner_system_prompt() -> str:
    about = f"\nAbout {OWNER_NAME}:\n{OWNER_ABOUT}\n" if OWNER_ABOUT else ""
    return (
        f"You are the personal assistant of {OWNER_NAME}, chatting with {OWNER_NAME} directly in Telegram. "
        f"Current time: {fmt_time(None)}.\n{about}\n"
        f"Help with anything: answering questions, drafting replies to people, planning, thinking things through. "
        f"You have no memory of earlier chats; if they reply to a message, it's included for context. "
        f"Be concise. Plain text, no markdown tables."
    )


# ---------------------------------------------------------------------------
# COMMANDS
# ---------------------------------------------------------------------------

OWNER_HELP = (
    "Owner commands\n\n"
    "/ai - AI replies to people for you\n"
    "/away - hard-coded 'unavailable' reply\n"
    "/available - short acknowledgement, you reply yourself\n"
    "/status - current mode and models\n"
    "/model <provider:model,...> - set models (both), or /model others ... / /model me ...\n"
    "/model reset - back to the models set in Vercel\n"
    "/aireply <instructions> - reply to a notification with this to let the AI answer that person\n\n"
    "Reply to a notification to answer that person directly.\n"
    "Any other message: chat with the AI."
)
PUBLIC_HELP = "Commands\n\n/start\n/help\n/status\n/faq\n/contact\n\nOr just write your message."


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_owner(update.effective_user):
        await update.message.reply_text(f"👋 Hi {OWNER_NAME}, I'm your assistant.\n\n{OWNER_HELP}")
        return
    await update.message.reply_text(
        f"🤖 Welcome!\n\nI'm the assistant of {OWNER_NAME}. Leave a message here and "
        f"I'll make sure it reaches them."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(OWNER_HELP if is_owner(update.effective_user) else PUBLIC_HELP)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = await get_settings(context.bot)
    mode = s["mode"]
    if is_owner(update.effective_user):
        await update.message.reply_text(
            f"Mode: {mode}\n{MODES[mode]}\n\n"
            f"Models replying to others: {fmt_models(others_models(s))}\n"
            f"Models for your chat: {fmt_models(owner_models(s))}"
        )
    else:
        state = "available 🟢" if mode == "available" else "currently away 🔴"
        await update.message.reply_text(f"{OWNER_NAME} is {state}.")


def mode_command(mode: str):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_owner(update.effective_user):
            return
        await set_mode(context.bot, mode)
    return handler


async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner only: /model, /model <spec>, /model others <spec>, /model me <spec>, /model reset."""
    if not is_owner(update.effective_user):
        return
    msg = update.message
    args = list(context.args)
    s = await get_settings(context.bot)

    if not args:
        await msg.reply_text(
            f"Models for others: {fmt_models(others_models(s))}{'' if s['others'] else ' (default)'}\n"
            f"Models for you: {fmt_models(owner_models(s))}{'' if s['me'] else ' (default)'}\n\n"
            f"Change: /model provider:model[,provider:model...]\n"
            f"Only one side: /model others ... or /model me ...\n"
            f"Undo: /model reset\n\n"
            f"Providers: {', '.join(llm.PROVIDERS)} (or any with <NAME>_BASE_URL set)"
        )
        return

    target = "both"
    if args[0].lower() in ("others", "me"):
        target = args.pop(0).lower()
    spec = "".join(args)  # allow "a:b, c:d" with spaces

    if spec.lower() in ("reset", "default"):
        if target in ("both", "others"):
            s["others"] = None
        if target in ("both", "me"):
            s["me"] = None
        await save_settings(context.bot, s)
        await msg.reply_text("✅ Models reset to the defaults from Vercel.")
        return

    models = llm.parse_models(spec)
    if not models:
        await msg.reply_text("Format: /model provider:model, e.g. /model groq:llama-3.3-70b-versatile")
        return
    problems = [p for p in (llm.check_provider(prov) for prov, _ in models) if p]
    if problems:
        await msg.reply_text("❌ Not changed:\n" + "\n".join(problems))
        return

    normalized = fmt_models(models)
    if target in ("both", "others"):
        s["others"] = normalized
    if target in ("both", "me"):
        s["me"] = normalized
    await save_settings(context.bot, s)
    await msg.reply_text(f"✅ Models for {'others and you' if target == 'both' else target}: {normalized}")


async def faq_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(FAQ_TEXT)


async def contact_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(CONTACT_TEXT)


async def aireply_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner replies to a notification with /aireply <instructions>."""
    msg = update.message
    if not is_owner(update.effective_user):
        return
    quoted = msg.reply_to_message
    note = (quoted.text or quoted.caption or "") if quoted else ""
    m = ID_RE.search(note)
    if not m:
        await msg.reply_text("Reply to a message notification with /aireply <instructions>.")
        return
    instructions = " ".join(context.args) or "Write a helpful, friendly reply."
    system = (
        f"You write Telegram messages on behalf of {OWNER_NAME}, following their instructions. "
        f"{('About ' + OWNER_NAME + ': ' + OWNER_ABOUT) if OWNER_ABOUT else ''}\n"
        f"Output only the message to send, in the sender's language, plain text."
    )
    prompt = f"The notification about their message:\n{note}\n\nInstructions from {OWNER_NAME}: {instructions}"
    try:
        text, label = await ai_generate(owner_models(await get_settings(context.bot)), system, prompt)
    except llm.AIError as e:
        await msg.reply_text(f"❌ AI failed: {clip(str(e), 1000)}")
        return
    try:
        await context.bot.send_message(int(m.group(1)), clip(text))
    except TelegramError as e:
        await msg.reply_text(f"❌ Couldn't deliver: {e}")
        return
    await msg.reply_text(clip(f"✅ Sent ({label}):\n\n{text}"))


# ---------------------------------------------------------------------------
# MESSAGES
# ---------------------------------------------------------------------------

async def handle_owner_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    quoted = msg.reply_to_message
    note = (quoted.text or quoted.caption or "") if quoted else ""
    m = ID_RE.search(note)

    # Reply to a notification -> relay to that person.
    if m:
        try:
            await context.bot.copy_message(int(m.group(1)), from_chat_id=msg.chat_id, message_id=msg.message_id)
            await msg.reply_text("✅ Reply delivered.")
        except TelegramError as e:
            await msg.reply_text(f"❌ Couldn't deliver: {e}")
        return

    # Otherwise -> chat with the AI.
    if not msg.text:
        await msg.reply_text("I can only chat about text. To answer someone, reply to their notification.")
        return
    models = owner_models(await get_settings(context.bot))
    if not models:
        await msg.reply_text("No AI models configured. Use /model provider:model, or set AI_MODELS in Vercel.")
        return
    await context.bot.send_chat_action(msg.chat_id, "typing")
    try:
        text, _ = await ai_generate(models, owner_system_prompt(), with_reply_context(msg, msg.text, "your"))
    except llm.AIError as e:
        await msg.reply_text(f"❌ AI failed: {clip(str(e), 1000)}")
        return
    await msg.reply_text(clip(text))


async def handle_other_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    sender = update.effective_user
    s = await get_settings(context.bot)
    mode, models = s["mode"], others_models(s)
    content = describe(msg)

    # 1. Reply to the sender.
    if mode == "ai" and models:
        await context.bot.send_chat_action(msg.chat_id, "typing")
        try:
            reply, label = await ai_generate(
                models, others_system_prompt(sender), with_reply_context(msg, content, "your")
            )
            source = f"AI · {label}"
        except llm.AIError as e:
            reply, source = AWAY_MESSAGE, f"hard-coded, AI failed: {clip(str(e), 300)}"
    elif mode == "available":
        reply, source = AVAILABLE_MESSAGE, "hard-coded"
    else:
        reply, source = AWAY_MESSAGE, "hard-coded" if mode == "away" else "hard-coded, no AI models set"
    try:
        await msg.reply_text(clip(reply))
    except TelegramError as e:
        logger.warning(f"Could not reply to sender: {e}")

    # 2. Notify the owner: who, when, what, and what the bot replied.
    username = f"@{sender.username}" if sender.username else "no username"
    quoted = msg.reply_to_message
    replying_to = ""
    if quoted and (quoted.text or quoted.caption):
        replying_to = f"↪️ Replying to: {clip(quoted.text or quoted.caption, 300)}\n\n"
    note = (
        f"📩 New message\n\n"
        f"👤 Name: {sender.full_name}\n"
        f"🔗 Username: {username}\n"
        f"🆔 ID: {sender.id}\n"
        f"🕒 Time: {fmt_time(msg.date)}\n\n"
        f"{replying_to}"
        f"💬 Message:\n{clip(content, 1800)}\n\n"
        f"🤖 Bot replied ({source}):\n{clip(reply, 1200)}\n\n"
        f"↩️ Reply to this message to answer them, or reply with /aireply <instructions>."
    )
    try:
        sent = await context.bot.send_message(YOUR_USER_ID, clip(note))
        if media_kind(msg):  # attach the original photo/voice/file under the notification
            await context.bot.copy_message(
                YOUR_USER_ID, from_chat_id=msg.chat_id, message_id=msg.message_id,
                reply_to_message_id=sent.message_id,
            )
    except TelegramError as e:
        logger.exception(f"Could not notify owner: {e}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    if is_owner(update.effective_user):
        await handle_owner_message(update, context)
    else:
        await handle_other_message(update, context)


# ---------------------------------------------------------------------------
# BOT
# ---------------------------------------------------------------------------

BOT_COMMANDS = [
    BotCommand("start", "Start"),
    BotCommand("help", "Show commands"),
    BotCommand("status", "Availability"),
    BotCommand("faq", "Frequently asked questions"),
    BotCommand("contact", "Other ways to reach me"),
]


def build_app() -> Application:
    application = Application.builder().token(BOT_TOKEN).updater(None).build()
    private = filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE
    application.add_handler(CommandHandler("start", start_command, filters=private))
    application.add_handler(CommandHandler("help", help_command, filters=private))
    application.add_handler(CommandHandler("status", status_command, filters=private))
    application.add_handler(CommandHandler("faq", faq_command, filters=private))
    application.add_handler(CommandHandler("contact", contact_command, filters=private))
    application.add_handler(CommandHandler("aireply", aireply_command, filters=private))
    application.add_handler(CommandHandler("model", model_command, filters=private))
    for mode in MODES:
        application.add_handler(CommandHandler(mode, mode_command(mode), filters=private))
    application.add_handler(MessageHandler(
        private & ~filters.COMMAND, handle_message
    ))
    return application


async def process_update(data: dict):
    application = build_app()
    async with application:
        await application.process_update(Update.de_json(data, application.bot))


async def setup_webhook(url: str) -> bool:
    application = build_app()
    async with application:
        ok = await application.bot.set_webhook(
            url=url, secret_token=WEBHOOK_SECRET or None, allowed_updates=["message"],
        )
        await application.bot.set_my_commands(BOT_COMMANDS)
        return ok


# ---------------------------------------------------------------------------
# FLASK (Vercel entrypoint)
# ---------------------------------------------------------------------------

app = Flask(__name__)


@app.route("/setup", methods=["GET"])
def setup():
    """Visit https://<your-app>.vercel.app/setup?key=<WEBHOOK_SECRET> once after deploying."""
    if not WEBHOOK_SECRET:
        return "Set WEBHOOK_SECRET in Vercel first.", 400
    if not hmac.compare_digest(request.args.get("key", ""), WEBHOOK_SECRET):
        return "Forbidden", 403
    url = "https://" + request.host + "/"
    ok = asyncio.run(setup_webhook(url))
    return (f"Webhook set to {url}", 200) if ok else ("Failed to set webhook", 500)


@app.route("/", methods=["POST", "GET"])
def webhook():
    if request.method == "GET":
        return "Bot running on Vercel Serverless!", 200
    if WEBHOOK_SECRET:
        header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(header, WEBHOOK_SECRET):
            return "Forbidden", 403
    try:
        asyncio.run(process_update(request.get_json(force=True)))
    except Exception:
        logger.exception("Update processing failed")  # still 200 so Telegram doesn't retry forever
    return "OK", 200
