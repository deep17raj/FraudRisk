"""`eth_gasPrice` from public RPC nodes, and the dollar cost of a 21,000-gas transfer from Coinbase's spot price."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

from intentkit.pipeline import BasePipeline, Req, Result
from intentkit.service import StrictModel

from .config import Settings

TRANSFER_GAS = 21_000

#: canonical chain -> (public RPC endpoints in order of preference, native asset for the dollar price).
CHAINS: dict[str, tuple[tuple[str, ...], str]] = {
    "ethereum": (("https://ethereum-rpc.publicnode.com", "https://eth.llamarpc.com"), "ETH"),
    "base": (("https://base-rpc.publicnode.com", "https://mainnet.base.org"), "ETH"),
    "arbitrum": (("https://arbitrum-one-rpc.publicnode.com", "https://arb1.arbitrum.io/rpc"), "ETH"),
    "optimism": (("https://optimism-rpc.publicnode.com", "https://mainnet.optimism.io"), "ETH"),
    "polygon": (("https://polygon-bor-rpc.publicnode.com", "https://polygon-rpc.com"), "POL"),
    "bsc": (("https://bsc-rpc.publicnode.com", "https://bsc-dataseed.binance.org"), "BNB"),
    "avalanche": (("https://avalanche-c-chain-rpc.publicnode.com", "https://api.avax.network/ext/bc/C/rpc"), "AVAX"),
}
ALIASES = {
    "eth": "ethereum", "mainnet": "ethereum", "ethereum mainnet": "ethereum", "ether": "ethereum", "l1": "ethereum",
    "base": "base", "base mainnet": "base", "arb": "arbitrum", "arbitrum one": "arbitrum", "arbitrum-one": "arbitrum",
    "op": "optimism", "op mainnet": "optimism", "optimism mainnet": "optimism", "matic": "polygon",
    "polygon pos": "polygon", "polygon mainnet": "polygon", "bnb": "bsc", "bnb chain": "bsc",
    "binance smart chain": "bsc", "bnb smart chain": "bsc", "avax": "avalanche", "avalanche c-chain": "avalanche",
    "c-chain": "avalanche",
}


def parse_chain(raw: str) -> Optional[str]:
    text = (raw or "").strip().lower()
    if not text:
        return None
    if text in CHAINS:
        return text
    if text in ALIASES:
        return ALIASES[text]
    for name in sorted([*CHAINS, *ALIASES], key=len, reverse=True):
        if re.search(rf"\b{re.escape(name)}\b", text):
            return name if name in CHAINS else ALIASES[name]
    return None


class GasPriceResponse(StrictModel):
    """Exactly the response fields of Telegraph's GAS_PRICE_GENERAL intent. Nothing else."""

    chain: str = ""
    gas_price_gwei: Optional[float] = Field(default=None, ge=0, le=100000, allow_inf_nan=False)
    transfer_fee_usd: Optional[float] = Field(default=None, ge=0, le=10000, allow_inf_nan=False)
    as_of: str = ""


class GasPricePipeline(BasePipeline):
    intent = "GAS_PRICE_GENERAL"

    def key(self, req: Req) -> Optional[str]:
        raw = req.get("chain", "").strip().lower()
        return f"gas:{parse_chain(raw) or raw}" if raw else None

    def empty(self, req: Req, reason: str) -> BaseModel:
        return GasPriceResponse(chain=(parse_chain(req.get("chain", "")) or req.get("chain", "").strip())[:100])

    async def _gas_wei(self, rpcs: tuple[str, ...]) -> Optional[int]:
        body = {"jsonrpc": "2.0", "id": 1, "method": "eth_gasPrice", "params": []}
        for url in rpcs:
            d = await self.up.get_json(url, method="POST", json_body=body)
            res = d.get("result") if isinstance(d, dict) else None
            if isinstance(res, str) and re.fullmatch(r"0x[0-9a-fA-F]+", res):
                return int(res, 16)
        return None

    async def _native_usd(self, asset: str) -> Optional[float]:
        d = await self.up.get_json(f"https://api.coinbase.com/v2/prices/{asset}-USD/spot")
        try:
            v = float(d["data"]["amount"])
            return v if v > 0 else None
        except (KeyError, TypeError, ValueError):
            return None

    async def compute(self, req: Req) -> Result:
        chain = parse_chain(req.get("chain", ""))
        if chain is None:
            return Result(self.empty(req, "unsupported chain"), degraded=True)
        rpcs, asset = CHAINS[chain]
        wei = await self._gas_wei(rpcs)
        if wei is None:
            return Result(self.empty(req, "no RPC node answered"), degraded=True)
        gwei = wei / 1e9
        usd = await self._native_usd(asset)
        fee = round(gwei * 1e-9 * TRANSFER_GAS * usd, 6) if usd is not None else None
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return Result(GasPriceResponse(chain=chain, gas_price_gwei=round(gwei, 6), transfer_fee_usd=fee, as_of=now),
                      degraded=usd is None)


__all__ = ["CHAINS", "GasPricePipeline", "GasPriceResponse", "Settings", "parse_chain"]
