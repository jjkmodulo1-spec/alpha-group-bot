import asyncio
import html
import json
import logging
import os
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
TOKEN_MAX_REPLIES_PER_MESSAGE = int(os.getenv("TOKEN_MAX_REPLIES_PER_MESSAGE", "1"))
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


def extract_token_queries(text: str) -> list[dict]:
    if not TOKEN_DETAILS_ENABLED:
        return []

    queries = []
    seen = set()

    for match in EVM_ADDRESS_RE.finditer(text):
        address = match.group(0)
        key = ("address", address.lower())
        if key not in seen:
            queries.append({"type": "address", "value": address})
            seen.add(key)

    for match in TICKER_DROP_RE.finditer(text):
        symbol = match.group(1).upper()
        if symbol in TOKEN_TICKER_BLOCKLIST:
            continue
        key = ("ticker", symbol)
        if key not in seen:
            queries.append({"type": "ticker", "value": symbol})
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


def is_target_chain(pair: dict) -> bool:
    return str(pair.get("chainId", "")).lower() == TOKEN_CHAIN_ID


def pick_best_pair(pairs: list[dict], query: dict) -> dict | None:
    chain_pairs = [pair for pair in pairs if is_target_chain(pair)]
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
    if query["type"] == "address":
        address = urllib.parse.quote(query["value"], safe="")
        url = f"https://api.dexscreener.com/token-pairs/v1/{TOKEN_CHAIN_ID}/{address}"
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


def fetch_dex_orders(token_address: str) -> dict:
    if not token_address:
        return {"paid": None, "ads": None, "cto": None}

    address = urllib.parse.quote(token_address, safe="")
    url = f"https://api.dexscreener.com/orders/v1/{TOKEN_CHAIN_ID}/{address}"
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


def format_token_details(pair: dict, token: dict, orders: dict) -> str:
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

    chart_url = pair.get("url") or f"https://dexscreener.com/{TOKEN_CHAIN_ID}/{pair.get('pairAddress', '')}"
    basescan_url = f"https://basescan.org/token/{token_address}" if token_address else None
    swap_url = f"https://app.uniswap.org/swap?chain={TOKEN_CHAIN_ID}&outputCurrency={urllib.parse.quote(token_address, safe='')}" if token_address else None
    link_parts = [
        html_link("Chart", chart_url),
        html_link("BaseScan", basescan_url),
        html_link("Swap", swap_url),
    ]
    link_line = " | ".join(part for part in link_parts if part)

    social_parts = collect_social_links(pair)
    social_line = " | ".join(social_parts) if social_parts else "None found"

    lines = [
        f"💊 {html.escape(name)} • ${html.escape(symbol)}",
        f"🕒 Age:  {format_age(pair.get('pairCreatedAt'))} [{change_label}] • 🧬 Base{cto}",
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
            "",
            f"<code>{html.escape(token_address)}</code>",
        ]
    )

    if link_line:
        lines.append(f"📊 {link_line}")
    lines.append(f"👻 Socials: {social_line}")

    return "\n".join(lines)


def build_token_detail_reply(query: dict) -> dict | None:
    try:
        pairs = lookup_pairs(query)
        pair = pick_best_pair(pairs, query)
        if not pair:
            return None

        token = token_for_query(pair, query)
        token_address = str(token.get("address") or "")
        if not token_address:
            return None

        orders = fetch_dex_orders(token_address)
        return {
            "token_address": token_address,
            "text": format_token_details(pair, token, orders),
        }
    except Exception as e:
        logger.error(f"Token detail lookup failed for {query}: {e}")
        return None


async def maybe_reply_token_details(message, text: str):
    queries = extract_token_queries(text)
    if not queries or TOKEN_MAX_REPLIES_PER_MESSAGE <= 0:
        return

    sent = 0
    for query in queries:
        if sent >= TOKEN_MAX_REPLIES_PER_MESSAGE:
            break

        query_key = token_cache_key(message, f"query:{query['type']}:{query['value'].lower()}")
        if token_cache_active(query_key):
            continue

        result = await asyncio.to_thread(build_token_detail_reply, query)
        if not result:
            mark_token_cache(query_key, TOKEN_LOOKUP_MISS_CACHE_SECONDS)
            continue

        token_key = token_cache_key(message, f"token:{TOKEN_CHAIN_ID}:{result['token_address'].lower()}")
        if token_cache_active(token_key):
            mark_token_cache(query_key, TOKEN_REPLY_CACHE_SECONDS)
            continue

        try:
            await message.reply_text(
                result["text"],
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
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

    # Admins often use alpha threads for announcements and pinned-style updates.
    if await is_admin(update):
        add_to_buffer(message, text, deleted=False)
        await maybe_reply_token_details(message, text)
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
    await maybe_reply_token_details(message, text)

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
