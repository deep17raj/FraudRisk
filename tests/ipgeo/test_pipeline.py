"""The pipeline against mocked providers: consensus, partial failure, early exit, caching."""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from ipgeo.pipeline import IpGeoPipeline, fallback_result
from tests.ipgeo import payloads as pl
from tests.ipgeo.conftest import Routes, make_settings


def pipeline(routes: Routes, **kw) -> IpGeoPipeline:
    return IpGeoPipeline(make_settings(**kw), routes.client())


async def test_all_providers_answering_gives_the_bay_area_consensus():
    routes = Routes(dict(pl.ALL_BY_HOST))
    res = await pipeline(routes, soft_deadline=1.0).locate("8.8.8.8")
    assert res.ip == "8.8.8.8" and res.country == "United States" and res.city == "Mountain View"
    assert res.latitude is not None and 37.3 < res.latitude < 38.1 and -122.2 < res.longitude < -121.8
    assert res.isp == "Google LLC" and not res.degraded


async def test_a_dead_provider_does_not_stop_the_answer():
    routes = Routes({**pl.ALL_BY_HOST, "ipwho.is": httpx.ConnectTimeout("down")})
    res = await pipeline(routes, soft_deadline=1.0).locate("8.8.8.8")
    assert res.latitude is not None and "ipwho" not in res.providers


async def test_a_provider_returning_garbage_is_ignored():
    routes = Routes({**pl.ALL_BY_HOST, "ipapi.co": {"error": True, "reason": "RateLimited"},
                     "get.geojs.io": "<html>nope</html>"})
    res = await pipeline(routes, soft_deadline=1.0).locate("8.8.8.8")
    assert res.latitude is not None and res.country == "United States"


async def test_one_provider_alone_is_enough():
    routes = Routes({"ipwho.is": pl.IPWHO})
    res = await pipeline(routes).locate("8.8.8.8")
    assert (res.latitude, res.longitude) == (37.339394, -121.894955) and res.providers == ("ipwho",)


async def test_nobody_answering_is_reported_as_degraded_not_invented():
    routes = Routes({h: httpx.ConnectError("down") for h in pl.ALL_BY_HOST})
    res = await pipeline(routes).locate("8.8.8.8")
    assert res.latitude is None and res.longitude is None and res.degraded and res.country == ""


async def test_a_degraded_answer_is_cached_only_briefly():
    routes = Routes({h: httpx.ConnectError("down") for h in pl.ALL_BY_HOST})
    pl_ = pipeline(routes, cache_ttl_degraded=0)          # 0 = not cached at all
    await pl_.locate("8.8.8.8")
    first = routes.total
    await pl_.locate("8.8.8.8")
    assert routes.total > first                           # asked again rather than serving a stale failure


async def test_a_good_answer_is_served_from_cache():
    routes = Routes(dict(pl.ALL_BY_HOST))
    pl_ = pipeline(routes, soft_deadline=1.0)
    await pl_.locate("8.8.8.8")
    calls = routes.total
    again = await pl_.locate("  Where is 8.8.8.8 located? ")
    assert routes.total == calls and again.ip == "8.8.8.8"
    assert pl_.cache.size() == 1


async def test_concurrent_identical_requests_share_one_lookup():
    routes = Routes({h: (p, 0.05) for h, p in pl.ALL_BY_HOST.items()})
    pl_ = pipeline(routes, soft_deadline=1.0)
    await asyncio.gather(*(pl_.locate("8.8.8.8") for _ in range(8)))
    assert all(n == 1 for n in routes.calls.values())


@pytest.mark.parametrize("ip", ["10.0.0.1", "192.168.1.1", "127.0.0.1", "169.254.1.1", "::1", "fe80::1"])
async def test_non_public_addresses_make_no_network_calls(ip):
    routes = Routes(dict(pl.ALL_BY_HOST))
    res = await pipeline(routes).locate(ip)
    assert routes.total == 0 and res.latitude is None and res.ip == ip and not res.degraded


async def test_no_ip_in_the_request_makes_no_network_calls():
    routes = Routes(dict(pl.ALL_BY_HOST))
    res = await pipeline(routes).locate("where is my package")
    assert routes.total == 0 and res.latitude is None and "no IP" in res.note


async def test_it_does_not_wait_for_a_straggler_beyond_the_soft_deadline():
    routes = Routes({"ipinfo.io": pl.IPINFO, "ipapi.co": pl.IPAPI_CO, "ipwho.is": (pl.IPWHO, 2.0)})
    t0 = time.monotonic()
    res = await pipeline(routes, provider_timeout=3.0, soft_deadline=0.3).locate("8.8.8.8")
    assert time.monotonic() - t0 < 1.0
    assert set(res.providers) == {"ipinfo", "ipapi_co"}


async def test_providers_that_answer_within_the_soft_deadline_are_all_included_so_the_answer_is_stable():
    """Regression: breaking at the first two agreeing providers made the latitude depend on who answered first."""
    routes = Routes({"ipinfo.io": pl.IPINFO, "ipapi.co": (pl.IPAPI_CO, 0.05), "ipwho.is": (pl.IPWHO, 0.1),
                     "free.freeipapi.com": (pl.FREEIPAPI, 0.15), "get.geojs.io": (pl.GEOJS, 0.2)})
    t0 = time.monotonic()
    res = await pipeline(routes, provider_timeout=3.0, soft_deadline=1.0).locate("8.8.8.8")
    assert time.monotonic() - t0 < 0.6                       # stopped as soon as every provider had answered
    assert set(res.providers) == {"ipinfo", "ipwho", "ipapi_co", "freeipapi"}      # geojs is the outlier
    assert res.latitude == pytest.approx(37.422505, abs=1e-6)                      # the four-provider median


async def test_the_answer_does_not_depend_on_the_order_providers_respond_in():
    fast_first = Routes({"ipinfo.io": pl.IPINFO, "ipwho.is": (pl.IPWHO, 0.05), "ipapi.co": (pl.IPAPI_CO, 0.1),
                         "free.freeipapi.com": (pl.FREEIPAPI, 0.15), "get.geojs.io": (pl.GEOJS, 0.2)})
    reversed_ = Routes({"get.geojs.io": pl.GEOJS, "free.freeipapi.com": (pl.FREEIPAPI, 0.05),
                        "ipapi.co": (pl.IPAPI_CO, 0.1), "ipwho.is": (pl.IPWHO, 0.15), "ipinfo.io": (pl.IPINFO, 0.2)})
    a = await pipeline(fast_first, soft_deadline=1.0).locate("8.8.8.8")
    b = await pipeline(reversed_, soft_deadline=1.0).locate("8.8.8.8")
    assert (a.latitude, a.longitude, a.city, a.isp) == (b.latitude, b.longitude, b.city, b.isp)


async def test_a_lone_answer_waits_only_the_soft_deadline_for_company():
    routes = Routes({"ipinfo.io": pl.IPINFO, "ipwho.is": (pl.IPWHO, 5.0)})
    t0 = time.monotonic()
    res = await pipeline(routes, provider_timeout=6.0, soft_deadline=0.3).locate("8.8.8.8")
    assert time.monotonic() - t0 < 1.5 and res.providers == ("ipinfo",)


async def test_the_ipinfo_token_is_sent_when_configured():
    routes = Routes({"ipinfo.io": pl.IPINFO})
    await pipeline(routes, ipinfo_token="secret-token", providers="ipinfo").locate("8.8.8.8")
    assert routes.requests[0].url.params["token"] == "secret-token"


async def test_provider_failures_open_the_breaker_instead_of_hammering_a_dead_service():
    routes = Routes({"ipwho.is": httpx.ConnectError("down")})
    pl_ = pipeline(routes, providers="ipwho", cache_ttl_degraded=0)
    for n in range(8):
        await pl_.locate(f"8.8.8.{n + 1}")
    assert routes.calls["ipwho.is"] == 4                  # the breaker opened after 4 consecutive failures


def test_fallback_result_keeps_the_address_and_claims_no_location():
    r = fallback_result("Where is 8.8.8.8?", "global_timeout")
    assert r.ip == "8.8.8.8" and r.latitude is None and r.degraded
    assert fallback_result("nonsense", "x").ip == "nonsense"
