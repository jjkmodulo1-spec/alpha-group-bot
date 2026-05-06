import os
import json
import logging
from collections import deque
from datetime import datetime
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
CONFIG_FILE = "config.json"

CHAIN_THRESHOLD = 3
CHAIN_EXPIRY = 120
BUFFER_SIZE = 10


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


# --- Conversation chain tracker ---

# Each entry: (user_id, user_obj, chat_id, msg_id, timestamp)
message_buffer: deque = deque(maxlen=BUFFER_SIZE)

# Key: frozenset(uid1, uid2)
# Value: {"messages": [(chat_id, msg_id, user_obj, timestamp)], "last_activity": datetime}
conversation_chains: dict = {}


def cleanup_chains():
    now = datetime.now()
    expired = [
        key for key, val in conversation_chains.items()
        if (now - val["last_activity"]).total_seconds() > CHAIN_EXPIRY
    ]
    for key in expired:
        del conversation_chains[key]


def find_recent_partner(current_user_id: int):
    for entry in reversed(message_buffer):
        uid, _, _, _, ts = entry
        if uid != current_user_id:
            if (datetime.now() - ts).total_seconds() <= CHAIN_EXPIRY:
                return uid
    return None


def track_chain(user, chat_id: int, msg_id: int, partner_id: int) -> list:
    cleanup_chains()

    pair = frozenset([user.id, partner_id])
    now = datetime.now()

    if pair not in conversation_chains:
        conversation_chains[pair] = {"messages": [], "last_activity": now}

    conversation_chains[pair]["messages"].append((chat_id, msg_id, user, now))
    conversation_chains[pair]["last_activity"] = now

    logger.info(f"Chain {set(pair)}: {len(conversation_chains[pair]['messages'])} msgs")

    if len(conversation_chains[pair]["messages"]) >= CHAIN_THRESHOLD:
        # Only take the last 2 messages, leave the rest
        last_two = [(m[0], m[1], m[2]) for m in conversation_chains[pair]["messages"][-2:]]
        del conversation_chains[pair]
        return last_two

    return []


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

    # Layer 1: explicit reply to someone else
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

    # Layer 3: chain detection
    partner_id = find_recent_partner(user.id)
    message_buffer.append((user.id, user, message.chat_id, message.message_id, datetime.now()))

    if partner_id is not None:
        to_delete = track_chain(user, message.chat_id, message.message_id, partner_id)

        if to_delete:
            logger.info(f"Chain hit. Deleting last 2 messages.")

            # Notify only the 2 users whose messages are being deleted
            notified = set()
            users_to_notify = []
            for _, _, u in to_delete:
                if u.id not in notified:
                    notified.add(u.id)
                    users_to_notify.append(u)

            for chat_id, msg_id, _ in to_delete:
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
                except Exception as e:
                    logger.error(f"Failed to delete msg {msg_id}: {e}")

            for u in users_to_notify:
                await notify_user(context, u)


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
