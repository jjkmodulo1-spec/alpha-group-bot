import asyncio
import json
import logging
import os
import re
import time
import urllib.request
from collections import defaultdict, deque

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters


logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


BOT_TOKEN = os.environ["BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
CONFIG_FILE = "config.json"

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.5-flash-lite:generateContent?key=" + GEMINI_API_KEY
)

BUFFER_LIMIT = int(os.getenv("BUFFER_LIMIT", "12"))
BUFFER_TTL_SECONDS = int(os.getenv("BUFFER_TTL_SECONDS", "600"))
DELETE_CONFIDENCE_THRESHOLD = float(os.getenv("DELETE_CONFIDENCE_THRESHOLD", "0.82"))
GEMINI_TIMEOUT_SECONDS = int(os.getenv("GEMINI_TIMEOUT_SECONDS", "8"))

STRONG_ALPHA_RE = re.compile(
    r"("
    r"0x[a-fA-F0-9]{40}"
    r"|https?://\S+"
    r"|\$[A-Za-z][A-Za-z0-9_]{1,12}\b"
    r"|\b[A-HJ-NP-Za-km-z1-9]{32,44}\b"
    r"|\b\d+(\.\d+)?\s?%"
    r"|\$\s?\d+(\.\d+)?"
    r"|\b(ca|contract|address|entry|target|targets|tp|sl|stop loss|support|resistance|breakout|retest|mcap|market cap|fdv|holders|liquidity|volume|whale|wallet|dex|cex|listing|launch|unlock|burn|airdrop|claim|funding|open interest|smart money)\b"
    r")",
    re.IGNORECASE,
)


# --- Config ---

_config: dict = {}


def load_config():
    global _config
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            _config = json.load(f)
    else:
        _config = {}


def save_config():
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
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


# --- Rolling context ---

thread_buffers = defaultdict(lambda: deque(maxlen=BUFFER_LIMIT))


def message_text(message) -> str:
    return (message.text or message.caption or "").strip()


def thread_key(message):
    return message.chat_id, message.message_thread_id


def prune_buffer(buf, now: float):
    while buf and now - buf[0]["ts"] > BUFFER_TTL_SECONDS:
        buf.popleft()


def add_to_buffer(message, text: str, deleted: bool):
    user = message.from_user
    if not user or not text:
        return

    now = time.time()
    buf = thread_buffers[thread_key(message)]
    prune_buffer(buf, now)
    buf.append(
        {
            "message_id": message.message_id,
            "user_id": user.id,
            "text": text[:1200],
            "deleted": deleted,
            "is_reply": message.reply_to_message is not None,
            "ts": now,
        }
    )


def get_context_before(message):
    now = time.time()
    buf = thread_buffers[thread_key(message)]
    prune_buffer(buf, now)
    return list(buf)


def user_aliases(context_items, current_user_id, reply_user_id=None):
    aliases = {}

    def alias(uid):
        if uid is None:
            return "Unknown"
        if uid == current_user_id:
            return "CURRENT_USER"
        if uid not in aliases:
            aliases[uid] = f"USER_{len(aliases) + 1}"
        return aliases[uid]

    for item in context_items:
        alias(item["user_id"])
    alias(reply_user_id)
    return alias


def format_context(context_items, current_user_id, reply_user_id=None):
    alias = user_aliases(context_items, current_user_id, reply_user_id)
    lines = []
    for item in context_items[-BUFFER_LIMIT:]:
        state = "deleted earlier" if item["deleted"] else "visible"
        reply_tag = " reply" if item["is_reply"] else ""
        lines.append(f"- {alias(item['user_id'])}{reply_tag} ({state}): {item['text']}")
    return "\n".join(lines) if lines else "- No recent context."


def parse_gemini_json(answer: str) -> dict:
    answer = answer.strip()
    try:
        return json.loads(answer)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", answer, re.DOTALL)
        if not match:
            return {}
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}


# --- Gemini moderation ---

def has_strong_alpha_signal(text: str) -> bool:
    return bool(STRONG_ALPHA_RE.search(text))


def gemini_should_delete(
    text: str,
    context_items,
    reply_text: str | None,
    reply_user_id: int | None,
    current_user_id: int,
) -> tuple[bool, str]:
    if has_strong_alpha_signal(text):
        return False, "strong alpha signal"

    reply_section = "None"
    if reply_text:
        alias = user_aliases(context_items, current_user_id, reply_user_id)
        reply_section = f"{alias(reply_user_id)}: {reply_text[:1200]}"

    prompt = (
        "You are moderating a Telegram crypto alpha thread.\n"
        "The goal is to remove regular chat from the alpha thread, not only two-person conversations.\n"
        "Judge ONLY the CURRENT_MESSAGE. Use RECENT_CONTEXT and REPLY_TARGET only to understand whether the current message adds alpha value.\n\n"
        "DELETE the current message only if it is clearly regular chat with no useful alpha value:\n"
        "- greetings, jokes, memes, thanks, reactions, hype, emojis, casual comments\n"
        "- vague social messages like 'gm', 'lol', 'nice', 'lfg', 'send it', 'crazy', 'what do you think?'\n"
        "- back-and-forth talk that does not add a trade idea, market fact, token detail, risk note, or useful question\n\n"
        "KEEP the current message if it has any alpha value, even if it is short or conversational:\n"
        "- contract address, ticker, wallet, link, chart, price level, entry, target, support, resistance, volume, mcap, holders, news, on-chain data\n"
        "- a useful alpha question such as asking for CA, entry, mcap, source, wallet, timeframe, risk, chart, or confirmation\n"
        "- a short follow-up that changes a trade decision, such as 'wait for pullback', 'entry here', 'dev sold', 'volume coming in', 'not buying yet'\n"
        "- anything ambiguous. When unsure, KEEP.\n\n"
        "Return only JSON with this shape:\n"
        "{\"action\":\"DELETE\" or \"KEEP\", \"confidence\":0.0-1.0, \"reason\":\"short reason\"}\n\n"
        f"RECENT_CONTEXT:\n{format_context(context_items, current_user_id, reply_user_id)}\n\n"
        f"REPLY_TARGET:\n{reply_section}\n\n"
        f"CURRENT_MESSAGE:\nCURRENT_USER: {text[:1600]}"
    )

    body = json.dumps(
        {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "maxOutputTokens": 120,
                "temperature": 0,
                "responseMimeType": "application/json",
            },
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        GEMINI_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=GEMINI_TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read())
            answer = data["candidates"][0]["content"]["parts"][0]["text"]
            verdict = parse_gemini_json(answer)
    except Exception as e:
        logger.error(f"Gemini API error: {e}")
        return False, "gemini error"

    action = str(verdict.get("action", "")).upper()
    try:
        confidence = float(verdict.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0
    reason = str(verdict.get("reason", "no reason"))

    logger.info(f"Gemini verdict={action} confidence={confidence:.2f} reason={reason} text={text[:80]}")
    should_delete = action == "DELETE" and confidence >= DELETE_CONFIDENCE_THRESHOLD
    return should_delete, reason


async def should_delete_message(message, text: str, context_items) -> tuple[bool, str]:
    user = message.from_user
    reply_text = None
    reply_user_id = None

    if message.reply_to_message:
        reply_text = message_text(message.reply_to_message)
        if message.reply_to_message.from_user:
            reply_user_id = message.reply_to_message.from_user.id

    return await asyncio.to_thread(
        gemini_should_delete,
        text,
        context_items,
        reply_text,
        reply_user_id,
        user.id,
    )


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


# --- Notifications ---

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
            parse_mode="HTML",
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

    text = message_text(message)

    # Media-only messages are usually charts/screenshots and are kept.
    if not text:
        add_to_buffer(message, "[media-only message]", deleted=False)
        return

    context_items = get_context_before(message)
    delete, reason = await should_delete_message(message, text, context_items)

    if delete:
        try:
            await message.delete()
            logger.info(f"Deleted regular chat from @{user.username or user.id}: {reason}")
            await notify_user(context, user)
            add_to_buffer(message, text, deleted=True)
        except Exception as e:
            logger.error(f"Delete failed: {e}")
            add_to_buffer(message, text, deleted=False)
        return

    add_to_buffer(message, text, deleted=False)


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
