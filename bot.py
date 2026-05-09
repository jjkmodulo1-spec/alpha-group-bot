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
TOKEN_MAX_REPLIES_PER_MESSAGE = int(os.getenv("TOKEN_MAX_REPLIES_PER_MESSAGE", "0"))
PNL_MEME_IMAGES_ENABLED = env_flag("PNL_MEME_IMAGES_ENABLED", "1")
PNL_MEME_API_URL = os.getenv("PNL_MEME_API_URL", "https://api.imgflip.com/get_memes")
PNL_BOT_LABEL = os.getenv("PNL_BOT_LABEL", "@RickBurpBot")
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
pnl_meme_cache = {"expires": 0.0, "templates": []}


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


def meme_reaction_name(multiplier: float | None) -> str:
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


def preferred_meme_templates(reaction: str) -> list[str]:
    return {
        "rekt": [
            "Sad Pablo Escobar",
            "Hide the Pain Harold",
            "This Is Fine",
            "Disaster Girl",
            "Crying Cat",
            "Ight Imma Head Out",
            "Surprised Pikachu",
            "Waiting Skeleton",
        ],
        "flat": [
            "Waiting Skeleton",
            "Is This A Pigeon",
            "They're The Same Picture",
            "This Is Fine",
            "Monkey Puppet",
            "Change My Mind",
            "Two Buttons",
            "Ancient Aliens",
        ],
        "win": [
            "Success Kid",
            "Leonardo Dicaprio Cheers",
            "Roll Safe Think About It",
            "Doge",
            "Surprised Pikachu",
            "Epic Handshake",
            "Oprah You Get A",
            "X, X Everywhere",
        ],
        "send": [
            "Leonardo Dicaprio Cheers",
            "Success Kid",
            "Expanding Brain",
            "Doge",
            "Oprah You Get A",
            "Buff Doge vs. Cheems",
            "The Rock Driving",
            "Epic Handshake",
        ],
        "moon": [
            "Leonardo Dicaprio Cheers",
            "Expanding Brain",
            "Doge",
            "Oprah You Get A",
            "Success Kid",
            "X, X Everywhere",
            "Epic Handshake",
            "Ancient Aliens",
        ],
    }.get(reaction, ["Success Kid"])


def load_meme_templates() -> list[dict]:
    now = time.time()
    if pnl_meme_cache["templates"] and pnl_meme_cache["expires"] > now:
        return pnl_meme_cache["templates"]

    data = fetch_json_url(PNL_MEME_API_URL, timeout=TOKEN_LOOKUP_TIMEOUT_SECONDS)
    memes = ((data or {}).get("data") or {}).get("memes") or []
    pnl_meme_cache["templates"] = memes
    pnl_meme_cache["expires"] = now + 3600
    return memes


def pick_meme_template(multiplier: float | None, seed_key: str = "") -> dict:
    if not PNL_MEME_IMAGES_ENABLED:
        raise ValueError("meme images are disabled")

    reaction = meme_reaction_name(multiplier)
    templates = load_meme_templates()
    if not templates:
        raise ValueError("no meme templates returned")

    templates_by_name = {
        str(template.get("name", "")).strip().lower(): template
        for template in templates
        if template.get("url")
    }

    candidates = []
    seen_urls = set()

    def add_candidate(template):
        url = template.get("url")
        if not url or url in seen_urls:
            return
        candidates.append(template)
        seen_urls.add(url)

    for preferred in preferred_meme_templates(reaction):
        match = templates_by_name.get(preferred.lower())
        if match:
            add_candidate(match)

    for preferred in preferred_meme_templates(reaction):
        preferred_key = preferred.lower()
        for name, template in templates_by_name.items():
            if preferred_key in name or name in preferred_key:
                add_candidate(template)

    if candidates:
        rng = random.Random(f"{reaction}:{seed_key}")
        return candidates[rng.randrange(len(candidates))]

    fallback = [template for template in templates if template.get("url")]
    if fallback:
        rng = random.Random(f"fallback:{reaction}:{seed_key}")
        return fallback[rng.randrange(len(fallback))]

    raise ValueError(f"no meme template matched reaction={reaction}")


def image_average_color(image) -> tuple[int, int, int]:
    from PIL import ImageStat

    thumb = image.convert("RGB").resize((1, 1))
    color = ImageStat.Stat(thumb).mean
    return tuple(max(40, min(235, int(c))) for c in color)


def generate_pnl_image(pair: dict, token: dict, chain_id: str, drop_record: dict) -> io.BytesIO:
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

    metrics = build_pnl_metrics(pair, token, chain_id, drop_record)
    seed = f"{metrics['token_address']}:{drop_record.get('dropped_at')}"
    meme_template = pick_meme_template(metrics["multiplier"], seed)
    meme_image = fetch_image_from_url(meme_template["url"])
    token_image = None
    if metrics["image_url"]:
        try:
            token_image = fetch_token_image(metrics["image_url"])
        except Exception as e:
            logger.info(f"Token image unavailable for PnL background: {e}")

    width, height = 1280, 720
    rng = random.Random(seed)

    source_image = meme_image
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

    meme_art = ImageOps.fit(meme_image, (565, 565), method=Image.Resampling.LANCZOS)
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
    image.paste(meme_art, (62, 126), mask)

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

    sent = 0
    for query in queries:
        if TOKEN_MAX_REPLIES_PER_MESSAGE > 0 and sent >= TOKEN_MAX_REPLIES_PER_MESSAGE:
            break

        chain_id = query_chain_id(query)
        query_key = token_cache_key(message, f"query:{chain_id}:{query['type']}:{query['value'].lower()}")
        is_ca = query["type"] == "address"
        if not is_ca and token_cache_active(query_key):
            continue

        result = await asyncio.to_thread(build_token_detail_reply, query)
        if not result:
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
            sent += 1
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

def main():
    load_config()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("setalpha", cmd_setalpha))
    app.add_handler(CommandHandler("clearalpha", cmd_clearalpha))
    app.add_handler(CommandHandler("setnotify", cmd_setnotify))
    app.add_handler(CommandHandler("clearnotify", cmd_clearnotify))
    app.add_handler(CommandHandler("setca", cmd_setca))
    app.add_handler(CommandHandler("clearca", cmd_clearca))
    app.add_handler(CommandHandler("setpnllabel", cmd_setpnllabel))
    app.add_handler(CommandHandler("pnl", cmd_pnl))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    logger.info("Alpha Guard is running.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
