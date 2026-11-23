"""Hermetic building blocks for the intents/ tests: no test here touches the network."""
from __future__ import annotations

import asyncio
from typing import Any, Callable

import httpx
from pydantic import BaseModel


class StubPipeline:
    """Stands in for an intent's pipeline: answers with `model(**fields)` (defaults = the honest empty answer)."""

    def __init__(self, model: type[BaseModel], error: Exception | None = None, delay: float = 0.0, **fields) -> None:
        self.model, self.error, self.delay, self.fields = model, error, delay, fields
        self.cache = type("C", (), {"size": staticmethod(lambda: 0)})()
        self.seen: list[dict] = []

    async def answer(self, req) -> BaseModel:
        self.seen.append(dict(req))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.model(**self.fields)

    def fallback(self, req, reason: str) -> BaseModel:
        return self.model()


class FakeHttp:
    """An httpx transport that answers from a table, so a pipeline can be tested against canned sources.

    `routes` maps a URL (query string ignored) to a rule:
        dict / list        HTTP 200 with that JSON            int          that HTTP status, empty body
        str                HTTP 200 with that text            Exception    raised (a network failure)
        None               HTTP 503 (the source is down)      callable(request) -> any of the above
    The longest route that prefixes the request URL wins; an unrouted URL is a 503.
    """

    def __init__(self, routes: dict[str, Any] | None = None) -> None:
        self.routes = dict(routes or {})
        self.calls: list[httpx.Request] = []

    def add(self, url: str, rule: Any) -> "FakeHttp":
        self.routes[url] = rule
        return self

    def urls(self) -> list[str]:
        return [str(r.url.copy_with(query=None)) for r in self.calls]

    def count(self, prefix: str = "") -> int:
        return len([u for u in self.urls() if u.startswith(prefix)])

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        url = str(request.url.copy_with(query=None))
        keys = [k for k in self.routes if url.startswith(k)]
        rule = self.routes[max(keys, key=len)] if keys else None
        if callable(rule):
            rule = rule(request)
        if isinstance(rule, tuple):                 # (rule, delay seconds)
            rule, delay = rule
            await asyncio.sleep(delay)
        if isinstance(rule, Exception):
            raise rule
        if isinstance(rule, httpx.Response):        # a ready-made response (custom status and headers)
            return rule
        if isinstance(rule, bool) or rule is None:
            return httpx.Response(503, text="down")
        if isinstance(rule, int):
            return httpx.Response(rule, text="")
        if isinstance(rule, str):
            return httpx.Response(200, text=rule)
        return httpx.Response(200, json=rule)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self), follow_redirects=True)


def fast(settings_cls: Callable, **kw):
    """A Settings object with short timeouts and no cache surprises, for unit tests."""
    base = dict(provider_timeout=1.0, global_timeout=3.0, cache_ttl=60, cache_ttl_degraded=5)
    base.update(kw)
    return settings_cls(**base)
