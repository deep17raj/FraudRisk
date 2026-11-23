"""TOKEN_HOLDER_COUNT_GENERAL from public block-explorer indexes (keyless).

Sources differ in how they count holders (for USDT on Ethereum Blockscout, Ethplorer and Routescan disagree by 8-10%),
so they are never averaged: one primary per chain answers, and the next is used only if it could not.
  1. Blockscout (`/api/v2/tokens/<address>`), the instance of the asked chain;
  2. Ethplorer (Ethereum only, its public `freekey`);
  3. Routescan, last, because it labels its own count `approx`.
Some Blockscout instances sit behind a Cloudflare bot challenge for some networks; that is treated as "source
unavailable" (never worked around), and the answer is `null` when no source can answer. A token given by symbol or name
is resolved on the chain's Blockscout search to the exact-symbol match that is verified and has the largest market cap,
so copycat tokens with the same ticker are not picked; an address in the request is always used as given.
"""
from __future__ import annotations

import re
from typing import Optional

from fastapi import FastAPI
from pydantic import BaseModel, Field

from intentkit.pipeline import BasePipeline, Req, Result
from intentkit.request import GENERIC_ALIASES
from intentkit.service import StrictModel, create_service_app
from intentkit.text import clip

from .config import Settings

VERSION = "0.1.0"
FIELDS = {"token": ("address", "contract", "token_address", "symbol", "name", *GENERIC_ALIASES),
          "chain": ("network", "blockchain", "chain_name")}
_ADDRESS = re.compile(r"\b0x[0-9a-fA-F]{40}\b")

#: canonical chain -> its sources. The URLs are public explorer indexes, not answers.
CHAINS: dict[str, dict] = {
    "ethereum": {"blockscout": "https://eth.blockscout.com", "ethplorer": True, "routescan": 1},
    "optimism": {"blockscout": "https://optimism.blockscout.com"},
    "base": {"blockscout": "https://base.blockscout.com"},
    "polygon": {"blockscout": "https://polygon.blockscout.com"},
    "arbitrum": {"blockscout": "https://arbitrum.blockscout.com"},
    "gnosis": {"blockscout": "https://gnosis.blockscout.com"},
    "avalanche": {"routescan": 43114},
}
ALIASES = {"eth": "ethereum", "mainnet": "ethereum", "ethereum mainnet": "ethereum", "op": "optimism", "op mainnet": "optimism",
           "optimism mainnet": "optimism", "base mainnet": "base", "matic": "polygon", "polygon pos": "polygon",
           "polygon mainnet": "polygon", "arb": "arbitrum", "arbitrum one": "arbitrum", "xdai": "gnosis", "gnosis chain": "gnosis",
           "avax": "avalanche", "avalanche c-chain": "avalanche", "c-chain": "avalanche"}


def parse_chain(raw: str) -> Optional[str]:
    t = (raw or "").strip().lower()
    if t in CHAINS:
        return t
    if t in ALIASES:
        return ALIASES[t]
    for name in sorted([*CHAINS, *ALIASES], key=len, reverse=True):
        if re.search(rf"\b{re.escape(name)}\b", t):
            return name if name in CHAINS else ALIASES[name]
    return None


class TokenHoldersResponse(StrictModel):
    """Exactly the response fields of Telegraph's TOKEN_HOLDER_COUNT_GENERAL intent. Nothing else."""

    token: str = ""
    chain: str = ""
    holders_count: Optional[int] = Field(default=None, ge=0, le=10_000_000_000)
    token_symbol: str = ""


def _count(value) -> Optional[int]:
    try:
        n = int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return None
    return n if 0 <= n <= 10_000_000_000 else None


class TokenHoldersPipeline(BasePipeline):
    intent = "TOKEN_HOLDER_COUNT_GENERAL"

    def key(self, req: Req) -> Optional[str]:
        t, c = req.get("token", "").strip().lower(), parse_chain(req.get("chain", "")) or req.get("chain", "").strip().lower()
        return f"th:{c}:{t}" if t else None

    def empty(self, req: Req, reason: str) -> BaseModel:
        return TokenHoldersResponse(token=req.get("token", "").strip()[:200],
                                    chain=(parse_chain(req.get("chain", "")) or req.get("chain", "").strip())[:60])

    # ---- sources ---------------------------------------------------------------------------------------------
    async def _blockscout(self, base: str, address: str) -> Optional[tuple[int, str]]:
        d = await self.up.get_json(f"{base}/api/v2/tokens/{address}", key=f"blockscout:{base}")
        n = _count(d.get("holders_count")) if isinstance(d, dict) else None
        return (n, str(d.get("symbol") or "")) if n is not None else None

    async def _ethplorer(self, address: str) -> Optional[tuple[int, str]]:
        d = await self.up.get_json(f"https://api.ethplorer.io/getTokenInfo/{address}", params={"apiKey": "freekey"}, key="ethplorer")
        n = _count(d.get("holdersCount")) if isinstance(d, dict) else None
        return (n, str(d.get("symbol") or "")) if n is not None else None

    async def _routescan(self, chain_id: int, address: str) -> Optional[tuple[int, str]]:
        d = await self.up.get_json(f"https://api.routescan.io/v2/network/mainnet/evm/{chain_id}/erc20/{address}/holders",
                                   params={"limit": 1, "count": "true"}, key="routescan")
        n = _count(d.get("count")) if isinstance(d, dict) else None
        return (n, "") if n is not None else None

    async def _by_name(self, base: str, text: str) -> Optional[tuple[str, int, str]]:
        """(address, holders, symbol) of a token named by symbol or name, never a look-alike.

        Blockscout's token list answers a text query with holder counts included, so one call does both jobs. Of the
        tokens whose symbol or name equals the text exactly, the verified, reputable one with the largest market cap wins.
        It is slow on some networks (5-10 s), hence the long timeout.
        """
        d = await self.up.get_json(f"{base}/api/v2/tokens", params={"q": text[:60], "type": "ERC-20"},
                                   key=f"blockscout:{base}", timeout=9.5)
        items = [i for i in ((d or {}).get("items") or []) if isinstance(d, dict) and isinstance(i, dict) and i.get("address_hash")]
        want = text.strip().lower()
        exact = [i for i in items if str(i.get("symbol", "")).lower() == want or str(i.get("name", "")).lower() == want]

        def rank(i: dict):
            try:
                cap = float(i.get("circulating_market_cap") or 0)
            except ValueError:
                cap = 0.0
            return (bool(i.get("is_verified_via_admin_panel")), i.get("reputation") == "ok", cap)
        for best in sorted(exact, key=rank, reverse=True)[:1]:
            n = _count(best.get("holders_count"))
            if n is not None:
                return best["address_hash"], n, str(best.get("symbol") or "")
        return None

    async def compute(self, req: Req) -> Result:
        token = req.get("token", "").strip()
        chain = parse_chain(req.get("chain", ""))
        if not token:
            return Result(self.empty(req, "no token"), degraded=True)
        if chain is None:
            return Result(self.empty(req, "unsupported chain"), degraded=True)
        src = CHAINS[chain]
        m = _ADDRESS.search(token)
        address = m.group(0) if m else None
        if address is None:                                   # a symbol or name: resolve it and read its count at once
            named = await self._by_name(src["blockscout"], token) if "blockscout" in src else None
            if named is None:
                return Result(self.empty(req, "token not found or no explorer answered"), degraded=True)
            return Result(TokenHoldersResponse(token=clip(token, 200), chain=chain, holders_count=named[1], token_symbol=named[2][:30]))
        answer: Optional[tuple[int, str]] = None
        if "blockscout" in src:
            answer = await self._blockscout(src["blockscout"], address)
        if answer is None and src.get("ethplorer"):
            answer = await self._ethplorer(address)
        if answer is None and "routescan" in src:
            answer = await self._routescan(src["routescan"], address)
        if answer is None:
            return Result(self.empty(req, "no explorer answered"), degraded=True)
        return Result(TokenHoldersResponse(token=clip(token, 200), chain=chain, holders_count=answer[0], token_symbol=answer[1][:30]))


def create_app(settings: Optional[Settings] = None, pipeline: Optional[TokenHoldersPipeline] = None) -> FastAPI:
    return create_service_app(
        title="Telegraph Token Holder Count Miner", intent="TOKEN_HOLDER_COUNT_GENERAL", path="/token-holders",
        version=VERSION, fields=FIELDS, model=TokenHoldersResponse, settings=settings or Settings.from_env(),
        pipeline=pipeline, make_pipeline=TokenHoldersPipeline, max_len=300)
