"""FX pipelines: the USD/<currency> rate and an amount conversion, both from the same rate tables."""
from __future__ import annotations

import re
from typing import Optional

from pydantic import BaseModel, Field

from intentkit.pipeline import BasePipeline, Req, Result
from intentkit.service import Num, StrictModel

from .config import Settings
from .rates import RateTables, parse_currencies, parse_one


class FxRateResponse(StrictModel):
    """Exactly the response field of Telegraph's FX_NOW_* intents. Nothing else."""

    rate: Optional[float] = Field(default=None, ge=0.0001, le=100000, allow_inf_nan=False)


class ConvertResponse(StrictModel):
    """Exactly the response field of Telegraph's CURRENCY_EXCHANGE_CONVERT intent. Nothing else."""

    converted_amount: Num = None


class _FxBase(BasePipeline):
    def __init__(self, settings: Settings, client) -> None:
        super().__init__(settings, client)
        self.tables = RateTables(self.up, settings.table_ttl)


class FxNowPipeline(_FxBase):
    """How many `currency` one US dollar buys. `default` is used when the request names none."""

    default = ""

    def __init__(self, settings: Settings, client) -> None:
        super().__init__(settings, client)
        self.intent = f"FX_NOW_{self.default}"

    def _ccy(self, req: Req) -> Optional[str]:
        return parse_one(req.get("currency", "")) or (self.default or None)

    def key(self, req: Req) -> Optional[str]:
        c = self._ccy(req)
        return f"fx:{c}" if c else None

    def empty(self, req: Req, reason: str) -> BaseModel:
        return FxRateResponse(rate=None)

    async def compute(self, req: Req) -> Result:
        ccy = self._ccy(req)
        rate = await self.tables.per_usd(ccy) if ccy else None
        if rate is None:
            return Result(self.empty(req, "unknown currency or no source"), degraded=True)
        return Result(FxRateResponse(rate=round(rate, 6)))


def fx_now(code: str) -> type[FxNowPipeline]:
    return type(f"FxNow{code}Pipeline", (FxNowPipeline,), {"default": code})


class ConvertPipeline(_FxBase):
    intent = "CURRENCY_EXCHANGE_CONVERT"

    @staticmethod
    def _amount(req: Req) -> float:
        raw = req.get("amount", "")
        if not raw:                                   # a one-sentence request carries it inline
            m = re.search(r"\d[\d,]*(?:\.\d+)?", req.get("query", ""))
            raw = m.group(0) if m else ""
        try:
            v = float(re.sub(r"[,\s]", "", raw)) if raw else 1.0
        except ValueError:
            return 1.0
        return v if v == v and 0 <= v <= 1e15 else 1.0

    def _pair(self, req: Req) -> Optional[tuple[str, str]]:
        src, dst = parse_one(req.get("from_currency", "")), parse_one(req.get("to_currency", ""))
        if src is None and dst is None:
            both = parse_currencies(req.get("query", ""))
            if len(both) >= 2:
                src, dst = both[0], both[1]
        return (src, dst) if src and dst else None

    def key(self, req: Req) -> Optional[str]:
        pair = self._pair(req)
        return f"cv:{pair[0]}:{pair[1]}:{self._amount(req)}" if pair else None

    def empty(self, req: Req, reason: str) -> BaseModel:
        return ConvertResponse(converted_amount=None)

    async def compute(self, req: Req) -> Result:
        pair = self._pair(req)
        if pair is None:
            return Result(self.empty(req, "currencies not understood"), degraded=True)
        a, b = await self.tables.per_usd(pair[0]), await self.tables.per_usd(pair[1])
        if a is None or b is None:
            return Result(self.empty(req, "unknown currency or no source"), degraded=True)
        return Result(ConvertResponse(converted_amount=round(self._amount(req) * b / a, 6)))
