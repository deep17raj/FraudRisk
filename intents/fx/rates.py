"""US-dollar exchange-rate tables from two keyless public sources.

  * Frankfurter (European Central Bank reference rates, about 30 major currencies) is preferred;
  * open.er-api.com covers about 160 currencies and fills every currency the ECB does not publish.

A table maps a currency code to "how many units of it one US dollar buys". A table that cannot be
downloaded is simply missing; the caller gets `None` for a currency neither source knows.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Optional

from intentkit.upstream import Upstream

FRANKFURTER = "https://api.frankfurter.dev/v1/latest"
ER_API = "https://open.er-api.com/v6/latest/USD"

#: Words callers use instead of an ISO code. Used only to understand the question.
NAMES = {
    "euro": "EUR", "euros": "EUR", "canadian dollar": "CAD", "canadian dollars": "CAD", "swedish krona": "SEK",
    "swedish kronor": "SEK", "krona": "SEK", "british pound": "GBP", "pound sterling": "GBP", "pound": "GBP",
    "sterling": "GBP", "japanese yen": "JPY", "yen": "JPY", "swiss franc": "CHF", "franc": "CHF",
    "australian dollar": "AUD", "new zealand dollar": "NZD", "chinese yuan": "CNY", "yuan": "CNY",
    "renminbi": "CNY", "indian rupee": "INR", "rupee": "INR", "pakistani rupee": "PKR", "mexican peso": "MXN",
    "brazilian real": "BRL", "south korean won": "KRW", "won": "KRW", "norwegian krone": "NOK",
    "danish krone": "DKK", "us dollar": "USD", "us dollars": "USD", "dollar": "USD", "dollars": "USD",
    "turkish lira": "TRY", "russian ruble": "RUB", "south african rand": "ZAR", "singapore dollar": "SGD",
    "hong kong dollar": "HKD", "uae dirham": "AED", "dirham": "AED", "saudi riyal": "SAR", "polish zloty": "PLN",
}
_CODE = re.compile(r"\b[A-Za-z]{3}\b")
_STOP = {"THE", "FOR", "AND", "NOW", "WHAT", "RATE", "FX", "PER", "TO", "IN", "OF", "IS", "MID", "USD"}


def parse_currencies(raw: str) -> list[str]:
    """Every currency code mentioned, in order ('USD to CAD' -> ['USD', 'CAD'])."""
    text = (raw or "").strip()
    low = text.lower()
    found: list[tuple[int, str]] = []
    for name in sorted(NAMES, key=len, reverse=True):
        m = re.search(rf"\b{re.escape(name)}\b", low)
        if m:
            found.append((m.start(), NAMES[name]))
    for m in _CODE.finditer(text):
        code = m.group(0).upper()
        if code == "USD" or code not in _STOP:
            found.append((m.start(), code))
    seen: list[str] = []
    for _, c in sorted(found):
        if c not in seen:
            seen.append(c)
    return seen


def parse_one(raw: str) -> Optional[str]:
    """The single non-USD currency in a request ('CAD', 'USD/CAD'); USD only if nothing else is named."""
    codes = parse_currencies(raw)
    others = [c for c in codes if c != "USD"]
    return others[0] if others else (codes[0] if codes else None)


class RateTables:
    def __init__(self, up: Upstream, ttl: float) -> None:
        self.up, self.ttl = up, ttl
        self._table: dict[str, float] = {}
        self._sources: dict[str, str] = {}
        self._at = 0.0
        self._lock = asyncio.Lock()

    async def table(self) -> tuple[dict[str, float], dict[str, str]]:
        if self._table and time.monotonic() - self._at < self.ttl:
            return self._table, self._sources
        async with self._lock:
            if self._table and time.monotonic() - self._at < self.ttl:
                return self._table, self._sources
            ecb, other = await asyncio.gather(
                self.up.get_json(FRANKFURTER, params={"base": "USD"}), self.up.get_json(ER_API))
            rates: dict[str, float] = {"USD": 1.0}
            src: dict[str, str] = {"USD": "base"}
            for blob, name in ((other.get("rates") if isinstance(other, dict) else None, "open.er-api.com"),
                               (ecb.get("rates") if isinstance(ecb, dict) else None, "ECB via Frankfurter")):
                if isinstance(blob, dict):         # ECB last, so it wins where both publish a currency
                    for code, v in blob.items():
                        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                            rates[str(code).upper()] = float(v)
                            src[str(code).upper()] = name
            if len(rates) > 1:
                self._table, self._sources, self._at = rates, src, time.monotonic()
            return rates, src

    async def per_usd(self, code: str) -> Optional[float]:
        rates, _ = await self.table()
        return rates.get(code.upper())
