"""TOKEN_HOLDER_COUNT_GENERAL from public block-explorer indexes (keyless).

Sources differ in how they count holders (for USDT on Ethereum Blockscout, Ethplorer and Routescan disagree by 8-10%),
so they are never averaged: one primary per chain answers, and the next is used only if it could not.
  1. Blockscout (`/api/v2/tokens/<address>`), the instance of the asked chain;
  2. Ethplorer (Ethereum only, its public `freekey`);
  3. Routescan, because it labels its own count `approx`;
  4. GoPlus token security, then GeckoTerminal token info, keyless indexes that cover every supported chain.
Some Blockscout instances sit behind a Cloudflare bot challenge for some networks (Base, Polygon, Arbitrum); that is
treated as "source unavailable" (never worked around), and the answer is `null` only when no source can answer. A token
given by symbol or name is first looked up in Uniswap's curated default token list (exactly one match on the chain), which
is fast and cannot pick a copycat; otherwise the chain's Blockscout search resolves it to the exact-symbol match that is
verified and has the largest market cap. An address in the request is always used as given.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Optional

from fastapi import FastAPI
from pydantic import BaseModel, Field

from intentkit.ask import capitalized_runs, is_sentence
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
CHAIN_IDS = {"ethereum": 1, "optimism": 10, "base": 8453, "polygon": 137, "arbitrum": 42161, "gnosis": 100, "avalanche": 43114}
GECKO_NETWORKS = {"ethereum": "eth", "optimism": "optimism", "base": "base", "polygon": "polygon_pos", "arbitrum": "arbitrum",
                  "gnosis": "xdai", "avalanche": "avax"}
TOKEN_LIST = "https://tokens.uniswap.org"      # Uniswap's curated default token list: one entry per symbol per chain
TOKEN_LIST_TTL = 6 * 3600.0
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


SKIP_WORDS = {"how", "what", "whats", "many", "much", "holders", "holder", "does", "do", "did", "have", "has", "the", "of", "on",
              "number", "count", "total", "token", "tokens", "coin", "for", "is", "are", "was", "there", "address",
              "addresses", "wallet", "wallets", "unique", "distinct", "hold", "holds", "holding", "tell", "me", "show",
              "give", "please", "which", "who", "i", "in", "and", "a", "an", "can", "you", "find", "get", "check", "erc20",
              "erc", "contract"}


def split_question(token: str, chain: str) -> tuple[str, str]:
    """(token, chain) from "How many holders does USDC have on Base?"; unchanged when `token` is not a sentence."""
    if not is_sentence(token):
        return token, chain
    m = _ADDRESS.search(token)
    if m:
        return m.group(0), chain or token
    chain_words = {w for name in (*CHAINS, *ALIASES) for w in name.split()} | {"mainnet", "chain", "network", "bnb"}
    words = re.findall(r"\$?[A-Za-z][A-Za-z0-9]*", token)

    def candidate(w: str, allow_chain_word: bool) -> bool:
        bare = w.lstrip("$")
        if bare.lower() in SKIP_WORDS or not (w.startswith("$") or bare[0].isupper() or bare.isupper()):
            return False
        return allow_chain_word or bare.lower() not in chain_words

    for allow in (False, True):                  # a ticker such as OP is also a chain alias: only used as a last resort
        group: list[str] = []
        for w in [*words, ""]:
            if w and candidate(w, allow):
                group.append(w)
            elif group:
                break
        if group:
            symbol = " ".join(g.lstrip("$") for g in group)
            rest = re.sub(r"\$?" + r"\s+".join(re.escape(g.lstrip("$")) for g in group), " ", token, count=1)
            return symbol, chain or rest
    return token, chain


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

    def __init__(self, settings, client) -> None:
        super().__init__(settings, client)
        self._listing: Optional[dict] = None
        self._listing_at = 0.0
        self._listing_ttl = TOKEN_LIST_TTL
        self._listing_lock = asyncio.Lock()

    def key(self, req: Req) -> Optional[str]:
        token, chain = split_question(req.get("token", "").strip(), req.get("chain", ""))
        t, c = token.lower(), parse_chain(chain) or chain.strip().lower()
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

    async def _goplus(self, chain_id: int, address: str) -> Optional[tuple[int, str]]:
        d = await self.up.get_json(f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}",
                                   params={"contract_addresses": address}, key="goplus")
        res = d.get("result") if isinstance(d, dict) and d.get("code") == 1 else None
        info = next(iter(res.values()), None) if isinstance(res, dict) and res else None
        n = _count(info.get("holder_count")) if isinstance(info, dict) else None
        return (n, str(info.get("token_symbol") or "")) if n is not None else None

    async def _gecko(self, network: str, address: str) -> Optional[tuple[int, str]]:
        d = await self.up.get_json(f"https://api.geckoterminal.com/api/v2/networks/{network}/tokens/{address}/info",
                                   key="geckoterminal")
        attrs = ((d.get("data") or {}).get("attributes") or {}) if isinstance(d, dict) else {}
        n = _count((attrs.get("holders") or {}).get("count")) if isinstance(attrs, dict) else None
        return (n, str(attrs.get("symbol") or "")) if n is not None else None

    async def _token_list(self) -> dict:
        """{chain id: {lower-case symbol or name: [(address, symbol)]}} from the curated list; {} when it cannot be read."""
        if self._listing is not None and time.monotonic() - self._listing_at < self._listing_ttl:
            return self._listing
        async with self._listing_lock:
            if self._listing is not None and time.monotonic() - self._listing_at < self._listing_ttl:
                return self._listing
            d = await self.up.get_json(TOKEN_LIST, timeout=8.0, key="tokenlist")
            tokens = d.get("tokens") if isinstance(d, dict) else None
            index: dict = {}
            for t in tokens if isinstance(tokens, list) else []:
                if not (isinstance(t, dict) and isinstance(t.get("chainId"), int) and isinstance(t.get("address"), str)
                        and _ADDRESS.fullmatch(t["address"])):
                    continue
                sym = str(t.get("symbol") or "")
                for label in {sym.lower(), str(t.get("name") or "").lower()} - {""}:
                    entries = index.setdefault(t["chainId"], {}).setdefault(label, [])
                    if t["address"].lower() not in {a.lower() for a, _ in entries}:
                        entries.append((t["address"], sym))
            self._listing, self._listing_at = index, time.monotonic()
            self._listing_ttl = TOKEN_LIST_TTL if index else 60.0     # an unreadable list is retried soon
            return index

    async def start(self) -> None:
        await self._token_list()

    async def _from_list(self, chain: str, text: str) -> Optional[tuple[str, str]]:
        """(address, symbol) when the curated list has exactly one token of that symbol or name on the chain."""
        found = (await self._token_list()).get(CHAIN_IDS[chain], {}).get(text.strip().lower(), [])
        return found[0] if len(found) == 1 else None

    async def _holders_of(self, chain: str, address: str) -> Optional[tuple[int, str]]:
        """The count for a known address: the chain's primary source first, the others only if it could not answer."""
        src = CHAINS[chain]
        answer: Optional[tuple[int, str]] = None
        if "blockscout" in src:
            answer = await self._blockscout(src["blockscout"], address)
        if answer is None and src.get("ethplorer"):
            answer = await self._ethplorer(address)
        if answer is None and "routescan" in src:
            answer = await self._routescan(src["routescan"], address)
        if answer is None:
            answer = await self._goplus(CHAIN_IDS[chain], address)
        if answer is None:
            answer = await self._gecko(GECKO_NETWORKS[chain], address)
        return answer

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
        token, chain_text = split_question(req.get("token", "").strip(), req.get("chain", ""))
        chain = parse_chain(chain_text)
        if not token:
            return Result(self.empty(req, "no token"), degraded=True)
        if chain is None:
            return Result(self.empty(req, "unsupported chain"), degraded=True)
        src = CHAINS[chain]
        m = _ADDRESS.search(token)
        address, listed_symbol = (m.group(0), "") if m else (None, "")
        if address is None:                                   # a symbol or name: the curated list resolves it fast
            listed = await self._from_list(chain, token)
            if listed is not None:
                address, listed_symbol = listed
        if address is None:                                   # not listed: Blockscout's search resolves it and counts at once
            named = await self._by_name(src["blockscout"], token) if "blockscout" in src else None
            if named is None:
                return Result(self.empty(req, "token not found or no explorer answered"), degraded=True)
            return Result(TokenHoldersResponse(token=clip(token, 200), chain=chain, holders_count=named[1], token_symbol=named[2][:30]))
        answer = await self._holders_of(chain, address)
        if answer is None and listed_symbol and "blockscout" in src:      # the list's pick had no count: try the name search
            named = await self._by_name(src["blockscout"], token)
            answer = (named[1], named[2]) if named is not None else None
        if answer is None:
            return Result(self.empty(req, "no explorer answered"), degraded=True)
        return Result(TokenHoldersResponse(token=clip(token, 200), chain=chain, holders_count=answer[0],
                                           token_symbol=(answer[1] or listed_symbol)[:30]))


def create_app(settings: Optional[Settings] = None, pipeline: Optional[TokenHoldersPipeline] = None) -> FastAPI:
    return create_service_app(
        title="Telegraph Token Holder Count Miner", intent="TOKEN_HOLDER_COUNT_GENERAL", path="/token-holders",
        version=VERSION, fields=FIELDS, model=TokenHoldersResponse, settings=settings or Settings.from_env(),
        pipeline=pipeline, make_pipeline=TokenHoldersPipeline, max_len=300)
