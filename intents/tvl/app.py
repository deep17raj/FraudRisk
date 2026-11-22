"""TVL_LOOKUP_GENERAL from DefiLlama (keyless).

Resolution order for `protocol`: a chain name (DefiLlama's chain list), then a protocol slug built from the name
(`/tvl/<slug>` answers for parent protocols such as "aave" too), then a fuzzy match over DefiLlama's full protocol
list (name, slug or symbol), which is a 10 MB download and so only fetched when the first two find nothing.
"Aave on Arbitrum" also fills `chain_tvl_usd` with the protocol's value locked on that chain.
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI
from pydantic import BaseModel, Field

from intentkit.pipeline import BasePipeline, Req, Result
from intentkit.request import GENERIC_ALIASES
from intentkit.service import StrictModel, create_service_app

from .config import Settings

VERSION = "0.1.0"
LLAMA = "https://api.llama.fi"
FIELDS = {"protocol": ("name", "chain", "pool", "project", *GENERIC_ALIASES)}
_ON = re.compile(r"\s+(?:on|in|@)\s+|\s*[(\[]\s*|\s*,\s*", re.I)
_LEAD = re.compile(r"^(?:what(?:'s| is)? the )?(?:current |total )?(?:tvl|total value locked)(?: of| for| in)?\s+", re.I)
_TAIL = re.compile(r"\s*(?:tvl|total value locked)\s*$", re.I)


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def split_request(raw: str) -> tuple[str, str]:
    """'Aave on Arbitrum' -> ('Aave', 'Arbitrum'); 'tvl of Uniswap V3' -> ('Uniswap V3', '')."""
    s = _TAIL.sub("", _LEAD.sub("", (raw or "").strip().strip("?.! ")))
    parts = [p.strip(" )]") for p in _ON.split(s, maxsplit=1)]
    return parts[0], (parts[1] if len(parts) > 1 else "")


class TvlResponse(StrictModel):
    """Exactly the response fields of Telegraph's TVL_LOOKUP_GENERAL intent. Nothing else."""

    protocol_or_chain: str = ""
    tvl_usd: Optional[float] = Field(default=None, ge=0, le=1e13, allow_inf_nan=False)
    chain_tvl_usd: Optional[float] = Field(default=None, ge=0, le=1e13, allow_inf_nan=False)
    as_of: str = ""


def _num(v) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1e13 else None


class TvlPipeline(BasePipeline):
    intent = "TVL_LOOKUP_GENERAL"

    def __init__(self, settings: Settings, client) -> None:
        super().__init__(settings, client)
        self._chains: dict[str, tuple[str, float]] = {}
        self._chains_at = 0.0
        self._protocols: list[dict] = []
        self._protocols_at = 0.0
        self._lock = asyncio.Lock()

    def key(self, req: Req) -> Optional[str]:
        n, c = split_request(req.get("protocol", ""))
        return f"tvl:{n.lower()}|{c.lower()}" if n else None

    def empty(self, req: Req, reason: str) -> BaseModel:
        return TvlResponse(protocol_or_chain=split_request(req.get("protocol", ""))[0][:200])

    async def _chain_table(self) -> dict[str, tuple[str, float]]:
        if self._chains and time.monotonic() - self._chains_at < self.settings.chains_ttl:
            return self._chains
        d = await self.up.get_json(f"{LLAMA}/v2/chains")
        if isinstance(d, list):
            table = {str(c["name"]).lower(): (str(c["name"]), float(c["tvl"])) for c in d
                     if isinstance(c, dict) and c.get("name") and _num(c.get("tvl")) is not None}
            if table:
                self._chains, self._chains_at = table, time.monotonic()
        return self._chains

    async def _protocol_list(self) -> list[dict]:
        if self._protocols and time.monotonic() - self._protocols_at < self.settings.protocols_ttl:
            return self._protocols
        async with self._lock:
            if self._protocols and time.monotonic() - self._protocols_at < self.settings.protocols_ttl:
                return self._protocols
            d = await self.up.get_json(f"{LLAMA}/protocols", timeout=30.0, key="llama-list")
            if isinstance(d, list):
                self._protocols = [{k: p.get(k) for k in ("name", "slug", "symbol", "tvl")} for p in d
                                   if isinstance(p, dict) and p.get("slug")]
                self._protocols_at = time.monotonic()
        return self._protocols

    async def _protocol_tvl(self, slug: str) -> Optional[float]:
        d = await self.up.get_json(f"{LLAMA}/tvl/{slug}")
        return _num(d)

    async def _by_chain_tvl(self, slug: str, chain: str) -> Optional[float]:
        d = await self.up.get_json(f"{LLAMA}/protocol/{slug}", timeout=15.0)
        cur = d.get("currentChainTvls") if isinstance(d, dict) else None
        if not isinstance(cur, dict):
            return None
        want = chain.lower()
        for k, v in cur.items():
            if k.lower() == want:
                return _num(v)
        return None

    async def compute(self, req: Req) -> Result:
        name, chain_hint = split_request(req.get("protocol", ""))
        if not name:
            return Result(self.empty(req, "no protocol"), degraded=True)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        chains = await self._chain_table()
        slug = slugify(name)
        tvl = await self._protocol_tvl(slug) if slug else None
        if tvl is None and name.lower() in chains:                # a chain, and not also a protocol of that name
            label, value = chains[name.lower()]
            return Result(TvlResponse(protocol_or_chain=label, tvl_usd=value, as_of=now))
        label = name
        if tvl is None:
            hit = await self._fuzzy(name)
            if hit is not None:
                slug, label, tvl = hit["slug"], hit.get("name") or name, _num(hit.get("tvl"))
                if tvl is None:
                    tvl = await self._protocol_tvl(slug)
        if tvl is None:
            return Result(self.empty(req, "protocol not found"), degraded=not self._protocols and not chains)
        chain_tvl = await self._by_chain_tvl(slug, chain_hint) if chain_hint else None
        return Result(TvlResponse(protocol_or_chain=label[:200], tvl_usd=tvl, chain_tvl_usd=chain_tvl, as_of=now))

    async def _fuzzy(self, name: str) -> Optional[dict]:
        rows = await self._protocol_list()
        q = name.lower()
        for test in (lambda p: (p.get("name") or "").lower() == q or (p.get("symbol") or "").lower() == q,
                     lambda p: (p.get("name") or "").lower().startswith(q) or (p.get("slug") or "").startswith(slugify(q))):
            found = [p for p in rows if test(p)]
            if found:
                return max(found, key=lambda p: p.get("tvl") or 0)
        return None


def create_app(settings: Optional[Settings] = None, pipeline: Optional[TvlPipeline] = None) -> FastAPI:
    return create_service_app(
        title="Telegraph TVL Lookup Miner", intent="TVL_LOOKUP_GENERAL", path="/tvl-lookup", version=VERSION,
        fields=FIELDS, model=TvlResponse, settings=settings or Settings.from_env(), pipeline=pipeline,
        make_pipeline=TvlPipeline, max_len=300)
