"""Ask several public exchanges for the last trade price and answer with their consensus."""
from __future__ import annotations

import asyncio
import math
from typing import Awaitable, Callable, Optional

from pydantic import BaseModel, Field

from intentkit.pipeline import BasePipeline, Req, Result, consensus
from intentkit.service import StrictModel

from .config import Settings
from .symbols import parse_symbol

PRICE_MIN, PRICE_MAX = 1e-6, 1e7


class CryptoPriceResponse(StrictModel):
    """Exactly the response field of Telegraph's CRYPTO_PRICE_USD intent. Nothing else."""

    price_usd: Optional[float] = Field(default=None, ge=PRICE_MIN, le=PRICE_MAX, allow_inf_nan=False)


def _num(value) -> Optional[float]:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and PRICE_MIN <= v <= PRICE_MAX else None


class CryptoPricePipeline(BasePipeline):
    intent = "CRYPTO_PRICE_USD"

    def __init__(self, settings: Settings, client) -> None:
        super().__init__(settings, client)
        self.sources: dict[str, Callable[[str], Awaitable[Optional[float]]]] = {
            "coinbase": self._coinbase, "kraken": self._kraken, "binance": self._binance,
            "okx": self._okx, "bitstamp": self._bitstamp}

    # ---- sources: each returns a USD price or None, never raises -------------------------------------
    async def _coinbase(self, sym: str) -> Optional[float]:
        d = await self.up.get_json(f"https://api.coinbase.com/v2/prices/{sym}-USD/spot")
        return _num(((d or {}).get("data") or {}).get("amount")) if isinstance(d, dict) else None

    async def _kraken(self, sym: str) -> Optional[float]:
        pair = {"BTC": "XBT", "DOGE": "XDG"}.get(sym, sym)
        d = await self.up.get_json("https://api.kraken.com/0/public/Ticker", params={"pair": f"{pair}USD"})
        res = d.get("result") if isinstance(d, dict) else None
        if not isinstance(res, dict) or not res:
            return None
        row = next(iter(res.values()))
        last = row.get("c") if isinstance(row, dict) else None
        return _num(last[0]) if isinstance(last, list) and last else None

    async def _binance(self, sym: str) -> Optional[float]:
        d = await self.up.get_json("https://data-api.binance.vision/api/v3/ticker/price",
                                   params={"symbol": f"{sym}USDT"})
        return _num(d.get("price")) if isinstance(d, dict) else None

    async def _okx(self, sym: str) -> Optional[float]:
        d = await self.up.get_json("https://www.okx.com/api/v5/market/ticker", params={"instId": f"{sym}-USDT"})
        rows = d.get("data") if isinstance(d, dict) else None
        return _num(rows[0].get("last")) if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None

    async def _bitstamp(self, sym: str) -> Optional[float]:
        d = await self.up.get_json(f"https://www.bitstamp.net/api/v2/ticker/{sym.lower()}usd/")
        return _num(d.get("last")) if isinstance(d, dict) else None

    # ---- pipeline ------------------------------------------------------------------------------------
    def key(self, req: Req) -> Optional[str]:
        sym = parse_symbol(req.get("symbol", ""))
        return f"px:{sym}" if sym else None

    def empty(self, req: Req, reason: str) -> BaseModel:
        return CryptoPriceResponse(price_usd=None)

    async def compute(self, req: Req) -> Result:
        s: Settings = self.settings
        sym = parse_symbol(req.get("symbol", ""))
        if not sym:
            return Result(self.empty(req, "no symbol"), degraded=True)
        loop = asyncio.get_running_loop()
        tasks = {asyncio.ensure_future(fn(sym)): name for name, fn in self.sources.items()}
        prices: dict[str, float] = {}
        pending = set(tasks)
        deadline = loop.time() + s.soft_deadline
        try:
            while pending:
                # Once one price is in, stragglers only get until the soft deadline.
                wait = None if not prices else max(0.0, deadline - loop.time())
                done, pending = await asyncio.wait(pending, timeout=wait, return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    break
                for t in done:
                    v = None if t.cancelled() or t.exception() else t.result()
                    if v is not None:
                        prices[tasks[t]] = v
        finally:
            for t in pending:
                t.cancel()
        value = consensus(list(prices.values()), s.outlier_pct / 100.0)
        if value is None:
            return Result(self.empty(req, "no exchange answered"), degraded=True)
        return Result(CryptoPriceResponse(price_usd=round(value, 8)), degraded=len(prices) < 2)
