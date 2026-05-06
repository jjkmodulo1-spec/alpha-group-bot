import os
import json
import logging
import urllib.request
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
CONFIG_FILE = "config.json"

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.5-flash-lite:generateContent?key=" + GEMINI_API_KEY
)


# --- Config (in-memory cache, disk only on write) ---

_config: dict = {}

def load_config():
    global _config
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            _config = json.load(f)
    else:
        _config = {}

def save_config():
    with open(CONFIG_FILE, "w") as f:
        json.dump(_config, f)

def get_notify_chat_id():
    val = _config.get("notify_chat_id")
    return int(val) if val is not None else None

def get_alpha_config():
    chat_id = _config.get("alpha_chat_id")
    thread_id = _config.get("alpha_thread_id")
    if chat_id is not None and thread_id is not None:
        return int(chat_id), int(thread_id)
    return None, None


# --- Gemini classifier ---

def is_chat_message(text: str) -> bool:
    prompt = (
        "You are a moderator for a private crypto alpha channel on Telegram. "
        "This channel is strictly for sharing trading signals, market intelligence, and crypto analysis. "
        "Your job is to classify each message as CHAT or ALPHA.\n\n"
        "CHAT — delete these:\n"
        "- Greetings and reactions: gm, gg, nice, lol, wagmi, lets go, fire\n"
        "- Personal opinions with no data: I think BTC is going up\n"
        "- Questions to other members: did you see that?, what do you think?\n"
        "- Off-topic talk: anything not directly related to a trade, token, or market event\n"
        "- Hype with no substance: this is huge, moon soon\n\n"
        "ALPHA — keep these:\n"
        "- Contract addresses or token tickers with context\n"
        "- Wallet activity: whale wallet 0x... just bought 500k\n"
        "- Price levels and setups: SOL holding $180 support, watching for breakout\n"
        "- On-chain data or exchange data\n"
        "- News that directly affects a token or market\n"
        "- Chart analysis with specific observations\n\n"
        "When in doubt, classify as ALPHA. Only classify as CHAT if it is clearly casual with zero trading value.\n\n"
        "Reply with exactly one word: CHAT or ALPHA.\n\n"
        f"Message: {text}"
    )

    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 10, "temperature": 0}
    }).encode("utf-8")

    req = urllib.request.Request(
        GEMINI_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            answer = data["candidates"][0]["content"]["parts"][0]["text"].strip().upper()
            logger.info(f"Gemini classified: {answer} — {text[:60]}")
            return answer == "CHAT"
    except Exception as e:
        logger.error(f"Gemini API error: {e}")
        return False  # On error, don't delete


# --- Commands ---

async def is_admin(update: Update) -> bool:
    member = await update.effective_chat.get_member(update.effective_user.id)
    return member.status in ("administrator", "creator")


async def cmd_setalpha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text("Admins only.")
        return

    thread_id = update.message.message_thread_id
    if thread_id is None:
        await update.message.reply_text("Run this command inside a forum thread.")
        return

    _config["alpha_chat_id"] = update.message.chat_id
    _config["alpha_thread_id"] = thread_id
    save_config()

    await update.message.reply_text("Done. This thread is now being monitored.")
    logger.info(f"Alpha thread set: chat={update.message.chat_id}, thread={thread_id}")


async def cmd_clearalpha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text("Admins only.")
        return

    _config.pop("alpha_chat_id", None)
    _config.pop("alpha_thread_id", None)
    save_config()
    await update.message.reply_text("Alpha thread monitoring disabled.")


async def cmd_setnotify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text("Admins only.")
        return

    _config["notify_chat_id"] = update.effective_chat.id
    save_config()
    await update.message.reply_text(
        f"Done. Notifications will be sent to this chat ({update.effective_chat.title or update.effective_chat.id})."
    )


async def cmd_clearnotify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text("Admins only.")
        return

    _config.pop("notify_chat_id", None)
    save_config()
    await update.message.reply_text("Notifications disabled.")


# --- Notify helper ---

async def notify_user(context: ContextTypes.DEFAULT_TYPE, user):
    notify_chat_id = get_notify_chat_id()
    if notify_chat_id is None:
        return
    try:
        mention = f"@{user.username}" if user.username else f'<a href="tg://user?id={user.id}">{user.first_name}</a>'
        await context.bot.send_message(
            chat_id=notify_chat_id,
            text=(
                f"{mention} your message was removed from the alpha channel.\n"
                f"Please keep general conversation here."
            ),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Notify failed for {user.id}: {e}")


# --- Message handler ---

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return

    alpha_chat_id, alpha_thread_id = get_alpha_config()
    if alpha_chat_id is None:
        return

    if message.chat_id != alpha_chat_id or message.message_thread_id != alpha_thread_id:
        return

    user = message.from_user
    if not user:
        return

    # Layer 1: explicit reply to someone else — instant delete, no AI needed
    if message.reply_to_message is not None:
        original_sender = message.reply_to_message.from_user
        if original_sender and original_sender.id != user.id:
            try:
                await message.delete()
                logger.info(f"Deleted explicit reply from @{user.username or user.id}")
                await notify_user(context, user)
            except Exception as e:
                logger.error(f"Delete failed: {e}")
            return

    # Layer 2: skip media-only messages (charts, screenshots — likely alpha)
    if not message.text:
        return

    # Layer 3: Gemini classification
    if is_chat_message(message.text):
        try:
            await message.delete()
            logger.info(f"Deleted chat message from @{user.username or user.id}")
            await notify_user(context, user)
        except Exception as e:
            logger.error(f"Delete failed: {e}")


def main():
    load_config()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("setalpha", cmd_setalpha))
    app.add_handler(CommandHandler("clearalpha", cmd_clearalpha))
    app.add_handler(CommandHandler("setnotify", cmd_setnotify))
    app.add_handler(CommandHandler("clearnotify", cmd_clearnotify))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    logger.info("Alpha Guard is running.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
