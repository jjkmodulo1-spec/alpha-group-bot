import asyncio
import html
import io
import json
import logging
import os
import random
import re
import time
import urllib.parse
import urllib.request
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters


logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


BOT_TOKEN = os.environ["BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
CONFIG_FILE = os.getenv("CONFIG_FILE", "/data/config.json")


def env_flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() not in {"0", "false", "no", "off"}

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.5-flash-lite:generateContent?key=" + GEMINI_API_KEY
)

BUFFER_LIMIT = int(os.getenv("BUFFER_LIMIT", "12"))
BUFFER_TTL_SECONDS = int(os.getenv("BUFFER_TTL_SECONDS", "600"))
DELETE_CONFIDENCE_THRESHOLD = float(os.getenv("DELETE_CONFIDENCE_THRESHOLD", "0.82"))
GEMINI_TIMEOUT_SECONDS = int(os.getenv("GEMINI_TIMEOUT_SECONDS", "8"))
TOKEN_DETAILS_ENABLED = env_flag("TOKEN_DETAILS_ENABLED", "1")
TOKEN_CHAIN_ID = os.getenv("TOKEN_CHAIN_ID", "base").strip().lower()
TOKEN_LOOKUP_TIMEOUT_SECONDS = int(os.getenv("TOKEN_LOOKUP_TIMEOUT_SECONDS", "6"))
TOKEN_ORDER_TIMEOUT_SECONDS = int(os.getenv("TOKEN_ORDER_TIMEOUT_SECONDS", "3"))
TOKEN_REPLY_CACHE_SECONDS = int(os.getenv("TOKEN_REPLY_CACHE_SECONDS", "1800"))
TOKEN_LOOKUP_MISS_CACHE_SECONDS = int(os.getenv("TOKEN_LOOKUP_MISS_CACHE_SECONDS", "300"))
TOKEN_MAX_REPLIES_PER_MESSAGE = int(os.getenv("TOKEN_MAX_REPLIES_PER_MESSAGE", "1"))
PNL_ANIME_IMAGES_ENABLED = env_flag("PNL_ANIME_IMAGES_ENABLED", "1")
PNL_ANIME_CHARACTER_DIR = os.getenv("PNL_ANIME_CHARACTER_DIR", "assets/pnl_characters")
PNL_ANIME_API_BASE = os.getenv("PNL_ANIME_API_BASE", "https://nekos.best/api/v2")
PNL_BOT_LABEL = os.getenv("PNL_BOT_LABEL", "@AlphaBot")
TOKEN_TICKER_BLOCKLIST = {
    item.strip().upper()
    for item in os.getenv(
        "TOKEN_TICKER_BLOCKLIST",
        "BTC,ETH,SOL,USDC,USDT,USD,WETH,WBTC,BNB,BASE",
    ).split(",")
    if item.strip()
}

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

TICKER_DROP_RE = re.compile(r"(?<![\w/])\$([A-Za-z][A-Za-z0-9_]{1,12})\b")
EVM_ADDRESS_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b")

CHAIN_LABELS = {
    "base": "Base",
}

ANNOUNCEMENT_RE = re.compile(
    r"\b("
    r"announcement|announce|announcing|update|official|notice|heads up|reminder|schedule|scheduled|"
    r"ama|space|spaces|livestream|live stream|call starts|starting soon|mint|snapshot|claim|"
    r"partnership|migration|maintenance|launch time|listing time|deadline|thread rules"
    r")\b",
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

    # Hydrate the in-memory chat_seen_users from persisted config so alarm
    # mention batches survive restarts.
    stored = _config.get("seen_users") or {}
    for chat_id_str, users in stored.items():
        chat_id = int(chat_id_str)
        for uid_str, mention in users.items():
            chat_seen_users[chat_id][int(uid_str)] = mention

def save_config():
    import tempfile
    dir_ = os.path.dirname(os.path.abspath(CONFIG_FILE))
    with tempfile.NamedTemporaryFile("w", dir=dir_, delete=False, suffix=".tmp", encoding="utf-8") as f:
        json.dump(_config, f)
        tmp = f.name
    os.replace(tmp, CONFIG_FILE)

def get_trusted_users(chat_id: int) -> list[int]:
    return _config.get("trusted_users", {}).get(str(chat_id), [])

def add_trusted_user(chat_id: int, user_id: int):
    trusted = _config.setdefault("trusted_users", {})
    key = str(chat_id)
    if user_id not in trusted.get(key, []):
        trusted.setdefault(key, []).append(user_id)
    save_config()

def remove_trusted_user(chat_id: int, user_id: int):
    trusted = _config.get("trusted_users", {})
    key = str(chat_id)
    if key in trusted and user_id in trusted[key]:
        trusted[key].remove(user_id)
    save_config()


def get_notify_chat_id():
    val = _config.get("notify_chat_id")
    return int(val) if val is not None else None

def get_alpha_config():
    chat_id = _config.get("alpha_chat_id")
    thread_id = _config.get("alpha_thread_id")
    if chat_id is not None and thread_id is not None:
        return int(chat_id), int(thread_id)
    return None, None


def get_ca_reply_config():
    enabled = TOKEN_DETAILS_ENABLED and bool(_config.get("ca_replies_enabled", True))
    if not enabled:
        return False, None, None

    chat_id = _config.get("ca_chat_id")
    thread_id = _config.get("ca_thread_id")
    if chat_id is None:
        return True, None, None

    return True, int(chat_id), int(thread_id) if thread_id is not None else None


def get_pnl_bot_label() -> str:
    return str(_config.get("pnl_bot_label") or PNL_BOT_LABEL)


def ca_drop_key(chat_id: int, thread_id: int | None, chain_id: str, address: str) -> str:
    thread_part = "main" if thread_id is None else str(thread_id)
    return f"{chat_id}:{thread_part}:{chain_id}:{address.lower()}"


def find_ca_drop_record(chat_id: int, thread_id: int | None, chain_id: str, address: str) -> dict | None:
    drops = _config.get("ca_drops") or {}
    exact_key = ca_drop_key(chat_id, thread_id, chain_id, address)
    exact = drops.get(exact_key)
    if exact:
        return exact

    suffix = f":{chain_id}:{address.lower()}"
    same_chat_records = [
        record
        for key, record in drops.items()
        if key.startswith(f"{chat_id}:") and key.endswith(suffix)
    ]
    if not same_chat_records:
        return None

    return min(same_chat_records, key=lambda record: float(record.get("dropped_at") or 0))


def save_ca_drop_if_new(message, token_result: dict):
    if token_result.get("query_type") != "address":
        return

    price_usd = safe_float(token_result.get("price_usd"))
    if price_usd is None or price_usd <= 0:
        return

    key = ca_drop_key(
        message.chat_id,
        message.message_thread_id,
        token_result["chain_id"],
        token_result["token_address"],
    )
    drops = _config.setdefault("ca_drops", {})
    if key in drops:
        return

    user = message.from_user
    caller = None
    if user:
        caller = user.username or user.first_name or str(user.id)

    drops[key] = {
        "chain_id": token_result["chain_id"],
        "token_address": token_result["token_address"],
        "symbol": token_result.get("symbol"),
        "name": token_result.get("name"),
        "drop_price_usd": price_usd,
        "drop_market_cap": token_result.get("market_cap"),
        "caller": caller,
        "dropped_at": time.time(),
        "chat_id": message.chat_id,
        "thread_id": message.message_thread_id,
        "message_id": message.message_id,
    }
    save_config()


# --- Rolling context ---

thread_buffers = defaultdict(lambda: deque(maxlen=BUFFER_LIMIT))

# Tracks every user the bot has seen post in a chat — used for alarm mentions
# { chat_id: { user_id: mention_html } }
chat_seen_users: dict[int, dict[int, str]] = defaultdict(dict)

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


# --- Token details ---

token_reply_cache: dict[tuple[int, int | None, str], float] = {}


def safe_float(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def trim_number(value: float, decimals: int = 1) -> str:
    return f"{value:.{decimals}f}".rstrip("0").rstrip(".")


def format_compact_number(value: float | None, decimals: int = 1) -> str:
    if value is None:
        return "n/a"

    abs_value = abs(value)
    for threshold, suffix in (
        (1_000_000_000, "B"),
        (1_000_000, "M"),
        (1_000, "K"),
    ):
        if abs_value >= threshold:
            return f"{trim_number(value / threshold, decimals)}{suffix}"

    if abs_value >= 100:
        return f"{value:,.0f}"
    if abs_value >= 10:
        return trim_number(value, 1)
    return trim_number(value, 2)


def format_usd(value: float | None) -> str:
    if value is None:
        return "n/a"
    if abs(value) < 1:
        return f"${value:.4f}".rstrip("0").rstrip(".")
    return f"${format_compact_number(value)}"


def format_price(value) -> str:
    price = safe_float(value)
    if price is None:
        return "n/a"
    if price >= 1:
        return f"${price:,.4f}".rstrip("0").rstrip(".")
    if price >= 0.01:
        return f"${price:.6f}".rstrip("0").rstrip(".")
    return f"${price:.10f}".rstrip("0").rstrip(".")


def format_percent(value) -> str:
    pct = safe_float(value)
    if pct is None:
        return "n/a"
    sign = "+" if pct > 0 else ""
    return f"{sign}{format_compact_number(pct, 1)}%"


def format_token_amount(value: float | None) -> str:
    if value is None:
        return "n/a"
    if abs(value) >= 1_000:
        return format_compact_number(value, 1)
    if abs(value) >= 1:
        return trim_number(value, 2)
    return f"{value:.4g}"


def format_age(pair_created_at) -> str:
    created = safe_float(pair_created_at)
    if not created:
        return "n/a"

    created_seconds = created / 1000 if created > 10_000_000_000 else created
    seconds = max(0, int(time.time() - created_seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    if seconds < 2_592_000:
        return f"{seconds // 86400}d"
    return f"{seconds // 2_592_000}mo"


def format_elapsed(seconds: float | int | None) -> str:
    if seconds is None:
        return "n/a"

    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    if seconds < 2_592_000:
        return f"{seconds // 86400}d"
    return f"{seconds // 2_592_000}mo"


def extract_token_queries(text: str) -> list[dict]:
    if not TOKEN_DETAILS_ENABLED:
        return []

    queries = []
    seen = set()

    for match in EVM_ADDRESS_RE.finditer(text):
        address = match.group(0)
        key = ("address", "base", address.lower())
        if key not in seen:
            queries.append({"type": "address", "value": address, "chain_id": "base"})
            seen.add(key)

    for match in TICKER_DROP_RE.finditer(text):
        symbol = match.group(1).upper()
        if symbol in TOKEN_TICKER_BLOCKLIST:
            continue
        key = ("ticker", TOKEN_CHAIN_ID, symbol)
        if key not in seen:
            queries.append({"type": "ticker", "value": symbol, "chain_id": TOKEN_CHAIN_ID})
            seen.add(key)

    return queries


def token_cache_key(message, key: str) -> tuple[int, int | None, str]:
    return message.chat_id, message.message_thread_id, key


def token_cache_active(key: tuple[int, int | None, str]) -> bool:
    now = time.time()
    expiry = token_reply_cache.get(key)
    if expiry is None:
        return False
    if expiry <= now:
        token_reply_cache.pop(key, None)
        return False
    return True


def mark_token_cache(key: tuple[int, int | None, str], ttl: int):
    token_reply_cache[key] = time.time() + ttl


def fetch_json_url(url: str, timeout: int | None = None):
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "alpha-group-bot/1.0",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout or TOKEN_LOOKUP_TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read())


def pair_liquidity_usd(pair: dict) -> float:
    liquidity = pair.get("liquidity") or {}
    return safe_float(liquidity.get("usd")) or 0


def pair_volume_24h(pair: dict) -> float:
    volume = pair.get("volume") or {}
    return safe_float(volume.get("h24")) or 0


def pair_txns_24h(pair: dict) -> int:
    txns = (pair.get("txns") or {}).get("h24") or {}
    return int(txns.get("buys") or 0) + int(txns.get("sells") or 0)


def pair_score(pair: dict) -> tuple[float, float, int, float]:
    cap = safe_float(pair.get("marketCap")) or safe_float(pair.get("fdv")) or 0
    return pair_liquidity_usd(pair), pair_volume_24h(pair), pair_txns_24h(pair), cap


def query_chain_id(query: dict) -> str:
    return str(query.get("chain_id") or TOKEN_CHAIN_ID).lower()


def is_target_chain(pair: dict, chain_id: str) -> bool:
    return str(pair.get("chainId", "")).lower() == chain_id


def pick_best_pair(pairs: list[dict], query: dict) -> dict | None:
    chain_id = query_chain_id(query)
    chain_pairs = [pair for pair in pairs if is_target_chain(pair, chain_id)]
    if not chain_pairs:
        return None

    if query["type"] == "ticker":
        symbol = query["value"].upper()
        chain_pairs = [
            pair
            for pair in chain_pairs
            if str((pair.get("baseToken") or {}).get("symbol", "")).upper() == symbol
        ]
    else:
        address = query["value"].lower()
        chain_pairs = [
            pair
            for pair in chain_pairs
            if str((pair.get("baseToken") or {}).get("address", "")).lower() == address
            or str((pair.get("quoteToken") or {}).get("address", "")).lower() == address
        ]

    if not chain_pairs:
        return None
    return max(chain_pairs, key=pair_score)


def lookup_pairs(query: dict) -> list[dict]:
    chain_id = query_chain_id(query)
    if query["type"] == "address":
        address = urllib.parse.quote(query["value"], safe="")
        url = f"https://api.dexscreener.com/token-pairs/v1/{chain_id}/{address}"
        data = fetch_json_url(url)
        return data if isinstance(data, list) else data.get("pairs", [])

    symbol = urllib.parse.quote(query["value"], safe="")
    url = f"https://api.dexscreener.com/latest/dex/search?q={symbol}"
    data = fetch_json_url(url)
    return data.get("pairs", []) if isinstance(data, dict) else []


def token_for_query(pair: dict, query: dict) -> dict:
    base_token = pair.get("baseToken") or {}
    quote_token = pair.get("quoteToken") or {}
    if query["type"] == "address":
        address = query["value"].lower()
        if str(quote_token.get("address", "")).lower() == address:
            return quote_token
    return base_token


def fetch_dex_orders(token_address: str, chain_id: str) -> dict:
    if not token_address:
        return {"paid": None, "ads": None, "cto": None}

    address = urllib.parse.quote(token_address, safe="")
    url = f"https://api.dexscreener.com/orders/v1/{chain_id}/{address}"
    try:
        data = fetch_json_url(url, timeout=TOKEN_ORDER_TIMEOUT_SECONDS)
    except Exception as e:
        logger.info(f"DexScreener orders lookup failed for {token_address}: {e}")
        return {"paid": None, "ads": None, "cto": None}

    orders = data if isinstance(data, list) else []

    def active(order):
        return str(order.get("status", "")).lower() in {"approved", "processing"}

    return {
        "paid": any(order.get("type") == "tokenProfile" and active(order) for order in orders),
        "ads": any(order.get("type") in {"tokenAd", "trendingBarAd"} and active(order) for order in orders),
        "cto": any(order.get("type") == "communityTakeover" and active(order) for order in orders),
    }


def html_link(label: str, url: str | None) -> str | None:
    if not url or not str(url).startswith("http"):
        return None
    return f'<a href="{html.escape(str(url), quote=True)}">{html.escape(label)}</a>'


def social_url(platform: str, handle: str | None, url: str | None) -> str | None:
    if url:
        return url
    if not handle:
        return None

    clean_handle = handle.lstrip("@")
    platform = platform.lower()
    if platform in {"twitter", "x"}:
        return f"https://x.com/{clean_handle}"
    if platform in {"telegram", "tg"}:
        return f"https://t.me/{clean_handle}"
    if platform == "instagram":
        return f"https://instagram.com/{clean_handle}"
    if platform == "facebook":
        return f"https://facebook.com/{clean_handle}"
    return None


def collect_social_links(pair: dict) -> list[str]:
    info = pair.get("info") or {}
    links = []
    seen = set()

    def add(label, url):
        link = html_link(label, url)
        if not link:
            return
        key = str(url).lower()
        if key not in seen:
            links.append(link)
            seen.add(key)

    for website in info.get("websites") or []:
        add(website.get("label") or "Web", website.get("url"))

    platform_labels = {
        "twitter": "X",
        "x": "X",
        "telegram": "TG",
        "tg": "TG",
        "instagram": "IG",
        "facebook": "Fb",
        "discord": "Discord",
    }
    for social in info.get("socials") or []:
        platform = str(social.get("platform") or social.get("type") or "Social")
        label = platform_labels.get(platform.lower(), platform.title())
        url = social_url(platform, social.get("handle"), social.get("url"))
        add(label, url)

    return links[:6]


def chain_label(chain_id: str) -> str:
    return CHAIN_LABELS.get(chain_id, chain_id.title())


def explorer_link(token_address: str, chain_id: str) -> str | None:
    if not token_address:
        return None
    if chain_id == "base":
        return f"https://basescan.org/token/{token_address}"
    return None


def swap_link(token_address: str, chain_id: str) -> str | None:
    if not token_address:
        return None
    if chain_id == "base":
        return f"https://app.uniswap.org/swap?chain=base&outputCurrency={urllib.parse.quote(token_address, safe='')}"
    return None


def format_token_details(pair: dict, token: dict, orders: dict, chain_id: str) -> str:
    symbol = str(token.get("symbol") or "?").upper()
    name = str(token.get("name") or symbol)
    token_address = str(token.get("address") or "")
    quote_token = pair.get("quoteToken") or {}
    quote_symbol = str(quote_token.get("symbol") or "")

    liquidity = pair.get("liquidity") or {}
    volume = pair.get("volume") or {}
    txns = pair.get("txns") or {}
    price_change = pair.get("priceChange") or {}
    boosts = pair.get("boosts") or {}

    h1_change = safe_float(price_change.get("h1"))
    h24_change = safe_float(price_change.get("h24"))
    change_label = f"{format_percent(h1_change)} 1h" if h1_change is not None else f"{format_percent(h24_change)} 24h"

    mc = safe_float(pair.get("marketCap"))
    fdv = safe_float(pair.get("fdv"))
    cap_value = mc if mc is not None else fdv
    fdv_part = ""
    if fdv is not None and (mc is None or abs(fdv - mc) / max(fdv, mc, 1) > 0.05):
        fdv_part = f" • FDV {format_usd(fdv)}"

    quote_liq = safe_float(liquidity.get("quote"))
    quote_liq_part = f" [{format_token_amount(quote_liq)} {html.escape(quote_symbol)}]" if quote_liq is not None and quote_symbol else ""

    h1_volume = safe_float(volume.get("h1"))
    h24_volume = safe_float(volume.get("h24"))
    volume_line = f"📊 Vol:    {format_usd(h1_volume)} [1h]"
    if h24_volume is not None:
        volume_line += f" • {format_usd(h24_volume)} [24h]"

    h1_txns = txns.get("h1") or {}
    buys = int(h1_txns.get("buys") or 0)
    sells = int(h1_txns.get("sells") or 0)
    txn_total = buys + sells

    def status_icon(value):
        if value is True:
            return "✅"
        if value is False:
            return "❌"
        return "?"

    dex_paid = status_icon(orders.get("paid"))
    ads_paid = status_icon(orders.get("ads"))
    cto = " • 🤝 CTO" if orders.get("cto") else ""
    active_boosts = int(boosts.get("active") or 0)

    chart_url = pair.get("url") or f"https://dexscreener.com/{chain_id}/{pair.get('pairAddress', '')}"
    explorer_url = explorer_link(token_address, chain_id)
    swap_url = swap_link(token_address, chain_id)
    link_parts = [
        html_link("Chart", chart_url),
        html_link("Explorer", explorer_url),
        html_link("Swap", swap_url),
    ]
    link_line = " | ".join(part for part in link_parts if part)

    social_parts = collect_social_links(pair)
    social_line = " | ".join(social_parts) if social_parts else "None found"

    lines = [
        f"💊 {html.escape(name)} • ${html.escape(symbol)}",
        f"🕒 Age:  {format_age(pair.get('pairCreatedAt'))} [{change_label}] • 🧬 {chain_label(chain_id)}{cto}",
        f"💵 Price: {format_price(pair.get('priceUsd'))}",
        f"💰 MC:   {format_usd(cap_value)}{fdv_part}",
        f"💧 Liq:  {format_usd(safe_float(liquidity.get('usd')))}{quote_liq_part}",
        volume_line,
    ]

    if txn_total:
        lines.append(f"⚡ Txns: {txn_total} [1h] • 🟢 {buys} / 🔴 {sells}")

    lines.extend(
        [
            f"🦅 Dex: Paid{dex_paid} Ads{ads_paid} {active_boosts}⚡",
            f"💼 PnL: /pnl {html.escape(token_address)}",
            "",
            f"<code>{html.escape(token_address)}</code>",
        ]
    )

    if link_line:
        lines.append(f"📊 {link_line}")
    lines.append(f"👻 Socials: {social_line}")

    return "\n".join(lines)


def build_pnl_metrics(pair: dict, token: dict, chain_id: str, drop_record: dict) -> dict:
    symbol = str(token.get("symbol") or "?").upper()
    name = str(token.get("name") or symbol)
    token_address = str(token.get("address") or "")
    volume = pair.get("volume") or {}
    liquidity = pair.get("liquidity") or {}

    drop_price = safe_float(drop_record.get("drop_price_usd"))
    current_price = safe_float(pair.get("priceUsd"))
    multiplier = None
    pnl_percent = None
    if drop_price and current_price is not None:
        multiplier = current_price / drop_price
        pnl_percent = (multiplier - 1) * 100

    dropped_at = safe_float(drop_record.get("dropped_at"))
    dropped_age = format_elapsed(time.time() - dropped_at) if dropped_at else "n/a"
    multiplier_text = f"{trim_number(multiplier, 2)}x" if multiplier is not None else "n/a"
    percent_text = format_percent(pnl_percent) if pnl_percent is not None else "n/a"
    market_cap = safe_float(pair.get("marketCap")) or safe_float(pair.get("fdv"))
    drop_market_cap = safe_float(drop_record.get("drop_market_cap"))
    if drop_market_cap is None and market_cap is not None and multiplier:
        drop_market_cap = market_cap / multiplier

    return {
        "symbol": symbol,
        "name": name,
        "token_address": token_address,
        "chain_id": chain_id,
        "drop_price": drop_price,
        "current_price": current_price,
        "multiplier": multiplier,
        "pnl_percent": pnl_percent,
        "multiplier_text": multiplier_text,
        "percent_text": percent_text,
        "dropped_age": dropped_age,
        "token_age": format_age(pair.get("pairCreatedAt")),
        "market_cap": market_cap,
        "drop_market_cap": drop_market_cap,
        "liquidity_usd": safe_float(liquidity.get("usd")),
        "volume_h1": safe_float(volume.get("h1")),
        "volume_h24": safe_float(volume.get("h24")),
        "chart_url": pair.get("url"),
        "explorer_url": explorer_link(token_address, chain_id),
        "image_url": (pair.get("info") or {}).get("imageUrl"),
        "caller": drop_record.get("caller") or "group call",
    }


def format_pnl_details(pair: dict, token: dict, chain_id: str, drop_record: dict) -> str:
    metrics = build_pnl_metrics(pair, token, chain_id, drop_record)

    lines = [
        f"💼 /pnl {html.escape(metrics['name'])} • ${html.escape(metrics['symbol'])}",
        f"📈 Since group CA drop: {metrics['multiplier_text']} [{metrics['percent_text']}]",
        f"🕒 Dropped: {metrics['dropped_age']} ago at {format_price(metrics['drop_price'])}",
        f"💵 Now: {format_price(metrics['current_price'])}",
        f"🧬 {chain_label(chain_id)} • Token age {metrics['token_age']}",
        f"💰 MC: {format_usd(metrics['market_cap'])}",
        f"💧 Liq: {format_usd(metrics['liquidity_usd'])}",
        f"📊 Vol: {format_usd(metrics['volume_h1'])} [1h] • {format_usd(metrics['volume_h24'])} [24h]",
        "",
        f"<code>{html.escape(metrics['token_address'])}</code>",
    ]

    chart = html_link("Chart", metrics["chart_url"])
    explorer = html_link("Explorer", metrics["explorer_url"])
    links = " | ".join(link for link in (chart, explorer) if link)
    if links:
        lines.append(f"📊 {links}")

    return "\n".join(lines)


def load_font(size: int, bold: bool = False):
    from PIL import ImageFont

    font_names = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for font_name in font_names:
        try:
            return ImageFont.truetype(font_name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def fit_text(draw, text: str, font, max_width: int, min_size: int = 24, bold: bool = False):
    size = getattr(font, "size", min_size)
    while size > min_size and draw.textbbox((0, 0), text, font=font)[2] > max_width:
        size -= 2
        font = load_font(size, bold=bold)
    return font


def draw_centered_text(draw, xy, text: str, font, fill):
    x, y = xy
    bbox = draw.textbbox((0, 0), text, font=font)
    draw.text((x - (bbox[2] - bbox[0]) / 2, y - (bbox[3] - bbox[1]) / 2), text, font=font, fill=fill)


def fetch_image_from_url(url: str):
    from PIL import Image

    if not url or not str(url).startswith("http"):
        raise ValueError("image URL is missing")

    req = urllib.request.Request(
        url,
        headers={"Accept": "image/*", "User-Agent": "alpha-group-bot/1.0"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=TOKEN_LOOKUP_TIMEOUT_SECONDS) as resp:
        data = resp.read(4_000_000)

    image = Image.open(io.BytesIO(data)).convert("RGBA")
    if image.width < 16 or image.height < 16:
        raise ValueError("image is too small")
    return image


def fetch_token_image(url: str):
    return fetch_image_from_url(url)


def pnl_reaction_name(multiplier: float | None) -> str:
    if multiplier is None:
        return "flat"
    if multiplier < 1:
        return "rekt"
    if multiplier < 1.5:
        return "flat"
    if multiplier < 3:
        return "win"
    if multiplier < 10:
        return "send"
    return "moon"


def anime_categories(reaction: str) -> list[str]:
    return {
        "rekt": ["cry", "slap", "bonk"],
        "flat": ["smug", "wave", "poke"],
        "win": ["happy", "smile", "highfive", "wink"],
        "send": ["dance", "happy", "highfive", "smile"],
        "moon": ["dance", "happy", "smile", "wink"],
    }.get(reaction, ["happy"])


def local_anime_character_paths(reaction: str) -> list[str]:
    roots = [
        os.path.join(PNL_ANIME_CHARACTER_DIR, reaction),
        PNL_ANIME_CHARACTER_DIR,
    ]
    paths = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            path = os.path.join(root, name)
            if not os.path.isfile(path):
                continue
            if name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                paths.append(path)
    return paths


def load_local_anime_character(path: str):
    from PIL import Image

    image = Image.open(path).convert("RGBA")
    if image.width < 16 or image.height < 16:
        raise ValueError("local anime character image is too small")
    return image


def fetch_anime_character_image(multiplier: float | None, seed_key: str = ""):
    if not PNL_ANIME_IMAGES_ENABLED:
        raise ValueError("anime images are disabled")

    reaction = pnl_reaction_name(multiplier)
    rng = random.Random(f"{reaction}:{seed_key}")
    local_paths = local_anime_character_paths(reaction)
    if local_paths:
        return load_local_anime_character(local_paths[rng.randrange(len(local_paths))])

    categories = anime_categories(reaction)
    last_error = None
    for category in rng.sample(categories, len(categories)):
        try:
            data = fetch_json_url(
                f"{PNL_ANIME_API_BASE.rstrip('/')}/{urllib.parse.quote(category, safe='')}",
                timeout=TOKEN_LOOKUP_TIMEOUT_SECONDS,
            )
            image_url = None
            if isinstance(data, dict):
                image_url = data.get("url")
                results = data.get("results")
                if not image_url and isinstance(results, list) and results:
                    image_url = results[0].get("url")
            if image_url:
                return fetch_image_from_url(image_url)
        except Exception as e:
            last_error = e

    raise ValueError(f"anime character fetch failed: {last_error}")


def image_average_color(image) -> tuple[int, int, int]:
    from PIL import ImageStat

    thumb = image.convert("RGB").resize((1, 1))
    color = ImageStat.Stat(thumb).mean
    return tuple(max(40, min(235, int(c))) for c in color)


def generate_pnl_image(pair: dict, token: dict, chain_id: str, drop_record: dict) -> io.BytesIO:
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

    metrics = build_pnl_metrics(pair, token, chain_id, drop_record)
    seed = f"{metrics['token_address']}:{drop_record.get('dropped_at')}"
    character_image = fetch_anime_character_image(metrics["multiplier"], seed)
    token_image = None
    if metrics["image_url"]:
        try:
            token_image = fetch_token_image(metrics["image_url"])
        except Exception as e:
            logger.info(f"Token image unavailable for PnL background: {e}")

    width, height = 1280, 720
    rng = random.Random(seed)

    source_image = character_image
    accent = image_average_color(source_image)
    green = (34, 255, 24)
    lime = (165, 255, 71)
    red = (255, 80, 104)
    gold = (255, 222, 100)

    base_bg = ImageOps.fit(source_image.convert("RGB"), (width, height), method=Image.Resampling.LANCZOS)
    base_bg = base_bg.filter(ImageFilter.GaussianBlur(26))
    base_bg = ImageEnhance.Color(base_bg).enhance(1.65)
    base_bg = ImageEnhance.Brightness(base_bg).enhance(0.34)
    image = base_bg.convert("RGBA")
    draw = ImageDraw.Draw(image, "RGBA")

    draw.rectangle([0, 0, width, height], fill=(0, 22, 8, 118))
    draw.rectangle([0, 0, width, height], fill=(*accent, 28))
    for _ in range(20):
        cx = rng.randint(-100, width + 100)
        cy = rng.randint(-80, height + 80)
        radius = rng.randint(45, 180)
        color = rng.choice([accent, green, lime, gold])
        draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=(*color, rng.randint(12, 40)))

    draw.rounded_rectangle([26, 26, 1254, 694], radius=28, fill=(0, 0, 0, 76), outline=(120, 255, 107, 155), width=2)
    draw.rounded_rectangle([40, 40, 1240, 680], radius=24, outline=(255, 255, 255, 58), width=1)

    if token_image:
        top_badge = ImageOps.fit(token_image, (74, 74), method=Image.Resampling.LANCZOS)
        badge_mask = Image.new("L", (74, 74), 0)
        badge_draw = ImageDraw.Draw(badge_mask)
        badge_draw.rounded_rectangle([0, 0, 73, 73], radius=18, fill=255)
        draw.rounded_rectangle([603, 12, 677, 86], radius=22, fill=(5, 8, 12, 220), outline=(*lime, 210), width=2)
        image.paste(top_badge, (603, 12), badge_mask)

    bot_font = load_font(31, bold=True)
    pnl_label = get_pnl_bot_label()
    bot_font = fit_text(draw, pnl_label, bot_font, 440, min_size=22, bold=True)
    draw.text((74, 54), pnl_label, font=bot_font, fill=(255, 226, 116, 255), stroke_width=3, stroke_fill=(0, 0, 0, 190))

    character_art = ImageOps.fit(character_image, (565, 565), method=Image.Resampling.LANCZOS)
    mask = Image.new("L", (565, 565), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle([0, 0, 564, 564], radius=42, fill=255)
    shadow = Image.new("RGBA", (630, 630), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.rounded_rectangle([34, 42, 600, 608], radius=54, fill=(0, 0, 0, 175))
    shadow = shadow.filter(ImageFilter.GaussianBlur(18))
    image.alpha_composite(shadow, (16, 92))
    draw.rounded_rectangle([54, 118, 619, 683], radius=46, fill=(255, 255, 255, 214))
    draw.rounded_rectangle([62, 126, 611, 675], radius=38, fill=(*accent, 235))
    image.paste(character_art, (62, 126), mask)

    if token_image:
        badge = ImageOps.fit(token_image, (96, 96), method=Image.Resampling.LANCZOS)
        badge_mask = Image.new("L", (96, 96), 0)
        badge_draw = ImageDraw.Draw(badge_mask)
        badge_draw.ellipse([0, 0, 95, 95], fill=255)
        draw.ellipse([538, 560, 654, 676], fill=(255, 255, 255, 240))
        draw.ellipse([544, 566, 648, 670], fill=(5, 8, 12, 245))
        image.paste(badge, (548, 570), badge_mask)

    title = metrics["symbol"]
    title_font = fit_text(draw, title, load_font(96, bold=True), 520, min_size=58, bold=True)
    draw.text((700, 120), title, font=title_font, fill=(255, 255, 255, 255), stroke_width=5, stroke_fill=(0, 0, 0, 210))

    called_text = f"called at {format_usd(metrics['drop_market_cap'])}"
    called_font = fit_text(draw, called_text, load_font(46, bold=True), 500, min_size=30, bold=True)
    draw.text((705, 220), called_text, font=called_font, fill=(245, 245, 245, 255), stroke_width=3, stroke_fill=(0, 0, 0, 180))

    mult = metrics["multiplier"] or 0
    mult_color = green if mult >= 1 else red
    gain_text = metrics["multiplier_text"].upper()
    gain_font = fit_text(draw, gain_text, load_font(170, bold=True), 500, min_size=96, bold=True)
    draw.text((705, 295), gain_text, font=gain_font, fill=(*mult_color, 255), stroke_width=8, stroke_fill=(0, 75, 0, 220))

    percent_text = metrics["percent_text"]
    pct_font = fit_text(draw, percent_text, load_font(42, bold=True), 290, min_size=28, bold=True)
    draw.text((1020, 402), percent_text, font=pct_font, fill=(*lime, 255), stroke_width=3, stroke_fill=(0, 0, 0, 190))

    reached_text = f"Reached {format_usd(metrics['market_cap'])}"
    reached_font = fit_text(draw, reached_text, load_font(48, bold=True), 500, min_size=30, bold=True)
    draw.text((705, 508), reached_text, font=reached_font, fill=(255, 255, 255, 255), stroke_width=4, stroke_fill=(0, 0, 0, 205))

    caller = str(metrics["caller"])
    if caller and not caller.startswith("@") and caller != "group call":
        caller = f"@{caller}"
    meta_font = fit_text(draw, caller, load_font(42, bold=True), 360, min_size=28, bold=True)
    draw.text((792, 588), caller, font=meta_font, fill=(255, 255, 255, 255), stroke_width=3, stroke_fill=(0, 0, 0, 190))
    draw.ellipse([718, 594, 750, 626], fill=(255, 255, 255, 240))
    draw.rectangle([708, 628, 760, 655], fill=(255, 255, 255, 240))

    time_text = metrics["dropped_age"]
    draw.ellipse([742, 648, 774, 680], outline=(255, 255, 255, 240), width=5)
    draw.line([758, 664, 758, 654], fill=(255, 255, 255, 240), width=4)
    draw.line([758, 664, 768, 670], fill=(255, 255, 255, 240), width=4)
    draw.text((790, 642), time_text, font=load_font(34, bold=True), fill=(255, 255, 255, 255), stroke_width=3, stroke_fill=(0, 0, 0, 180))

    image = image.convert("RGB").filter(ImageFilter.UnsharpMask(radius=1, percent=115, threshold=3))
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    output.seek(0)
    output.name = "group-ca-pnl.png"
    return output


def build_token_detail_reply(query: dict) -> dict | None:
    try:
        chain_id = query_chain_id(query)
        pairs = lookup_pairs(query)
        pair = pick_best_pair(pairs, query)
        if not pair:
            return None

        token = token_for_query(pair, query)
        token_address = str(token.get("address") or "")
        if not token_address:
            return None

        orders = fetch_dex_orders(token_address, chain_id)
        return {
            "chain_id": chain_id,
            "token_address": token_address,
            "query_type": query["type"],
            "price_usd": pair.get("priceUsd"),
            "market_cap": safe_float(pair.get("marketCap")) or safe_float(pair.get("fdv")),
            "symbol": token.get("symbol"),
            "name": token.get("name"),
            "text": format_token_details(pair, token, orders, chain_id),
        }
    except Exception as e:
        logger.error(f"Token detail lookup failed for {query}: {e}")
        return None


def build_pnl_reply(address: str, chat_id: int, thread_id: int | None) -> dict | None:
    query = {"type": "address", "value": address, "chain_id": "base"}
    try:
        drop_record = find_ca_drop_record(chat_id, thread_id, "base", address)
        if not drop_record:
            return None

        pairs = lookup_pairs(query)
        pair = pick_best_pair(pairs, query)
        if not pair:
            return None

        token = token_for_query(pair, query)
        token_address = str(token.get("address") or "")
        if not token_address:
            return None

        text = format_pnl_details(pair, token, "base", drop_record)
        image = None
        try:
            image = generate_pnl_image(pair, token, "base", drop_record)
        except Exception as e:
            logger.info(f"PnL image generation unavailable for {address}: {e}")

        metrics = build_pnl_metrics(pair, token, "base", drop_record)
        caption = (
            f"${html.escape(metrics['symbol'])} since group CA drop: "
            f"{metrics['multiplier_text']} [{metrics['percent_text']}]\n"
            f"<code>{html.escape(metrics['token_address'])}</code>"
        )
        return {"text": text, "image": image, "caption": caption}
    except Exception as e:
        logger.error(f"PnL lookup failed for {address}: {e}")
        return None


async def maybe_reply_token_details(message, text: str):
    queries = extract_token_queries(text)
    if not queries:
        return

    # Slice upfront before launching any network calls
    if TOKEN_MAX_REPLIES_PER_MESSAGE > 0:
        queries = queries[:TOKEN_MAX_REPLIES_PER_MESSAGE]

    # Filter out already-cached ticker queries before hitting DexScreener
    active_queries = []
    for query in queries:
        chain_id = query_chain_id(query)
        query_key = token_cache_key(message, f"query:{chain_id}:{query['type']}:{query['value'].lower()}")
        is_ca = query["type"] == "address"
        if not is_ca and token_cache_active(query_key):
            continue
        active_queries.append((query, query_key, is_ca))

    if not active_queries:
        return

    # Fetch all results concurrently instead of serially
    results = await asyncio.gather(
        *[asyncio.to_thread(build_token_detail_reply, q) for q, _, _ in active_queries],
        return_exceptions=True,
    )

    for (query, query_key, is_ca), result in zip(active_queries, results):
        if isinstance(result, Exception) or result is None:
            mark_token_cache(query_key, TOKEN_LOOKUP_MISS_CACHE_SECONDS)
            continue

        if is_ca:
            save_ca_drop_if_new(message, result)

        token_key = token_cache_key(message, f"token:{result['chain_id']}:{result['token_address'].lower()}")
        if not is_ca and token_cache_active(token_key):
            mark_token_cache(query_key, TOKEN_REPLY_CACHE_SECONDS)
            continue

        try:
            await message.reply_text(
                result["text"],
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            if not is_ca:
                mark_token_cache(query_key, TOKEN_REPLY_CACHE_SECONDS)
                mark_token_cache(token_key, TOKEN_REPLY_CACHE_SECONDS)
        except Exception as e:
            logger.error(f"Token detail reply failed: {e}")


# --- Gemini moderation ---
def has_strong_alpha_signal(text: str) -> bool:
    return bool(STRONG_ALPHA_RE.search(text) or ANNOUNCEMENT_RE.search(text))

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
        "- announcements, official notices, reminders, schedules, AMAs, spaces, launches, listings, claims, migrations, maintenance, or rule updates\n"
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
    try:
        if not update.effective_user:
            return False
        member = await update.effective_chat.get_member(update.effective_user.id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False

async def is_trusted(update: Update) -> bool:
    return update.effective_user.id in get_trusted_users(update.effective_chat.id)


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


async def cmd_setca(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text("Admins only.")
        return

    _config["ca_replies_enabled"] = True
    _config["ca_chat_id"] = update.message.chat_id
    _config["ca_thread_id"] = update.message.message_thread_id
    save_config()

    await update.message.reply_text("Done. CA detail replies are enabled here.")
    logger.info(
        f"CA replies set: chat={update.message.chat_id}, thread={update.message.message_thread_id}"
    )


async def cmd_clearca(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text("Admins only.")
        return

    _config["ca_replies_enabled"] = False
    _config.pop("ca_chat_id", None)
    _config.pop("ca_thread_id", None)
    save_config()

    await update.message.reply_text("CA detail replies disabled.")


async def cmd_setpnllabel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text("Admins only.")
        return

    label = " ".join(context.args or []).strip()
    if not label:
        await update.message.reply_text("Usage: /setpnllabel @YourBotName")
        return

    _config["pnl_bot_label"] = label[:40]
    save_config()
    await update.message.reply_text(f"PnL card label set to: {_config['pnl_bot_label']}")


async def cmd_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return

    source_text = " ".join(context.args or [])
    if not source_text and message.reply_to_message:
        source_text = message_text(message.reply_to_message)

    match = EVM_ADDRESS_RE.search(source_text)
    if not match:
        await message.reply_text(
            "Usage: /pnl 0xBaseContractAddress\n"
            "You can also reply to a Base CA or token card with /pnl."
        )
        return

    address = match.group(0)
    reply = await asyncio.to_thread(
        build_pnl_reply,
        address,
        message.chat_id,
        message.message_thread_id,
    )
    if not reply:
        await message.reply_text(
            "No group CA drop record found for that contract yet.\n"
            "Post the Base CA in the enabled group/thread first, then use /pnl."
        )
        return

    if reply.get("image"):
        await message.reply_photo(
            photo=reply["image"],
            caption=reply["caption"],
            parse_mode="HTML",
        )
        return

    await message.reply_text(reply["text"], parse_mode="HTML", disable_web_page_preview=True)


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
    in_alpha_thread = (
        alpha_chat_id is not None
        and message.chat_id == alpha_chat_id
        and message.message_thread_id == alpha_thread_id
    )

    ca_enabled, ca_chat_id, ca_thread_id = get_ca_reply_config()
    if ca_enabled and ca_chat_id is not None:
        in_ca_reply_thread = message.chat_id == ca_chat_id and message.message_thread_id == ca_thread_id
    else:
        in_ca_reply_thread = ca_enabled and in_alpha_thread

    if not in_alpha_thread and not in_ca_reply_thread:
        return

    user = message.from_user
    if not user:
        return

    # Record this user so the alarm fire can mention everyone.
    # Persist to config only on first encounter to avoid a write-per-message.
    already_seen = user.id in chat_seen_users[message.chat_id]
    if user.username:
        chat_seen_users[message.chat_id][user.id] = f"@{html.escape(user.username)}"
    else:
        chat_seen_users[message.chat_id][user.id] = f'<a href="tg://user?id={user.id}">{html.escape(user.first_name)}</a>'

    if not already_seen:
        seen_store = _config.setdefault("seen_users", {})
        chat_key = str(message.chat_id)
        seen_store.setdefault(chat_key, {})[str(user.id)] = chat_seen_users[message.chat_id][user.id]
        save_config()

    text = message_text(message)

    # Media-only messages are usually charts/screenshots and are kept.
    if not text:
        if in_alpha_thread:
            add_to_buffer(message, "[media-only message]", deleted=False)
        return

    # Admins often use alpha threads for announcements and pinned-style updates.
    if await is_admin(update):
        if in_alpha_thread:
            add_to_buffer(message, text, deleted=False)
        if in_ca_reply_thread:
            await maybe_reply_token_details(message, text)
        return

    if in_alpha_thread:
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

    if in_ca_reply_thread:
        await maybe_reply_token_details(message, text)
        return

# ---------------------------------------------------------------------------
# Alarm system
# ---------------------------------------------------------------------------

# Countdown checkpoints: send a reminder this many seconds BEFORE the event
ALARM_CHECKPOINTS: list[int] = [86400, 21600, 3600, 600]  # 24h, 6h, 1h, 10min

ALARM_CHECKPOINT_META: dict[int, tuple[str, str]] = {
    86400: ("📅", "24 hours"),
    21600: ("⏳", "6 hours"),
    3600:  ("🔔", "1 hour"),
    600:   ("🚨", "10 minutes"),
}

ALARM_EVENT_EMOJIS: dict[str, str] = {
    "nft":        "🎨",
    "mint":       "🎨",
    "airdrop":    "🪂",
    "claim":      "🪂",
    "space":      "🎙️",
    "ama":        "🎙️",
    "listing":    "📈",
    "list":       "📈",
    "unlock":     "🔓",
    "vesting":    "🔓",
    "launch":     "🚀",
    "snapshot":   "📸",
    "vote":       "🗳️",
    "presale":    "💰",
    "sale":       "💰",
    "ido":        "💰",
    "burn":       "🔥",
    "migration":  "🔄",
    "migrate":    "🔄",
    "twitter":    "🐦",
    "stream":     "📡",
    "live":       "📡",
    "whitelist":  "📋",
    "wl":         "📋",
    "raffle":     "🎰",
    "reveal":     "🖼️",
    "drop":       "💧",
}

# Regex: detects a timezone or UTC offset in user text — required for absolute times
_TZ_RE = re.compile(
    r"(?:\b(UTC|GMT|EST|EDT|CST|CDT|MST|MDT|PST|PDT|CET|CEST|EET|EEST|BST|IST|"
    r"JST|KST|AEST|AEDT|NZST|NZDT|HKT|SGT|WIB|WIT|WITA|MSK|AST|NST|NDT|"
    r"ART|BRT|CLT|PET|VET|COT|ECT|BOT|UYT|PYT|GYT|SRT|TRT|EAT|WAT|CAT)\b"
    r"|[+-]\d{2}:?\d{2})",   # numeric offsets like +05:30 or -0800
    re.IGNORECASE,
)

# Regex: detects purely relative time expressions — these don't need a timezone
_RELATIVE_TIME_RE = re.compile(
    r"\bin\s+\d+\s*(second|minute|min|hour|hr|day|week|month)s?\b"
    r"|\b\d+\s*(second|minute|min|hour|hr|day|week|month)s?\s+(from\s+now|later)\b",
    re.IGNORECASE,
)

# Regex: detects absolute time indicators that DO require a timezone
_ABSOLUTE_TIME_RE = re.compile(
    r"\b(\d{1,2}(:\d{2})?\s*(am|pm)|noon|midnight|midday"
    r"|(\d{1,2}:\d{2})"                         # 15:00 style
    r"|(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+\d{1,2}"  # May 20
    r"|\d{1,2}[/\-]\d{1,2}([/\-]\d{2,4})?"      # 05/20, 5-20-2025
    r"|tomorrow|tonight|next\s+\w+"              # tomorrow, tonight, next Friday
    r"|(monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b",
    re.IGNORECASE,
)


def check_timezone_requirement(text: str) -> tuple[bool, str | None]:
    """Check whether the alarm text needs a timezone and has one.

    Returns (ok, error_message_or_None).
    - If purely relative ('in 2 hours') → ok=True, no timezone needed.
    - If absolute time detected without a timezone → ok=False with a helpful message.
    - If timezone present → ok=True.
    """
    has_tz = bool(_TZ_RE.search(text))
    if has_tz:
        return True, None

    is_relative = bool(_RELATIVE_TIME_RE.search(text))
    if is_relative:
        return True, None  # relative times are unambiguous without TZ

    has_absolute = bool(_ABSOLUTE_TIME_RE.search(text))
    if has_absolute:
        return False, (
            "⚠️ <b>Please include a timezone</b> so I know exactly when to fire the alarm.\n\n"
            "Add <b>UTC</b> or <b>GMT</b> (recommended), or your local zone:\n"
            "<code>UTC · GMT · EST · PST · CET · IST · JST · +05:30</code>\n\n"
            "Examples:\n"
            "  <code>/alarm BAYC mint tomorrow 3pm UTC</code>\n"
            "  <code>/alarm space tonight 8pm EST</code>\n"
            "  <code>/alarm listing May 20 at noon GMT</code>"
        )

    # No time detected at all — let Gemini try, it will fail gracefully
    return True, None


def alarm_emoji(event_label: str) -> str:
    words = re.split(r"[\s_/$]+", event_label.lower())
    for word in words:
        if word in ALARM_EVENT_EMOJIS:
            return ALARM_EVENT_EMOJIS[word]
    return "⏰"


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m}m" + (f" {s}s" if s else "")
    if seconds < 86400:
        h, rem = divmod(seconds, 3600)
        m = rem // 60
        return f"{h}h" + (f" {m}m" if m else "")
    d, rem = divmod(seconds, 86400)
    h = rem // 3600
    return f"{d}d" + (f" {h}h" if h else "")


# --- Alarm storage helpers ---

def get_alarms() -> dict:
    return _config.setdefault("alarms", {})


def save_alarm(alarm: dict):
    get_alarms()[alarm["id"]] = alarm
    save_config()


def delete_alarm(alarm_id: str):
    get_alarms().pop(alarm_id, None)
    save_config()


def list_chat_alarms(chat_id: int) -> list[dict]:
    now = time.time()
    return sorted(
        [a for a in get_alarms().values()
         if a["chat_id"] == chat_id and a["fire_at"] > now],
        key=lambda a: a["fire_at"],
    )


# --- Gemini NLP alarm parser ---

def _normalise_alarm_input(text: str) -> str:
    """Pre-process user input before sending to Gemini.

    Fixes common ambiguous patterns that confuse the model:
    - '13:00pm' → '13:00'  (13 is already PM in 24h; trailing 'pm' is nonsense)
    - '1:00am'  → '01:00'  is fine, leave alone
    """
    # Remove 'pm'/'am' suffix when the hour is already unambiguous (13-23)
    text = re.sub(
        r"\b([1][3-9]|2[0-3])(?::(\d{2}))?\s*(?:pm|am)\b",
        lambda m: f"{m.group(1)}:{m.group(2) or '00'}",
        text,
        flags=re.IGNORECASE,
    )
    return text


def _sanity_check_parsed(raw_text: str, parsed: dict) -> bool:
    """Light sanity check: does the parsed timestamp roughly match what the user typed?

    Returns True if the result looks plausible, False if it's likely a hallucination.
    """
    fire_at = parsed.get("fire_at")
    if not fire_at:
        return False

    fire_dt = datetime.fromtimestamp(fire_at, tz=timezone.utc)

    # If the user mentioned a specific hour (e.g. "13:00" or "3pm"), verify the
    # parsed hour is within ±1 of what they wrote (accounting for AM/PM conversion).
    hour_match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", raw_text, re.IGNORECASE)
    if hour_match:
        stated_hour = int(hour_match.group(1))
        ampm = (hour_match.group(3) or "").lower()
        if ampm == "pm" and stated_hour < 12:
            stated_hour += 12
        elif ampm == "am" and stated_hour == 12:
            stated_hour = 0
        parsed_hour = fire_dt.hour
        if abs(parsed_hour - stated_hour) > 1 and abs(parsed_hour - stated_hour) < 23:
            logger.warning(
                f"Alarm sanity fail: user wrote hour={stated_hour}, "
                f"Gemini returned hour={parsed_hour} (fire_at={fire_at})"
            )
            return False

    # If the user mentioned a specific day-of-month, verify it matches.
    day_match = re.search(
        r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+(\d{1,2})\b",
        raw_text,
        re.IGNORECASE,
    )
    if not day_match:
        day_match = re.search(r"\b(\d{1,2})[/\-]\d{1,2}\b", raw_text)
    if day_match:
        stated_day = int(day_match.group(1))
        if fire_dt.day != stated_day:
            logger.warning(
                f"Alarm sanity fail: user wrote day={stated_day}, "
                f"Gemini returned day={fire_dt.day} (fire_at={fire_at})"
            )
            return False

    return True


ALARM_CONFIDENCE_THRESHOLD = float(os.getenv("ALARM_CONFIDENCE_THRESHOLD", "0.85"))


def parse_alarm_with_gemini(raw_text: str) -> dict | None:
    """Send raw natural-language text to Gemini and extract label + fire_at.

    Returns {"label": str, "fire_at": float} or None on failure.
    """
    # Pre-process: normalise ambiguous time strings before sending to Gemini
    normalised = _normalise_alarm_input(raw_text)

    now_utc = datetime.now(timezone.utc)
    now_unix = int(now_utc.timestamp())
    now_str = now_utc.strftime("%A, %Y-%m-%d %H:%M UTC")

    prompt = (
        "You are a crypto group bot parsing a user's alarm or reminder request.\n"
        f"Current time: {now_str}  (Unix: {now_unix})\n\n"
        "Extract:\n"
        "  label  — short, clear event name (max 80 chars). Keep token names, tickers, handles.\n"
        "  fire_at_unix — exact Unix timestamp (integer) when the event happens / alarm should fire.\n\n"
        "Rules:\n"
        "- If no timezone is mentioned, assume UTC.\n"
        "- 'tomorrow' = next calendar day at the stated time (or 09:00 UTC if no time given).\n"
        "- 'tonight' = today at 20:00 UTC if no time given.\n"
        "- 'in X hours/minutes/days' → add to current Unix timestamp.\n"
        "- Dates without year: use the nearest future occurrence.\n"
        "- Times are already in 24-hour format if the hour is 13 or higher.\n"
        "- confidence: 0.0–1.0 reflecting how certain you are about the parsed time.\n"
        "  Set confidence < 0.85 if ANY detail (date, hour, timezone) is ambiguous.\n\n"
        f'User text: "{normalised}"\n\n'
        "Return ONLY valid JSON — no markdown, no explanation:\n"
        '{"label":"<event name>","fire_at_unix":<integer>,"confidence":<float>,"error":null}\n\n'
        "If you cannot determine a time, return:\n"
        '{"label":null,"fire_at_unix":null,"confidence":0.0,"error":"<short reason>"}'
    )

    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": 160,
            "temperature": 0,
            "responseMimeType": "application/json",
        },
    }).encode("utf-8")

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
            result = parse_gemini_json(answer)
    except Exception as e:
        logger.error(f"Gemini alarm parse failed: {e}")
        return None

    confidence = safe_float(result.get("confidence")) or 0.0
    fire_at_raw = result.get("fire_at_unix")
    label = result.get("label")

    if not label or fire_at_raw is None or confidence < ALARM_CONFIDENCE_THRESHOLD:
        logger.info(
            f"Alarm parse low-confidence or missing: label={label!r} "
            f"fire_at={fire_at_raw} confidence={confidence:.2f} "
            f"error={result.get('error')!r}"
        )
        return None

    fire_at = float(fire_at_raw)
    if fire_at <= time.time():
        return None

    parsed = {"label": str(label)[:200].strip(), "fire_at": fire_at, "confidence": confidence}

    # Post-parse sanity check: does the result roughly match what the user typed?
    if not _sanity_check_parsed(raw_text, parsed):
        logger.warning(
            f"Alarm sanity check failed for input={raw_text!r}, "
            f"parsed fire_at={fire_at} ({datetime.fromtimestamp(fire_at, tz=timezone.utc).isoformat()})"
        )
        return None

    return parsed


# --- Job callbacks ---

async def cleanup_token_cache(context: ContextTypes.DEFAULT_TYPE):
    """Purge expired entries from token_reply_cache to prevent unbounded growth."""
    now = time.time()
    stale_keys = [k for k, expiry in list(token_reply_cache.items()) if expiry <= now]
    for k in stale_keys:
        token_reply_cache.pop(k, None)
    if stale_keys:
        logger.debug(f"Purged {len(stale_keys)} stale token cache entries.")
    """Sends a pre-event reminder at a checkpoint (24h / 6h / 1h / 10min before)."""
    data = context.job.data
    alarm_id = data["alarm_id"]
    seconds_before = data["seconds_before"]

    if alarm_id not in get_alarms():
        return  # alarm was cancelled

    alarm = get_alarms()[alarm_id]
    chat_id = alarm["chat_id"]
    thread_id = alarm.get("thread_id")
    label = html.escape(alarm.get("label", "Event"))
    evt_emoji = alarm_emoji(alarm.get("label", ""))
    cp_emoji, cp_label = ALARM_CHECKPOINT_META.get(seconds_before, ("⏰", format_duration(seconds_before)))

    fire_dt = datetime.fromtimestamp(alarm["fire_at"], tz=timezone.utc)
    fire_str = fire_dt.strftime("%H:%M UTC")

    # At 10 minutes: send mention batches first, then the countdown message
    if seconds_before == 600:
        seen = list((chat_seen_users.get(chat_id) or {}).values())
        batch: list[str] = []
        char_count = 0
        BATCH_CHAR_LIMIT = 3800
        BATCH_SIZE_LIMIT = 25

        async def flush_batch(b: list[str]):
            if not b:
                return
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    message_thread_id=thread_id,
                    text="👥 " + " ".join(b),
                    parse_mode="HTML",
                )
                await asyncio.sleep(0.7)
            except Exception as e:
                logger.error(f"Mention batch failed (alarm={alarm_id}): {e}")

        for mention in seen:
            if len(batch) >= BATCH_SIZE_LIMIT or char_count + len(mention) > BATCH_CHAR_LIMIT:
                await flush_batch(batch)
                batch = []
                char_count = 0
            batch.append(mention)
            char_count += len(mention) + 1

        await flush_batch(batch)

    text = (
        f"{cp_emoji} <b>{cp_label} until:</b> {evt_emoji} <b>{label}</b>\n"
        f"🕐 Fires at {fire_str} — use /alarms to see all events"
    )

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
            text=text,
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Countdown notification failed (alarm={alarm_id}, before={seconds_before}s): {e}")


_ALLOWED_HTML_TAGS = re.compile(r"</?(?:b|i|u|s|code|pre|tg-spoiler)>", re.IGNORECASE)


def sanitise_gemini_html(text: str) -> str:
    """Strip any HTML tags Telegram doesn't support while keeping the safe ones.

    Gemini at temperature=0.9 may produce stray '<', '&', or unrecognised tags.
    Strategy: escape the whole string, then un-escape only the allowed tags.
    """
    escaped = html.escape(text)
    # Un-escape allowed open/close tags, e.g. &lt;b&gt; → <b>
    escaped = re.sub(
        r"&lt;(/?(?:b|i|u|s|code|pre|tg-spoiler))&gt;",
        r"<\1>",
        escaped,
        flags=re.IGNORECASE,
    )
    return escaped


def generate_alarm_fire_message(label: str, creator_mention: str, emoji: str) -> str:
    """Ask Gemini to write a hype fire message for this specific event.

    Falls back to the hardcoded message if Gemini fails or times out.
    """
    fallback = (
        f"🔥 {emoji} <b>IT'S TIME!</b> {emoji} 🔥\n\n"
        f"<b>{html.escape(label)}</b>\n\n"
        f"⚡ Set by {creator_mention}"
    )

    prompt = (
        "You are the notification bot for a crypto alpha Telegram group.\n"
        "An event is happening RIGHT NOW. Write a short, high-energy alert message for the group.\n\n"
        "Rules:\n"
        "- 2 to 4 lines max. No fluff, no filler.\n"
        "- Match the energy to the event: a mint is hype, an AMA is informative, a listing is urgent.\n"
        "- Always include the event name bold using HTML: <b>event name</b>\n"
        "- End with one line: ⚡ Set by {creator}\n"
        "- Use relevant emojis but don't overdo it.\n"
        "- Output plain HTML-safe Telegram message text only. No JSON, no markdown, no code blocks.\n\n"
        f"Event: {label}\n"
        f"Creator: {creator_mention}\n"
    )

    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": 120,
            "temperature": 0.9,
        },
    }).encode("utf-8")

    req = urllib.request.Request(
        GEMINI_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=GEMINI_TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read())
            text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            if text:
                return sanitise_gemini_html(text)
    except Exception as e:
        logger.warning(f"Gemini fire message failed for '{label}': {e}")

    return fallback


async def fire_alarm(context: ContextTypes.DEFAULT_TYPE):
    """Fires the main alarm — IT'S TIME message."""
    alarm = context.job.data
    alarm_id = alarm["id"]

    if alarm_id not in get_alarms():
        return

    delete_alarm(alarm_id)

    chat_id = alarm["chat_id"]
    thread_id = alarm.get("thread_id")
    raw_label = alarm.get("label", "Event")
    emoji = alarm_emoji(raw_label)
    creator = alarm.get("created_by") or "the group"
    creator_mention = f"@{html.escape(creator)}" if creator != "the group" else creator

    main_text = await asyncio.to_thread(
        generate_alarm_fire_message, raw_label, creator_mention, emoji
    )

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
            text=main_text,
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Alarm fire failed (id={alarm_id}): {e}")


# --- Scheduling helpers ---

def schedule_alarm(alarm: dict, app):
    """Register the main alarm job + all applicable countdown jobs."""
    now = time.time()
    fire_at = alarm["fire_at"]
    alarm_id = alarm["id"]

    # Countdown notifications — only schedule those that are still in the future
    for seconds_before in ALARM_CHECKPOINTS:
        trigger_at = fire_at - seconds_before
        delay = trigger_at - now
        if delay > 5:
            app.job_queue.run_once(
                fire_countdown,
                when=delay,
                data={"alarm_id": alarm_id, "seconds_before": seconds_before},
                name=f"alarm:{alarm_id}:cp:{seconds_before}",
            )

    # Main fire job
    app.job_queue.run_once(
        fire_alarm,
        when=max(0.0, fire_at - now),
        data=alarm,
        name=f"alarm:{alarm_id}:fire",
    )


def cancel_alarm_jobs(alarm_id: str, app):
    """Remove all PTB jobs associated with an alarm."""
    job_names = [f"alarm:{alarm_id}:fire"] + [
        f"alarm:{alarm_id}:cp:{cp}" for cp in ALARM_CHECKPOINTS
    ]
    for name in job_names:
        for job in app.job_queue.get_jobs_by_name(name):
            job.schedule_removal()


async def _create_and_confirm_alarm(
    message,
    application,
    label: str,
    fire_at: float,
    created_by: str,
    *,
    natural: bool = False,
    created_by_id: int | None = None,
) -> None:
    """Persist + schedule an alarm and reply with a confirmation."""
    alarm_id = str(uuid.uuid4())[:8]
    alarm = {
        "id": alarm_id,
        "chat_id": message.chat_id,
        "thread_id": message.message_thread_id,
        "label": label,
        "fire_at": fire_at,
        "created_by": created_by,
        "created_by_id": created_by_id,  # always compare by ID, not username
        "created_at": time.time(),
    }
    save_alarm(alarm)
    schedule_alarm(alarm, application)

    emoji = alarm_emoji(label)
    fire_dt = datetime.fromtimestamp(fire_at, tz=timezone.utc)
    fire_str = fire_dt.strftime("%A, %b %d %Y at %H:%M UTC")
    seconds_until = max(0, int(fire_at - time.time()))

    now = time.time()
    active_cps = [cp for cp in ALARM_CHECKPOINTS if fire_at - cp - now > 5]
    cp_labels = [ALARM_CHECKPOINT_META[cp][1] for cp in active_cps]
    notify_line = f"🔔 Reminders: {', '.join(cp_labels)} before" if cp_labels else ""

    intro = "Got it! Alarm set 👌" if natural else f"{emoji} Alarm set!"
    reply = (
        f"{emoji} <b>{intro}</b>\n\n"
        f"📌 <b>{html.escape(label)}</b>\n"
        f"🕐 <b>{fire_str}</b>\n"
        f"⏱ In <b>{format_duration(seconds_until)}</b>\n"
    )
    if notify_line:
        reply += f"{notify_line}\n"
    reply += f"\nID: <code>{alarm_id}</code>"

    await message.reply_text(reply, parse_mode="HTML")


# --- Trusted user commands ---

async def cmd_addtrusted(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text("Admins only.")
        return

    message = update.message
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.reply_text("Reply to the user's message with /addtrusted.")
        return

    target = message.reply_to_message.from_user
    add_trusted_user(message.chat_id, target.id)
    name = f"@{target.username}" if target.username else target.first_name
    await message.reply_text(f"✅ {html.escape(name)} added to trusted. They can now set alarms.", parse_mode="HTML")


async def cmd_removetrusted(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text("Admins only.")
        return

    message = update.message
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.reply_text("Reply to the user's message with /removetrusted.")
        return

    target = message.reply_to_message.from_user
    remove_trusted_user(message.chat_id, target.id)
    name = f"@{target.username}" if target.username else target.first_name
    await message.reply_text(f"✅ {html.escape(name)} removed from trusted.", parse_mode="HTML")


async def cmd_listtrusted(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text("Admins only.")
        return

    trusted = get_trusted_users(update.message.chat_id)
    if not trusted:
        await update.message.reply_text("No trusted users for this chat.")
        return

    lines = ["<b>Trusted Users</b>"]
    for uid in trusted:
        try:
            member = await update.effective_chat.get_member(uid)
            user = member.user
            name = f"@{user.username}" if user.username else html.escape(user.first_name)
        except Exception:
            name = str(uid)
        lines.append(f"• {name} — <code>{uid}</code>")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# --- Commands ---

async def cmd_alarm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set an alarm — accepts fully natural language, no fixed format needed.

    Examples (all work):
      /alarm BAYC mint tomorrow 3pm UTC
      /alarm airdrop claim in 2 hours
      /alarm $TOKEN listing on Binance May 20 at noon
      /alarm CryptoAlpha Twitter space tonight 8pm EST
      /alarm snapshot in 30 minutes
    """
    message = update.message
    if not message:
        return

    if not await is_admin(update) and not await is_trusted(update):
        await message.reply_text("Only admins and trusted users can set alarms.")
        return

    raw = " ".join(context.args or []).strip()
    if not raw:
        await message.reply_text(
            "⏰ <b>Set an Alarm — just describe it naturally!</b>\n\n"
            "Examples:\n"
            "  /alarm BAYC mint tomorrow 3pm <b>UTC</b>\n"
            "  /alarm airdrop claim in 2 hours\n"
            "  /alarm CryptoAlpha space tonight 8pm <b>EST</b>\n"
            "  /alarm $TOKEN listing on Binance May 20 12pm <b>UTC</b>\n"
            "  /alarm snapshot in 30 minutes\n"
            "  /alarm WL raffle next Friday 6pm <b>GMT</b>\n\n"
            "📌 Include a timezone (UTC, GMT, EST…) for specific times.\n"
            "📌 Relative times like <i>in 2 hours</i> don't need one.\n\n"
            "I'll ping the group at 24h, 6h, 1h, and 10min before! 🚀",
            parse_mode="HTML",
        )
        return

    # Validate timezone before hitting Gemini
    tz_ok, tz_error = check_timezone_requirement(raw)
    if not tz_ok:
        await message.reply_text(tz_error, parse_mode="HTML")
        return

    thinking = await message.reply_text("⏳ Parsing your alarm…")
    parsed = await asyncio.to_thread(parse_alarm_with_gemini, raw)

    try:
        await thinking.delete()
    except Exception:
        pass

    if not parsed:
        await message.reply_text(
            "⚠️ I couldn't figure out the time from that.\n\n"
            "Try being a bit more specific:\n"
            "  <code>/alarm NFT mint tomorrow 3pm UTC</code>\n"
            "  <code>/alarm airdrop in 2 hours</code>\n"
            "  <code>/alarm space May 20 at 5pm UTC</code>",
            parse_mode="HTML",
        )
        return

    if parsed["fire_at"] - time.time() < 300:
        await message.reply_text("⚠️ Minimum alarm time is 5 minutes.")
        return

    if parsed["fire_at"] - time.time() > 90 * 86400:
        await message.reply_text("⚠️ Maximum alarm duration is 90 days.")
        return

    user = update.effective_user
    created_by = (user.username or user.first_name or str(user.id)) if user else "unknown"
    created_by_id = user.id if user else None
    await _create_and_confirm_alarm(
        message, context.application, parsed["label"], parsed["fire_at"],
        created_by, created_by_id=created_by_id,
    )


async def cmd_alarms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all upcoming alarms for this chat."""
    message = update.message
    if not message:
        return

    upcoming = list_chat_alarms(message.chat_id)
    if not upcoming:
        await message.reply_text("No active alarms for this chat.")
        return

    now = time.time()
    lines = ["<b>⏰ Upcoming Alarms</b>\n"]
    for alarm in upcoming:
        remaining = max(0, int(alarm["fire_at"] - now))
        emoji = alarm_emoji(alarm.get("label", ""))
        label = html.escape(alarm.get("label", "?"))
        alarm_id = alarm["id"]
        creator = html.escape(alarm.get("created_by", "?"))
        fire_dt = datetime.fromtimestamp(alarm["fire_at"], tz=timezone.utc)
        fire_str = fire_dt.strftime("%b %d, %H:%M UTC")
        lines.append(
            f"{emoji} <b>{label}</b>\n"
            f"   🕐 {fire_str} — in {format_duration(remaining)}\n"
            f"   👤 @{creator} • ID: <code>{alarm_id}</code>"
        )

    await message.reply_text("\n\n".join(lines), parse_mode="HTML")


async def cmd_cancelalarm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel an alarm by ID. Get IDs from /alarms."""
    message = update.message
    if not message:
        return

    args = context.args or []
    if not args:
        await message.reply_text("Usage: /cancelalarm <alarm_id>\nGet IDs with /alarms")
        return

    alarm_id = args[0].strip()
    alarm = get_alarms().get(alarm_id)

    if not alarm:
        await message.reply_text(
            f"⚠️ No active alarm with ID <code>{html.escape(alarm_id)}</code>.",
            parse_mode="HTML",
        )
        return

    user = update.effective_user
    creator_id = alarm.get("created_by_id")
    # Prefer ID comparison (unforgeable). Fall back to legacy username/name records.
    if creator_id is not None:
        is_creator = user is not None and user.id == creator_id
    else:
        creator = alarm.get("created_by", "")
        is_creator = user and (
            user.username == creator
            or user.first_name == creator
            or str(user.id) == creator
        )
    if not is_creator and not await is_admin(update):
        await message.reply_text("⚠️ Only admins or the alarm creator can cancel this.")
        return

    cancel_alarm_jobs(alarm_id, context.application)
    delete_alarm(alarm_id)
    label = html.escape(alarm.get("label", "?"))
    await message.reply_text(f"✅ Alarm cancelled: <b>{label}</b>", parse_mode="HTML")





# --- Startup restore ---

def restore_alarms(app):
    """Re-schedule alarms and their countdown jobs after a restart."""
    now = time.time()
    alarms = get_alarms()
    stale: list[str] = []
    restored = 0

    for alarm_id, alarm in list(alarms.items()):
        if alarm["fire_at"] <= now:
            stale.append(alarm_id)
        else:
            schedule_alarm(alarm, app)
            restored += 1

    for alarm_id in stale:
        alarms.pop(alarm_id)
    if stale:
        save_config()

    if restored:
        logger.info(f"Restored {restored} pending alarm(s) from config.")
    if stale:
        logger.info(f"Purged {len(stale)} expired alarm(s) from config.")


def main():
    load_config()

    async def post_init(app):
        restore_alarms(app)
        # Purge expired token cache entries every 30 minutes
        app.job_queue.run_repeating(cleanup_token_cache, interval=1800, first=1800)

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )
    # Block all commands and messages in private DMs
    GROUP_FILTER = filters.ChatType.GROUPS | filters.ChatType.CHANNEL

    app.add_handler(CommandHandler("setalpha", cmd_setalpha, filters=GROUP_FILTER))
    app.add_handler(CommandHandler("clearalpha", cmd_clearalpha, filters=GROUP_FILTER))
    app.add_handler(CommandHandler("setnotify", cmd_setnotify, filters=GROUP_FILTER))
    app.add_handler(CommandHandler("clearnotify", cmd_clearnotify, filters=GROUP_FILTER))
    app.add_handler(CommandHandler("setca", cmd_setca, filters=GROUP_FILTER))
    app.add_handler(CommandHandler("clearca", cmd_clearca, filters=GROUP_FILTER))
    app.add_handler(CommandHandler("setpnllabel", cmd_setpnllabel, filters=GROUP_FILTER))
    app.add_handler(CommandHandler("pnl", cmd_pnl, filters=GROUP_FILTER))
    # Alarm commands
    app.add_handler(CommandHandler("alarm", cmd_alarm, filters=GROUP_FILTER))
    app.add_handler(CommandHandler("alarms", cmd_alarms, filters=GROUP_FILTER))
    app.add_handler(CommandHandler("cancelalarm", cmd_cancelalarm, filters=GROUP_FILTER))
    app.add_handler(CommandHandler("addtrusted", cmd_addtrusted, filters=GROUP_FILTER))
    app.add_handler(CommandHandler("removetrusted", cmd_removetrusted, filters=GROUP_FILTER))
    app.add_handler(CommandHandler("listtrusted", cmd_listtrusted, filters=GROUP_FILTER))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND & GROUP_FILTER, handle_message))
    logger.info("Alpha Guard is running.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
