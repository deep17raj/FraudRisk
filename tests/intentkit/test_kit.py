"""The shared helpers: request reading, DoH client, bulk feed, URL feed and DNS blocklist check."""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from intentkit.cache import TTLCache
from intentkit.doh import DohClient
from intentkit.feeds import BulkFeed
from intentkit.request import read_fields
from intentkit.resilience import CircuitBreaker, CircuitOpen, call_with_retry
from intentkit.threat import UrlFeed, dns_blocked, host_of, url_key
from tests.emailsec.conftest import CLOUDFLARE, GOOGLE, SECURITY, FakeDns, noerror, nxdomain

# ---------------------------------------------------------------- request reading

def _echo_app() -> FastAPI:
    app = FastAPI()

    @app.post("/x")
    async def x(request: Request):
        return await read_fields(request, {"a": ("alias_a",), "b": ()}, max_len=10)
    return app


def test_read_fields_prefers_the_documented_name_then_aliases_then_the_query_string():
    c = TestClient(_echo_app())
    assert c.post("/x", json={"alias_a": "via-alias", "a": "direct"}).json() == {"a": "direct", "b": ""}
    assert c.post("/x", json={"alias_a": "via-alias"}).json()["a"] == "via-alias"
    assert c.post("/x?a=from-query&b=two", content=b"").json() == {"a": "from-query", "b": "two"}


def test_read_fields_ignores_non_strings_and_survives_bad_bodies_and_truncates():
    c = TestClient(_echo_app())
    assert c.post("/x", json={"a": 5, "b": None}).json() == {"a": "", "b": ""}
    assert c.post("/x", json=["a"]).json() == {"a": "", "b": ""}
    assert c.post("/x", content=b"{bad", headers={"content-type": "application/json"}).json() == {"a": "", "b": ""}
    assert c.post("/x", json={"a": "x" * 99}).json()["a"] == "x" * 10
    assert c.post("/x", json={"a": "  spaced  "}).json()["a"] == "spaced"


# ---------------------------------------------------------------- DoH client

async def test_doh_parses_status_records_and_comment():
    dns = FakeDns(google=lambda n, t: {**noerror(n, (1, "1.2.3.4")), "Comment": ["EDE(16): Censored"]})
    ans = await DohClient(dns.client()).query("google", "example.com", "A")
    assert ans.ok and ans.status == 0 and ans.data() == ["1.2.3.4"] and "Censored" in ans.comment
    assert ans.data(28) == []


async def test_doh_returns_none_rather_than_raising():
    for rule in (None, httpx.ConnectError("x"), 500, {"not": "dns"}):
        dns = FakeDns(google=rule)
        assert await DohClient(dns.client()).query("google", "example.com") is None


async def test_doh_maps_http_400_to_a_format_error():
    ans = await DohClient(FakeDns(google=400).client()).query("google", "example.com")
    assert ans is not None and ans.status == 1 and not ans.ok


async def test_doh_breaker_stops_hammering_a_dead_resolver():
    dns = FakeDns(google=None)
    doh = DohClient(dns.client(), failure_threshold=3, cooldown_s=60)
    for _ in range(6):
        await doh.query("google", "example.com")
    assert dns.count(GOOGLE) == 3


# ---------------------------------------------------------------- DNS blocklist check

async def test_cloudflare_security_block_is_recognised_by_a_null_address():
    dns = FakeDns(security=lambda n, t: noerror(n, (1, "0.0.0.0")))
    assert await dns_blocked(DohClient(dns.client()), "bad.example") is True
    assert dns.calls[0][0] == SECURITY


@pytest.mark.parametrize("data", [(1, "104.18.5.35"), (1, "93.184.216.34")])
async def test_a_normal_answer_is_not_a_block(data):
    dns = FakeDns(security=lambda n, t: noerror(n, data))
    assert await dns_blocked(DohClient(dns.client()), "fine.example") is False


async def test_nxdomain_is_not_a_block_and_an_outage_is_unknown():
    assert await dns_blocked(DohClient(FakeDns(security=lambda n, t: nxdomain(n)).client()), "x.example") is False
    assert await dns_blocked(DohClient(FakeDns(security=None).client()), "x.example") is None


# ---------------------------------------------------------------- URL feed

FEED = "\n".join([
    "https://www.roblox.ly/games/84833567598383/fight-on-a-baseplate?privateServerLinkCode=606",
    "http://www.galeri-pemenang99.blogspot.com/",
    "https://docs.google.com/forms/d/e/1FAIpQLSabc/viewform",
    "# a comment",
    "",
])


def loaded_feed() -> UrlFeed:
    f = UrlFeed("t", "http://feed.invalid", 3600, 1.0)
    f._items = f.parse(FEED)
    f._loaded_at = time.monotonic()
    return f


def test_url_key_ignores_scheme_www_trailing_slash_and_fragment():
    assert url_key("https://www.Example.com/a/b/#frag") == url_key("http://example.com/a/b")
    assert url_key("example.com/x?y=1") == "example.com/x?y=1"
    assert url_key("not a url with spaces") != ""   # best effort, never raises
    assert url_key("http://[::1") == ""             # unparseable -> empty


def test_host_of_strips_port_userinfo_and_www():
    assert host_of("https://user:pw@WWW.Example.com:8080/p") == "example.com"


def test_feed_matches_the_exact_url_regardless_of_scheme():
    f = loaded_feed()
    assert f.has_url("http://roblox.ly/games/84833567598383/fight-on-a-baseplate?privateServerLinkCode=606")
    assert not f.has_url("https://roblox.ly/games/84833567598383/other")


def test_a_whole_site_entry_matches_its_host_but_a_path_entry_does_not():
    f = loaded_feed()
    assert f.has_host("galeri-pemenang99.blogspot.com") and f.has_host("www.galeri-pemenang99.blogspot.com")
    # docs.google.com hosts one listed form, not a phishing site: the host must NOT match.
    assert not f.has_host("docs.google.com") and not f.has_host("roblox.ly")
    assert f.has_url("https://docs.google.com/forms/d/e/1FAIpQLSabc/viewform")
    assert not f.has_url("https://docs.google.com/document/d/safe")


def test_bulk_feed_skips_comments_and_blank_lines():
    f = BulkFeed("t", "http://x", 60, 1.0)
    assert f.parse("# c\n\n a \nb\n") == frozenset({"a", "b"})


async def test_feed_load_replaces_items_and_a_failed_refresh_keeps_the_old_copy():
    state = {"ok": True}

    def handler(request):
        return httpx.Response(200, text=FEED) if state["ok"] else httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    f = UrlFeed("t", "http://feed.invalid/f.txt", 3600, 1.0)
    assert not f.ready
    await f.preload(client)
    assert f.ready and f.has_host("galeri-pemenang99.blogspot.com")
    state["ok"] = False
    f._loaded_at = 0.0                 # force stale
    await f._load(client)
    assert f.ready and f.has_host("galeri-pemenang99.blogspot.com")    # still the old copy


async def test_preload_leaves_a_fresh_feed_alone():
    calls = []
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: calls.append(1) or httpx.Response(200, text="x")))
    f = loaded_feed()
    await f.preload(client)
    assert calls == []


# ---------------------------------------------------------------- cache and breaker

def test_cache_expires_evicts_and_ignores_non_positive_ttl():
    now = [0.0]
    c = TTLCache(max_items=2, clock=lambda: now[0])
    c.set("a", 1, 10)
    c.set("zero", 1, 0)
    assert c.get("a") == 1 and c.get("zero") is None
    now[0] = 11
    assert c.get("a") is None
    c.set("x", 1, 100); c.set("y", 2, 100); c.set("z", 3, 100)
    assert c.size() == 2 and c.get("x") is None


async def test_cache_single_flight_survives_a_cancelled_caller():
    c = TTLCache()
    started = asyncio.Event()

    async def work():
        started.set()
        await asyncio.sleep(0.05)
        return "done"

    first = asyncio.ensure_future(c.single_flight("k", work))
    await started.wait()
    first.cancel()
    assert await c.single_flight("k", work) == "done"      # joined the still-running computation


async def test_breaker_opens_then_half_opens_then_closes():
    now = [0.0]
    b = CircuitBreaker(failure_threshold=2, cooldown_s=10, clock=lambda: now[0])

    async def boom():
        raise httpx.ConnectError("x")

    async def fine():
        return 1

    for _ in range(2):
        with pytest.raises(httpx.ConnectError):
            await call_with_retry(boom, b, retries=0)
    with pytest.raises(CircuitOpen):
        await call_with_retry(fine, b)
    now[0] = 11
    assert await call_with_retry(fine, b) == 1 and b.state == "closed"
