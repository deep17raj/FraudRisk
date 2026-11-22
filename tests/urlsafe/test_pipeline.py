"""The pipeline with an injected feed and fake Cloudflare security DNS."""
from __future__ import annotations

import httpx

from tests.urlsafe.conftest import FakeDns, blocked, build, clean, empty_feed, loaded_feed, make_settings
from urlsafe.pipeline import UrlSafePipeline, fallback_result
from urlsafe.verdict import Verdict

LISTED = "https://secure-login-update.evil.example/paypal/verify.php?id=1"


async def test_an_exact_feed_url_plus_a_dns_block_is_malicious_with_two_detections():
    pl, _ = build(security=lambda n, t: blocked(n))
    r = await pl.check(LISTED)
    assert r.verdict is Verdict.MALICIOUS and r.detections == 2 and r.url == LISTED
    assert "OpenPhish" in r.summary and "Cloudflare Security DNS" in r.summary


async def test_an_exact_feed_url_alone_is_malicious_with_one_detection():
    pl, _ = build(security=lambda n, t: clean(n))
    r = await pl.check(LISTED)
    assert r.verdict is Verdict.MALICIOUS and r.detections == 1


async def test_a_listed_whole_site_with_a_clean_dns_is_suspicious():
    pl, _ = build(security=lambda n, t: clean(n))
    r = await pl.check("http://galeri-pemenang99.blogspot.com/some/page")
    assert r.verdict is Verdict.SUSPICIOUS and r.detections == 1


async def test_a_dns_block_alone_is_suspicious():
    pl, _ = build(security=lambda n, t: blocked(n))
    r = await pl.check("http://malware.testcategory.com/")
    assert r.verdict is Verdict.SUSPICIOUS and r.detections == 1 and "Cloudflare" in r.summary


async def test_a_clean_address_is_safe_with_zero_detections():
    pl, _ = build(security=lambda n, t: clean(n))
    r = await pl.check("https://www.example.com/")
    assert r.verdict is Verdict.SAFE and r.detections == 0 and not r.degraded


async def test_other_pages_on_a_platform_with_one_listed_page_are_not_flagged():
    pl, _ = build(security=lambda n, t: clean(n))
    r = await pl.check("https://docs.google.com/document/d/some-safe-doc")
    assert r.verdict is Verdict.SAFE and r.detections == 0
    listed = await pl.check("https://docs.google.com/forms/d/e/1FAIpQLSabc/viewform")
    assert listed.verdict is Verdict.MALICIOUS


async def test_a_lure_shaped_address_is_suspicious_without_a_single_detection():
    pl, _ = build(security=lambda n, t: clean(n))
    r = await pl.check("https://paypal.com.account-verify.example/login")
    assert r.verdict is Verdict.SUSPICIOUS and r.detections == 0 and "brand name" in r.summary


async def test_no_source_reachable_is_unknown_and_degraded():
    pl, _ = build(feed=empty_feed(), security=None)
    r = await pl.check("https://www.example.com/")
    assert r.verdict is Verdict.UNKNOWN and r.degraded and r.detections == 0


async def test_an_unready_feed_with_working_dns_is_still_a_verdict_that_names_only_dns():
    pl, _ = build(feed=empty_feed(), security=lambda n, t: clean(n))
    r = await pl.check("https://www.example.com/")
    assert r.verdict is Verdict.SAFE and "OpenPhish" not in r.summary


async def test_a_dns_outage_does_not_hide_a_feed_listing():
    pl, _ = build(security=httpx.ConnectError("down"))
    r = await pl.check(LISTED)
    assert r.verdict is Verdict.MALICIOUS and r.detections == 1


async def test_an_ip_address_is_not_sent_to_dns_and_is_not_suspicious_on_that_alone():
    pl, dns = build(security=lambda n, t: clean(n))
    r = await pl.check("http://203.0.113.9/index.html")
    assert dns.count() == 0 and r.verdict is Verdict.SAFE


async def test_dns_can_be_switched_off():
    pl, dns = build(security=lambda n, t: blocked(n), enable_dns=False)
    r = await pl.check("http://malware.testcategory.com/")
    assert dns.count() == 0 and r.verdict is Verdict.SAFE


async def test_the_echoed_url_is_the_one_found_not_the_whole_question():
    pl, _ = build(security=lambda n, t: clean(n))
    r = await pl.check("Is https://www.example.com/page. safe to click?")
    assert r.url == "https://www.example.com/page"


async def test_no_url_in_the_request_is_unknown_with_no_network_calls():
    pl, dns = build(security=lambda n, t: clean(n))
    r = await pl.check("is this safe?")
    assert r.verdict is Verdict.UNKNOWN and dns.count() == 0 and "No URL" in r.summary


async def test_results_are_cached_by_url_identity_not_by_spelling():
    pl, dns = build(security=lambda n, t: clean(n), cache_ttl_degraded=0)
    await pl.check("https://www.example.com/a/")
    n = dns.count()
    r = await pl.check("http://EXAMPLE.com/a")
    assert dns.count() == n and r.url == "http://EXAMPLE.com/a"      # served from cache, echoing this caller's URL


async def test_a_degraded_unknown_is_not_cached():
    pl, dns = build(feed=empty_feed(), security=None, cache_ttl_degraded=0)
    await pl.check("https://www.example.com/")
    n = dns.count()
    await pl.check("https://www.example.com/")
    assert dns.count() > n


async def test_start_loads_the_feed_and_never_raises():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="http://x.example/\n")))
    pl = UrlSafePipeline(make_settings(), client, doh=None)
    await pl.start()
    assert pl.feed.ready
    broken = UrlSafePipeline(make_settings(), httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    await broken.start()
    assert not broken.feed.ready


def test_fallback_never_claims_safe():
    r = fallback_result("http://x.example", "timed out")
    assert r.verdict is Verdict.UNKNOWN and r.detections == 0 and r.degraded
