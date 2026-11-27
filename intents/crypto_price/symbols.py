"""Turn what a caller sent into an upper-case ticker symbol."""
from __future__ import annotations

import re
from typing import Optional

#: Common names -> ticker. Used only to understand the question, never to answer it.
NAMES = {
    "bitcoin": "BTC", "xbt": "BTC", "ethereum": "ETH", "ether": "ETH", "solana": "SOL", "ripple": "XRP",
    "binance coin": "BNB", "binancecoin": "BNB", "bnb chain": "BNB", "cardano": "ADA", "dogecoin": "DOGE",
    "xdg": "DOGE", "tether": "USDT", "usd coin": "USDC", "polkadot": "DOT", "litecoin": "LTC",
    "chainlink": "LINK", "avalanche": "AVAX", "tron": "TRX", "polygon": "POL", "matic": "POL",
    "shiba inu": "SHIB", "toncoin": "TON", "stellar": "XLM", "bitcoin cash": "BCH", "uniswap": "UNI",
    "cosmos": "ATOM", "near protocol": "NEAR", "aptos": "APT", "arbitrum": "ARB", "optimism": "OP",
    "monero": "XMR", "filecoin": "FIL", "aave": "AAVE",
}
_STOP = {"WHAT", "IS", "THE", "OF", "PRICE", "CURRENT", "IN", "US", "USD", "DOLLARS", "DOLLAR", "FOR", "AND", "A",
         "AN", "TO", "NOW", "RIGHT", "HOW", "MUCH", "COST", "COSTS", "WORTH", "VALUE", "CRYPTO", "COIN", "TOKEN",
         "SPOT", "LIVE", "LATEST", "TODAY", "PLEASE", "TELL", "ME", "QUOTE"}
_QUOTE = re.compile(r"[-/_]?(USDT|USDC|USD)$", re.I)


#: Words that make a different asset out of a name: "Ethereum Classic" is not Ethereum, "Wrapped Bitcoin" is not Bitcoin.
_QUALIFIERS = r"(?:classic|cash|gold|sv|diamond|lightning|2\.0|v2|v3|dao|name\s+service)"
_PREFIXES = r"(?:wrapped|staked|bridged|binance[\s-]peg(?:ged)?|lido\s+staked|coinbase\s+wrapped|restaked)"


def _is_variant_of(name: str, low: str) -> bool:
    n = re.escape(name)
    return bool(re.search(rf"\b{n}\s+{_QUALIFIERS}\b", low) or re.search(rf"\b{_PREFIXES}\s+{n}\b", low))


def parse_symbol(raw: str) -> Optional[str]:
    text = (raw or "").strip()
    if not text:
        return None
    low = text.lower()
    for name in sorted(NAMES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(name)}\b", low) and not _is_variant_of(name, low):
            return NAMES[name]
    words = text.split()
    if len(words) == 1:
        word = words[0].lstrip("$")
        stripped = _QUOTE.sub("", word)
        sym = (stripped if len(stripped) >= 2 else word).upper()
        return sym if re.fullmatch(r"[A-Z0-9]{1,15}", sym) else None
    for word in re.findall(r"\$?[A-Za-z0-9]{2,15}", text):
        w = word.lstrip("$").upper()
        if w not in _STOP and (word.isupper() or word.startswith("$")):
            return w
    return None


_ASKED = re.compile(r"(?:(?:price|cost|value|quote|worth)\s+(?:of|for)|how\s+much\s+is|what(?:'s|\s+is)(?:\s+the)?)\s+(?P<n>.+)$", re.I)
_TRAILING = re.compile(r"\s+(?:in\s+(?:us\s+)?(?:usd|dollars?)|right\s+now|now|today|currently|worth|price)$", re.I)
_GENERIC_SUFFIX = re.compile(r"\s+(?:coin|token|protocol|network|crypto|cryptocurrency)$", re.I)


def name_variants(name: str) -> list[str]:
    """The name as given, then without a generic word on the end ("Fetch.ai token" -> "Fetch.ai")."""
    short = _GENERIC_SUFFIX.sub("", name).strip()
    return [name] + ([short] if short and short.lower() != name.lower() else [])


def coin_name(raw: str) -> str:
    """The coin's name out of a request ("What is the price of The Graph?" -> "The Graph"); a bare name is unchanged."""
    text = re.sub(r"\s+", " ", (raw or "").replace("$", " ")).strip().rstrip("?!. ")
    m = _ASKED.search(text)
    if m:
        text = m.group("n")
    for _ in range(3):                                     # "... in USD right now" peels one tail at a time
        text = _TRAILING.sub("", text).strip()
    return re.sub(r"^(?:the\s+)?(?:current\s+|latest\s+)?price\s+of\s+", "", text, flags=re.I).strip()
