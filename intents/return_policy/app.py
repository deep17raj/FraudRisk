"""RETURN_POLICY_VERIFY_GENERAL: read live from the retailer's own policy page.

  1. The retailer is a name, a domain or a URL. A name is resolved to its official website through Wikidata (property
     P856), choosing a result that looks like a company, never a person's name or a product.
  2. Candidate policy pages are collected: a small map of known policy URLs (a source LOCATOR, never a stored answer),
     links on the homepage that mention returns or refunds, and a few common paths (/returns, /refund-policy, ...).
  3. Every page is fetched through the SSRF-safe fetcher and parsed. Only statements about returning or refunding within a
     period count as a window (shipping, processing and bank times are ignored), weeks and months are converted to days, and
     the most frequently stated window wins.
`verified` is true only when a window was read from the retailer's own domain. Pages that block bots (Cloudflare challenge),
need JavaScript, or state no period give `return_window_days` null and `verified` false: nothing is guessed, and no
challenge is circumvented. A policy is cached for hours, so a shop is asked for a few pages a few times a day at most.
"""
from __future__ import annotations

import asyncio
import re
from typing import Optional
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI
from pydantic import BaseModel, Field

from intentkit.pipeline import BasePipeline, Req, Result
from intentkit.safe_fetch import FetchBlocked, fetch_page
from intentkit.service import StrictModel, create_service_app
from intentkit.text import clip

from .config import Settings
from .parse import best_window, conditions_of, find_windows, page_text, policy_links, registrable

VERSION = "0.1.0"
FIELDS = {"retailer": ("store", "merchant", "shop", "seller", "vendor", "company", "name", "url", "site", "query", "text", "input")}
WIKIDATA = "https://www.wikidata.org/w/api.php"
COMMON_PATHS = ("/returns", "/return-policy", "/refund-policy", "/policies/refund-policy", "/pages/returns",
                "/pages/return-policy", "/help/returns", "/shipping-returns")
#: Where known retailers publish their policy. Source locators only: the page is fetched and parsed on every (uncached) request.
LOCATORS: dict[str, tuple[str, ...]] = {
    "adafruit.com": ("https://www.adafruit.com/shipping",),
    "system76.com": ("https://system76.com/shipping-returns",),
    "sparkfun.com": ("https://www.sparkfun.com/returns",),
    "arduino.cc": ("https://store.arduino.cc/policies/refund-policy",),
    "frame.work": ("https://frame.work/returns",),
    "puri.sm": ("https://puri.sm/policies/",),
}
_NOISE = re.compile(r"\b(return|returns|refund|refunds|policy|policies|exchange|exchanges|the|store|shop|online|what|is|are|for|of|'s|"
                    r"how|long|many|days|do|does|can|i|a|an|at|from|to)\b", re.I)
_COMPANYISH = re.compile(r"company|retailer|store|manufacturer|brand|vendor|business|corporation|e-?commerce|online|electronics|"
                         r"clothing|supermarket|chain|marketplace|maker|supplier|distributor|shop", re.I)
_URL = re.compile(r"https?://[^\s<>\"']+", re.I)
_DOMAIN = re.compile(r"^(?:www\.)?((?:[a-z0-9-]+\.)+[a-z]{2,})(?:/|$)", re.I)


class ReturnPolicyResponse(StrictModel):
    """Exactly the response fields of Telegraph's RETURN_POLICY_VERIFY_GENERAL intent. Nothing else."""

    retailer: str = ""
    return_window_days: Optional[int] = Field(default=None, ge=0, le=3650)
    conditions: list[str] = Field(default_factory=list)
    verified: bool = False
    sources: list[str] = Field(default_factory=list)


def retailer_name(text: str) -> str:
    return re.sub(r"\s+", " ", _NOISE.sub(" ", text)).strip(" ?.,'\"")


class ReturnPolicyPipeline(BasePipeline):
    intent = "RETURN_POLICY_VERIFY_GENERAL"

    def key(self, req: Req) -> Optional[str]:
        r = re.sub(r"\s+", " ", req.get("retailer", "")).strip().lower()
        return f"rp:{r}" if r else None

    def empty(self, req: Req, reason: str) -> BaseModel:
        return ReturnPolicyResponse(retailer=req.get("retailer", "").strip()[:200])

    # ---- the retailer's site -----------------------------------------------------------------------------------
    async def _site_of(self, text: str) -> Optional[str]:
        m = _URL.search(text)
        if m:
            u = urlparse(m.group(0).rstrip(".,;)"))
            return f"{u.scheme}://{u.netloc}/" if u.netloc else None
        d = _DOMAIN.match(text.strip())
        if d:
            return f"https://{d.group(1).lower()}/"
        name = retailer_name(text)
        if not name:
            return None
        found = await self.up.get_json(WIKIDATA, params={"action": "wbsearchentities", "search": name[:80], "language": "en",
                                                         "limit": 6, "format": "json"})
        ids = [r["id"] for r in ((found or {}).get("search") or []) if isinstance(found, dict) and r.get("id")]
        if not ids:
            return None
        ents = await self.up.get_json(WIKIDATA, params={"action": "wbgetentities", "ids": "|".join(ids), "props": "claims|labels|descriptions",
                                                        "languages": "en", "format": "json"})
        best: Optional[tuple[float, str]] = None
        for i, eid in enumerate(ids):
            e = ((ents or {}).get("entities") or {}).get(eid) if isinstance(ents, dict) else None
            claims = ((e or {}).get("claims") or {}).get("P856") or []
            site = claims[0].get("mainsnak", {}).get("datavalue", {}).get("value") if claims else None
            if not isinstance(site, str) or not site.startswith("http"):
                continue
            label = (((e or {}).get("labels") or {}).get("en") or {}).get("value", "").lower()
            desc = (((e or {}).get("descriptions") or {}).get("en") or {}).get("value", "")
            fit = (2 if label == name.lower() else 0) + (1 if _COMPANYISH.search(desc) else 0)
            if fit >= 1 and (best is None or fit - 0.1 * i > best[0]):          # earlier search results win ties
                best = (fit - 0.1 * i, site)
        if best is None:
            return None
        u = urlparse(best[1])
        return f"{u.scheme}://{u.netloc}/"

    async def _page(self, url: str) -> tuple[str, int, str]:
        s: Settings = self.settings
        try:
            p = await fetch_page(self.client, url, timeout=s.provider_timeout, max_bytes=700_000, allow_private=s.allow_private_hosts)
        except (FetchBlocked, httpx.HTTPError, asyncio.TimeoutError):
            return url, 0, ""
        return p.url, p.status, p.text

    async def compute(self, req: Req) -> Result:
        s: Settings = self.settings
        raw = re.sub(r"\s+", " ", req.get("retailer", "")).strip()
        if not raw:
            return Result(self.empty(req, "no retailer"), degraded=True)
        base = ReturnPolicyResponse(retailer=clip(raw, 200))
        site = await self._site_of(raw)
        if site is None:
            return Result(base, degraded=False)
        host = urlparse(site).hostname or ""
        reg = registrable(host)
        urls = list(LOCATORS.get(reg, ()))
        home_url, home_status, home_html = await self._page(site)
        if home_status == 200:
            urls += policy_links(home_html, home_url)
        urls += [site.rstrip("/") + p for p in COMMON_PATHS]
        urls = list(dict.fromkeys(urls))[: s.max_pages]
        pages = await asyncio.wait_for(asyncio.gather(*(self._page(u) for u in urls)), timeout=max(2.0, s.global_timeout - 4.0)) \
            if urls else []
        texts: list[tuple[str, str]] = []
        for url, status, html in pages:
            if status == 200 and html:
                t = page_text(html)
                if re.search(r"return|refund", t, re.I):
                    texts.append((url, t))
        per_page = [find_windows(t) for _, t in texts]
        window = best_window(per_page)
        used = [u for (u, _), w in zip(texts, per_page) if window is not None and window in w]
        own = [u for u in used if registrable(urlparse(u).hostname or "") == reg]
        conditions = conditions_of([t for (u, t) in texts if u in used] or [t for _, t in texts])
        if window is None:
            sources = [u for u, _ in texts[:2]] if conditions else []
            return Result(ReturnPolicyResponse(retailer=base.retailer, conditions=conditions, verified=False, sources=sources),
                          degraded=not texts)
        return Result(ReturnPolicyResponse(retailer=base.retailer, return_window_days=window, conditions=conditions,
                                           verified=bool(own), sources=used[:4]))


def create_app(settings: Optional[Settings] = None, pipeline: Optional[ReturnPolicyPipeline] = None) -> FastAPI:
    return create_service_app(
        title="Telegraph Return Policy Miner", intent="RETURN_POLICY_VERIFY_GENERAL", path="/return-policy", version=VERSION,
        fields=FIELDS, model=ReturnPolicyResponse, settings=settings or Settings.from_env(), pipeline=pipeline,
        make_pipeline=ReturnPolicyPipeline, max_len=300)
