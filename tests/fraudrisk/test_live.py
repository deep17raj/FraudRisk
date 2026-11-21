"""Live provider checks. Deselected by default; run with `python -m pytest -m live`.

These exist to catch a provider changing its response shape, losing its keyless tier or
disappearing - the kind of breakage no hermetic test can see. They assert the ADAPTER still
normalises real responses correctly, not that any particular IP is still flagged.
"""
from __future__ import annotations

import httpx
import pytest

from fraudrisk.config import Settings
from fraudrisk.models import Family
from fraudrisk.classify import classify
from fraudrisk.sources.card_bin import CardBinSource
from fraudrisk.sources.feeds import FeedRegistry
from fraudrisk.sources.ip_rep import IpWhoAdapter, IpReputationSource, TorExitSource
from fraudrisk.sources.phish import PhishFeedSource

pytestmark = pytest.mark.live

SETTINGS = Settings()


@pytest.fixture
async def client():
    async with httpx.AsyncClient(headers={"User-Agent": SETTINGS.user_agent},
                                 follow_redirects=True, timeout=20.0) as c:
        yield c


async def test_ipwho_keyless_tier_still_returns_usable_fields(client):
    intel = await IpWhoAdapter().lookup("8.8.8.8", client, 15.0, SETTINGS)
    assert intel is not None
    assert intel.country_code == "US"
    assert intel.asn and intel.org, "ASN/ownership is what the anonymity inference relies on"


async def test_ipwho_adapter_produces_a_scored_signal(client):
    source = IpReputationSource(IpWhoAdapter())
    signals = await source.collect(classify("8.8.8.8"), client, 15.0, SETTINGS)
    assert signals, "a successful lookup must yield at least one observation"
    assert all(0.0 <= s.severity <= 1.0 and 0.0 <= s.reliability <= 1.0 for s in signals)


async def test_tor_exit_list_is_reachable_and_plausible(client):
    feeds = FeedRegistry(3600, 20.0, tor=True, phish=False)
    await feeds.preload(client)
    assert feeds.tor.ready, "the Tor exit list did not load"
    assert feeds.tor.size > 200, f"only {feeds.tor.size} exits - feed shape may have changed"


async def test_tor_source_flags_a_current_exit_node(client):
    """Picks an address from the live list itself, so it cannot go stale."""
    feeds = FeedRegistry(3600, 20.0, tor=True, phish=False)
    await feeds.preload(client)
    sample = sorted(feeds.tor._items)[0]
    signals = await TorExitSource(feeds).collect(classify(sample), client, 5.0, SETTINGS)
    assert [s.id for s in signals] == ["anon.tor_exit"]
    assert signals[0].verified is True and signals[0].family is Family.ANONYMITY


async def test_openphish_feed_is_reachable_and_plausible(client):
    feeds = FeedRegistry(3600, 20.0, tor=False, phish=True)
    await feeds.preload(client)
    assert feeds.phish.ready, "the OpenPhish feed did not load"
    assert feeds.phish.size > 50, f"only {feeds.phish.size} entries"


async def test_phish_source_flags_a_current_feed_entry(client):
    feeds = FeedRegistry(3600, 20.0, tor=False, phish=True)
    await feeds.preload(client)
    sample = next(x for x in sorted(feeds.phish._items) if x.startswith("http"))
    parsed = classify(sample)
    signals = await PhishFeedSource(feeds).collect(parsed, client, 5.0, SETTINGS)
    assert any(s.id == "phish.feed_hit" and s.verified for s in signals), sample


async def _bin_signals_or_skip(subject, client):
    """binlist allows roughly 5 requests/minute unauthenticated.

    A 429 is an expected external condition, not a regression: the real pipeline treats it as
    transient, trips the breaker and drops the card signal. These tests exist to catch a
    RESPONSE-SHAPE change, so they skip rather than fail when the limit is in force.
    """
    try:
        return await CardBinSource().collect(classify(subject), client, 15.0, SETTINGS)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            pytest.skip("binlist rate limit (429) in force")
        raise


async def test_binlist_still_answers_and_normalises(client):
    signals = await _bin_signals_or_skip("BIN 457173", client)
    assert signals, "binlist returned nothing for a known BIN"
    assert any("Denmark" in s.text for s in signals), [s.text for s in signals]


async def test_binlist_treats_an_unknown_bin_as_no_data_not_an_error(client):
    assert await _bin_signals_or_skip("BIN 000000", client) == []


async def test_full_pipeline_against_live_sources(client):
    """One end-to-end pass with every real provider, inside the real time budget."""
    import time

    from fraudrisk.pipeline import FraudRiskPipeline

    pipeline = FraudRiskPipeline(Settings(anthropic_api_key=""), client)
    await pipeline.feeds.preload(client)

    t0 = time.monotonic()
    res = await pipeline.assess(
        "New account 2 days old, login from 185.220.101.1, first transfer of $9,500 "
        "to a new payee after a password reset")
    elapsed = time.monotonic() - t0

    assert elapsed < Settings().global_timeout
    assert res.summary.strip() and res.signals
    assert res.debug["corroborated"] is True, "live run should fuse at least two families"
