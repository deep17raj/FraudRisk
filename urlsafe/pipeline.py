"""Check one URL against the live phishing feed and Cloudflare's security DNS, plus its own structure."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from intentkit.cache import TTLCache
from intentkit.doh import DohClient
from intentkit.threat import OPENPHISH_URL, UrlFeed, dns_blocked, url_key

from .config import Settings
from .heuristics import analyse
from .parse import Target, parse_target
from .verdict import Evidence, Listing, Outcome, Verdict, decide

log = logging.getLogger("urlsafe.pipeline")

FEED_NAME = "OpenPhish"
DNS_NAME = "Cloudflare Security DNS"


@dataclass(frozen=True)
class UrlResult:
    url: str
    verdict: Verdict
    detections: int
    summary: str
    degraded: bool = False


def fallback_result(raw: str, reason: str) -> UrlResult:
    return UrlResult(url=raw.strip()[:2000], verdict=Verdict.UNKNOWN, detections=0, degraded=True,
                     summary=f"The check could not be completed ({reason}), so no verdict is given.")


class UrlSafePipeline:
    def __init__(self, settings: Settings, client: httpx.AsyncClient, feed: Optional[UrlFeed] = None,
                 doh: Optional[DohClient] = None) -> None:
        self.settings = settings
        self._client = client
        self.feed = feed if feed is not None else (
            UrlFeed("openphish", OPENPHISH_URL, settings.feed_ttl, settings.feed_timeout)
            if settings.enable_feed else None)
        self.doh = doh or DohClient(client, timeout=settings.provider_timeout)
        self.cache = TTLCache(max_items=settings.cache_max_items)

    async def start(self) -> None:
        """Load the feed once so the first request already has it. Never raises."""
        if self.feed is not None:
            await self.feed.preload(self._client)

    async def check(self, raw: str) -> UrlResult:
        target = parse_target(raw)
        if target is None:
            return UrlResult(url=raw.strip()[:2000], verdict=Verdict.UNKNOWN, detections=0,
                             summary="No URL or domain could be found in the request, so there is nothing to check.")
        if self.feed is not None:
            self.feed.ensure_fresh(self._client)
        key = url_key(target.url) or target.url
        hit = self.cache.get(key)
        if hit is not None:
            return UrlResult(url=target.raw, verdict=hit.verdict, detections=hit.detections,
                             summary=hit.summary, degraded=hit.degraded)
        res = await self.cache.single_flight(key, lambda: self._evaluate(target, key))
        return UrlResult(url=target.raw, verdict=res.verdict, detections=res.detections,
                         summary=res.summary, degraded=res.degraded)

    async def _gather(self, target: Target) -> Evidence:
        ev = Evidence(flags=analyse(target))
        feed = self.feed
        if feed is not None and feed.ready:
            ev.checked.append(FEED_NAME)
            if feed.has_url(target.url):
                ev.listings.append(Listing(FEED_NAME, "url", "this exact URL is on its live phishing feed"))
            elif feed.has_host(target.host):
                ev.listings.append(Listing(FEED_NAME, "host", "this site is on its live phishing feed"))
        if self.settings.enable_dns and not target.is_ip:
            blocked = await dns_blocked(self.doh, target.host)
            if blocked is not None:
                ev.checked.append(DNS_NAME)
                if blocked:
                    ev.listings.append(Listing(DNS_NAME, "host", "it blocks this host as malware or phishing"))
        return ev

    async def _evaluate(self, target: Target, key: str) -> UrlResult:
        t0 = time.monotonic()
        ev = await self._gather(target)
        out: Outcome = decide(ev, self.settings.suspicious_weight)
        degraded = out.verdict is Verdict.UNKNOWN
        res = UrlResult(url=target.raw, verdict=out.verdict, detections=out.detections,
                        summary=out.summary, degraded=degraded)
        self.cache.set(key, res, self.settings.cache_ttl_degraded if degraded else self.settings.cache_ttl)
        log.info("checked", extra={"fields": {
            "event": "checked", "url": target.raw[:300], "verdict": res.verdict.value,
            "detections": res.detections, "checked": ev.checked, "flags": [f.id for f in ev.flags],
            "ms": int((time.monotonic() - t0) * 1000)}})
        return res
