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


def parse_symbol(raw: str) -> Optional[str]:
    text = (raw or "").strip()
    if not text:
        return None
    low = text.lower()
    for name in sorted(NAMES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(name)}\b", low):
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
