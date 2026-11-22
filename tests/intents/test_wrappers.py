"""URL_SCAN_VERDICT and THREAT_INTELLIGENCE_INDICATOR are views over the urlsafe and malware pipelines."""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from intents.threatintel.app import IpsumFeed, ThreatIntelPipeline, ThreatIntelResponse, create_app as create_ti
from intents.urlscan.app import UrlScanPipeline, UrlScanResponse, create_app as create_us
from malware.config import Settings as MalwareSettings
from malware.pipeline import MalwareResult
from tests.intents.helpers import FakeHttp, StubPipeline
from tests.urlsafe.conftest import make_settings as urlsafe_settings
from urlsafe.pipeline import UrlResult
from urlsafe.verdict import Verdict


class Inner:
    def __init__(self, result):
        self.result, self.seen, self.cache = result, [], type("C", (), {"size": staticmethod(lambda: 0)})()
        self.started = False

    async def check(self, raw):
        self.seen.append(raw)
        return self.result

    async def start(self):
        self.started = True


def url_pipeline(verdict) -> UrlScanPipeline:
    pl = UrlScanPipeline(urlsafe_settings(), FakeHttp().client())
    pl.inner = Inner(UrlResult("u", verdict, 0, "s"))
    return pl


@pytest.mark.parametrize("verdict,safe", [(Verdict.SAFE, True), (Verdict.SUSPICIOUS, False),
                                          (Verdict.MALICIOUS, False), (Verdict.UNKNOWN, False)])
async def test_only_a_checked_and_unflagged_url_is_safe(verdict, safe):
    assert (await url_pipeline(verdict).answer({"url": "https://example.com"})).safe is safe


async def test_the_url_reaches_the_urlsafe_pipeline_and_the_fallback_is_not_safe():
    pl = url_pipeline(Verdict.SAFE)
    await pl.answer({"url": "https://example.com/x"})
    assert pl.inner.seen == ["https://example.com/x"]
    assert pl.fallback({}, "boom").safe is False
    await pl.start()
    assert pl.inner.started


def malware_settings(**kw):
    return MalwareSettings(**kw)


def ti_pipeline(result=MalwareResult(False), ipsum=()) -> ThreatIntelPipeline:
    pl = ThreatIntelPipeline(malware_settings(), FakeHttp().client())
    pl.inner = Inner(result)
    pl.ipsum._items = frozenset(ipsum)
    pl.ipsum._loaded_at = time.monotonic()
    return pl


async def test_a_flag_from_the_malware_sources_is_malicious():
    assert (await ti_pipeline(MalwareResult(True)).answer({"indicator": "evil.example"})).malicious is True


async def test_a_public_ip_on_enough_blocklists_is_malicious_and_others_are_not():
    pl = ti_pipeline(ipsum={"185.220.101.4"})
    assert (await pl.answer({"indicator": "185.220.101.4"})).malicious is True
    assert (await pl.answer({"indicator": "8.8.8.8"})).malicious is False
    assert (await pl.answer({"indicator": "what is 185.220.101.4?"})).malicious is True


async def test_private_addresses_are_never_reported_even_if_a_feed_lists_them():
    pl = ti_pipeline(ipsum={"10.0.0.1", "127.0.0.1"})
    assert (await pl.answer({"indicator": "10.0.0.1"})).malicious is False


async def test_a_domain_a_cve_and_an_empty_request_are_not_looked_up_in_the_ip_feed():
    pl = ti_pipeline(ipsum={"185.220.101.4"})
    for raw in ("example.com", "CVE-2021-44228", "APT29", ""):
        assert (await pl.answer({"indicator": raw})).malicious is False


async def test_an_unloaded_feed_degrades_to_the_malware_answer():
    pl = ti_pipeline(ipsum=())
    assert (await pl.answer({"indicator": "185.220.101.4"})).malicious is False


def test_ipsum_keeps_only_addresses_on_enough_blocklists():
    body = "# header\n1.1.1.1\t1\n2.2.2.2\t2\n3.3.3.3\t5\nbad line\n4.4.4.4\tx\n"
    assert IpsumFeed(1, 1, min_lists=2).parse(body) == frozenset({"2.2.2.2", "3.3.3.3"})
    assert IpsumFeed(1, 1, min_lists=3).parse(body) == frozenset({"3.3.3.3"})


def test_wrapper_http_contracts():
    with TestClient(create_us(urlsafe_settings(), pipeline=StubPipeline(UrlScanResponse, safe=True))) as c:
        assert c.post("/url-scan", json={"url": "https://example.com"}).json() == {"safe": True}
    with TestClient(create_ti(malware_settings(), pipeline=StubPipeline(ThreatIntelResponse, malicious=True))) as c:
        assert c.post("/threat-intelligence", json={"indicator": "8.8.8.8"}).json() == {"malicious": True}
        assert c.post("/threat-intelligence", content=b"x").status_code == 200
