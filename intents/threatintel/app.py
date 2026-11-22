"""THREAT_INTELLIGENCE_INDICATOR: `malicious` for an IP address, domain or file hash.

Two layers:
  * `malware.pipeline.MalwarePipeline` (CIRCL hashlookup, Team Cymru, AlienVault OTX, OpenPhish and Cloudflare's
    security DNS), so this intent can never disagree with MALWARE_DETECTION_HASH about a hash or domain;
  * IPsum, an open aggregate of more than thirty public IP blocklists, for IP addresses. An address is reported
    when at least `IPSUM_MIN_LISTS` (default 2) independent blocklists carry it. One list alone is too noisy.

A CVE or a threat-actor name carries no hash, domain or IP to look up, so it reads as "not reported" (false); no
source here can say otherwise.
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
from typing import Optional

import httpx
from fastapi import FastAPI

from intentkit.feeds import BulkFeed
from intentkit.pipeline import Req
from intentkit.request import GENERIC_ALIASES
from intentkit.service import StrictModel, create_service_app
from malware.config import Settings
from malware.indicator import Kind, classify
from malware.pipeline import MalwarePipeline

VERSION = "0.1.0"
IPSUM_URL = "https://raw.githubusercontent.com/stamparm/ipsum/master/ipsum.txt"
FIELDS = {"indicator": ("ioc", "ip", "domain", "hash", "url", "host", *GENERIC_ALIASES)}


class ThreatIntelResponse(StrictModel):
    """Exactly the response field of Telegraph's THREAT_INTELLIGENCE_INDICATOR intent. Nothing else."""

    malicious: bool = False


class IpsumFeed(BulkFeed):
    """IPsum lines are `ip<TAB>number of blocklists`; keep the addresses on at least `min_lists` of them."""

    def __init__(self, ttl: float, timeout: float, min_lists: int) -> None:
        super().__init__("ipsum", IPSUM_URL, ttl, timeout)
        self.min_lists = min_lists

    def parse(self, body: str) -> frozenset[str]:
        out: set[str] = set()
        for line in body.splitlines():
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit() and int(parts[1]) >= self.min_lists:
                out.add(parts[0])
        return frozenset(out)


class ThreatIntelPipeline:
    intent = "THREAT_INTELLIGENCE_INDICATOR"

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings, self.client = settings, client
        self.inner = MalwarePipeline(settings, client)
        self.cache = self.inner.cache
        self.ipsum = IpsumFeed(ttl=21600, timeout=max(settings.feed_timeout, 15.0),
                               min_lists=int(os.environ.get("IPSUM_MIN_LISTS", "2") or 2))

    async def start(self) -> None:
        await asyncio.gather(self.inner.start(), self.ipsum.preload(self.client), return_exceptions=True)

    async def answer(self, req: Req) -> ThreatIntelResponse:
        raw = req.get("indicator", "")
        ind = classify(raw)
        if ind.kind in (Kind.IPV4, Kind.IPV6):
            # The IPsum check is an in-memory lookup: do it first so a listed address never waits on OTX.
            self.ipsum.ensure_fresh(self.client)
            if ipaddress.ip_address(ind.value).is_global and self.ipsum.ready and ind.value in self.ipsum:
                return ThreatIntelResponse(malicious=True)
        return ThreatIntelResponse(malicious=(await self.inner.check(raw)).malicious)

    def fallback(self, req: Req, reason: str) -> ThreatIntelResponse:
        return ThreatIntelResponse(malicious=False)


def create_app(settings: Optional[Settings] = None, pipeline: Optional[ThreatIntelPipeline] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    return create_service_app(
        title="Telegraph Threat Intelligence Miner", intent="THREAT_INTELLIGENCE_INDICATOR",
        path="/threat-intelligence", version=VERSION, fields=FIELDS, model=ThreatIntelResponse, settings=settings,
        pipeline=pipeline, make_pipeline=ThreatIntelPipeline, start_timeout=max(settings.feed_timeout, 15.0) + 2)
