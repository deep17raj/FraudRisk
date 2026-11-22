"""Fake DNS-over-HTTPS resolvers so every test here is hermetic."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from emailsec.config import Settings  # noqa: E402

GOOGLE, CLOUDFLARE, SECURITY = "dns.google", "cloudflare-dns.com", "security.cloudflare-dns.com"


def make_settings(**kw) -> Settings:
    base = dict(provider_timeout=1.0, grace=0.1, global_timeout=3.0, cache_ttl=60, cache_ttl_degraded=5)
    base.update(kw)
    return Settings(**base)


def noerror(name: str, *records: tuple[int, str]) -> dict:
    d = {"Status": 0, "TC": False, "RD": True, "RA": True, "Question": [{"name": name, "type": 1}]}
    if records:
        d["Answer"] = [{"name": name, "type": t, "TTL": 300, "data": data} for t, data in records]
    return d


def nxdomain(name: str) -> dict:
    return {"Status": 3, "Question": [{"name": name, "type": 1}]}


def servfail(name: str) -> dict:
    return {"Status": 2, "Question": [{"name": name, "type": 1}]}


class FakeDns:
    """host -> rule. A rule is a dict, an Exception, a (dict, delay) tuple, an int (an HTTP status), or
    a callable(name, rtype) returning any of those. A missing rule means that resolver is down (503)."""

    def __init__(self, **rules) -> None:
        self.rules = {GOOGLE: rules.get("google"), CLOUDFLARE: rules.get("cloudflare"),
                      SECURITY: rules.get("security")}
        self.calls: list[tuple[str, str, str]] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        host, name, rtype = request.url.host, request.url.params["name"], request.url.params["type"]
        self.calls.append((host, name, rtype))
        rule = self.rules.get(host)
        if callable(rule):
            rule = rule(name, rtype)
        delay = 0.0
        if isinstance(rule, tuple):
            rule, delay = rule
        if delay:
            await asyncio.sleep(delay)
        if isinstance(rule, Exception):
            raise rule
        if isinstance(rule, int):
            return httpx.Response(rule, text="status")
        if rule is None:
            return httpx.Response(503, text="down")
        return httpx.Response(200, json=rule)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))

    def count(self, host: str | None = None) -> int:
        return len([c for c in self.calls if host is None or c[0] == host])
