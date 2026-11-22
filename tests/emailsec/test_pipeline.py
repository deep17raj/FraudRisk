"""lookup_ok is exactly "the resolver's DNS status was 0 (NOERROR)"."""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from emailsec.pipeline import EmailSecPipeline, fallback_lookup
from tests.emailsec.conftest import FakeDns, make_settings, noerror, nxdomain, servfail

MX = (15, "10 mx.example.com.")


def pipeline(dns: FakeDns, **kw) -> EmailSecPipeline:
    return EmailSecPipeline(make_settings(**kw), dns.client())


async def test_noerror_is_ok():
    dns = FakeDns(google=lambda n, t: noerror(n, MX), cloudflare=lambda n, t: noerror(n, MX))
    res = await pipeline(dns).lookup("example.com", "MX")
    assert res.ok and res.status == 0 and res.provider == "google" and not res.degraded


async def test_nxdomain_is_not_ok():
    dns = FakeDns(google=lambda n, t: nxdomain(n), cloudflare=lambda n, t: nxdomain(n))
    res = await pipeline(dns).lookup("no-such-domain-xyz.example", "MX")
    assert not res.ok and res.status == 3


async def test_a_name_with_no_record_of_that_type_is_still_ok():
    """NODATA is NOERROR with an empty answer section: the lookup itself succeeded."""
    dns = FakeDns(google=lambda n, t: noerror(n), cloudflare=lambda n, t: noerror(n))
    res = await pipeline(dns).lookup("example.com", "TXT")
    assert res.ok and res.status == 0


async def test_servfail_is_not_ok():
    dns = FakeDns(google=lambda n, t: servfail(n), cloudflare=lambda n, t: servfail(n))
    res = await pipeline(dns).lookup("broken.example", "A")
    assert not res.ok and res.status == 2


async def test_the_domain_part_of_an_email_address_is_queried():
    dns = FakeDns(google=lambda n, t: noerror(n, MX))
    await pipeline(dns).lookup("someone@Example.com", "mx")
    assert ("dns.google", "example.com", "MX") in dns.calls


async def test_a_missing_record_type_defaults_to_mx():
    dns = FakeDns(google=lambda n, t: noerror(n, MX))
    res = await pipeline(dns).lookup("example.com", "")
    assert res.record_type == "MX" and ("dns.google", "example.com", "MX") in dns.calls


async def test_the_fallback_resolver_answers_when_the_preferred_one_is_down():
    dns = FakeDns(google=None, cloudflare=lambda n, t: noerror(n, MX))
    res = await pipeline(dns).lookup("example.com", "MX")
    assert res.ok and res.provider == "cloudflare"


async def test_a_slow_preferred_resolver_is_not_waited_for_beyond_the_grace_period():
    dns = FakeDns(google=(noerror("example.com", MX), 2.0), cloudflare=lambda n, t: noerror(n, MX))
    t0 = time.monotonic()
    res = await pipeline(dns, provider_timeout=3.0, grace=0.1).lookup("example.com", "MX")
    assert time.monotonic() - t0 < 1.0 and res.provider == "cloudflare" and res.ok


async def test_when_resolvers_disagree_the_preferred_one_wins_even_if_it_answers_second():
    dns = FakeDns(google=(noerror("x.example", MX), 0.15), cloudflare=lambda n, t: nxdomain(n))
    res = await pipeline(dns, grace=0.5).lookup("x.example", "MX")
    assert res.provider == "google" and res.ok


async def test_both_resolvers_down_is_degraded_and_never_claims_success():
    dns = FakeDns(google=httpx.ConnectError("down"), cloudflare=httpx.ConnectError("down"))
    res = await pipeline(dns).lookup("example.com", "MX")
    assert not res.ok and res.degraded and res.status is None


async def test_a_resolver_400_is_a_format_error_not_an_outage():
    dns = FakeDns(google=400, cloudflare=400)
    res = await pipeline(dns).lookup("example.com", "MX")
    assert not res.ok and res.status == 1 and not res.degraded


@pytest.mark.parametrize("domain", ["not a domain!", "", "x" * 64 + ".com", "-bad.com"])
async def test_an_invalid_name_is_a_format_error_with_no_network_call(domain):
    dns = FakeDns(google=lambda n, t: noerror(n, MX))
    res = await pipeline(dns).lookup(domain, "MX")
    assert not res.ok and res.status == 1 and dns.count() == 0


async def test_an_invalid_record_type_is_a_format_error_with_no_network_call():
    dns = FakeDns(google=lambda n, t: noerror(n, MX))
    res = await pipeline(dns).lookup("example.com", "NOPE")
    assert not res.ok and res.status == 1 and dns.count() == 0


async def test_answers_are_cached_and_the_cache_key_ignores_case_and_formatting():
    dns = FakeDns(google=lambda n, t: noerror(n, MX))
    pl = pipeline(dns)
    await pl.lookup("Example.com", "mx")
    calls = dns.count()
    await pl.lookup("https://example.com/", "MX")
    assert dns.count() == calls and pl.cache.size() == 1


async def test_a_degraded_answer_is_not_served_stale():
    dns = FakeDns(google=httpx.ConnectError("down"), cloudflare=httpx.ConnectError("down"))
    pl = pipeline(dns, cache_ttl_degraded=0)
    await pl.lookup("example.com", "MX")
    n = dns.count()
    await pl.lookup("example.com", "MX")
    assert dns.count() > n


async def test_concurrent_identical_lookups_share_one_query():
    dns = FakeDns(google=(noerror("example.com", MX), 0.05), cloudflare=(noerror("example.com", MX), 0.05))
    pl = pipeline(dns)
    await asyncio.gather(*(pl.lookup("example.com", "MX") for _ in range(6)))
    assert dns.count("dns.google") == 1


def test_fallback_lookup_never_claims_success():
    r = fallback_lookup("example.com", "MX", "global_timeout")
    assert not r.ok and r.degraded
