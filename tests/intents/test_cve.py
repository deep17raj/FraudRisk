from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from intents.cve.app import create_app
from intents.cve.config import Settings
from intents.cve.pipeline import CvePipeline, CveResponse
from intents.cve.source import CIRCL, NVD, lookup, parse_cve_id, severity_for
from intents.vulntriage.app import VulnTriagePipeline, VulnTriageResponse, create_app as create_triage
from intentkit.upstream import Upstream
from tests.intents.helpers import FakeHttp, StubPipeline, fast


def nvd(primary=8.8, secondary=9.8, v30=None, v2=None, desc="Remote code execution.", total=1):
    metrics = {}
    if primary is not None or secondary is not None:
        metrics["cvssMetricV31"] = [
            *([{"source": "vendor", "type": "Secondary", "cvssData": {"baseScore": secondary}}] if secondary is not None else []),
            *([{"source": "nvd@nist.gov", "type": "Primary", "cvssData": {"baseScore": primary}}] if primary is not None else [])]
    if v30 is not None:
        metrics["cvssMetricV30"] = [{"type": "Primary", "cvssData": {"baseScore": v30}}]
    if v2 is not None:
        metrics["cvssMetricV2"] = [{"type": "Primary", "cvssData": {"baseScore": v2}}]
    vulns = [] if not total else [{"cve": {"id": "CVE-2017-0144", "published": "2017-03-17T00:59:00.000",
                                           "descriptions": [{"lang": "es", "value": "hola"}, {"lang": "en", "value": desc}],
                                           "metrics": metrics, "references": [{"url": "https://a.test/1"}, {"url": "https://a.test/2"}]}}]
    return {"totalResults": total, "vulnerabilities": vulns}


def circl(score=7.5):
    cna = {"descriptions": [{"lang": "en", "value": "CIRCL text"}], "references": [{"url": "https://c.test/1"}]}
    if score is not None:
        cna["metrics"] = [{"cvssV3_1": {"baseScore": score, "baseSeverity": "HIGH"}}]
    return {"cveMetadata": {"cveId": "CVE-2017-0144", "datePublished": "2017-03-14T00:00:00Z"}, "containers": {"cna": cna}}


def up(fake):
    return Upstream(fake.client(), timeout=1.0)


async def test_the_nvd_primary_score_is_preferred_to_a_secondary_one():
    info = await lookup(up(FakeHttp({NVD: nvd(primary=8.8, secondary=9.8)})), "CVE-2017-0144")
    assert (info.score, info.severity, info.source) == (8.8, "HIGH", "NVD")
    assert info.description == "Remote code execution." and info.published == "2017-03-17"
    assert info.references == ("https://a.test/1", "https://a.test/2")


async def test_older_cvss_versions_are_used_only_when_v31_is_absent():
    assert (await lookup(up(FakeHttp({NVD: nvd(None, None, v30=7.0, v2=5.0)})), "CVE-2000-0001")).score == 7.0
    assert (await lookup(up(FakeHttp({NVD: nvd(None, None, v2=5.0)})), "CVE-2000-0002")).score == 5.0


async def test_circl_answers_when_nvd_is_down_or_rate_limited():
    for rule in (503, 403, 429, None):
        info = await lookup(up(FakeHttp({NVD: rule, CIRCL: circl(7.5)})), "CVE-2017-0144")
        assert (info.score, info.source) == (7.5, "CIRCL")


async def test_circl_fills_in_a_score_nvd_has_not_assigned_yet_and_keeps_nvds_text():
    info = await lookup(up(FakeHttp({NVD: nvd(None, None, desc="NVD text"), CIRCL: circl(6.1)})), "CVE-2017-0144")
    assert (info.score, info.description) == (6.1, "NVD text")


async def test_a_known_cve_without_any_score_has_a_null_score_not_zero():
    info = await lookup(up(FakeHttp({NVD: nvd(None, None), CIRCL: circl(None)})), "CVE-2017-0144")
    assert info is not None and info.score is None and info.severity is None


async def test_an_unknown_cve_and_an_outage_are_told_apart():
    unknown = await lookup(up(FakeHttp({NVD: nvd(total=0), CIRCL: 404})), "CVE-2099-0001")
    assert unknown is not None and unknown.score is None
    assert await lookup(up(FakeHttp({NVD: 503, CIRCL: 503})), "CVE-2099-0002") is None


async def test_both_cve_intents_share_one_nvd_request_per_cve():
    fake = FakeHttp({NVD: nvd()})
    full = CvePipeline(fast(Settings), fake.client())
    triage = VulnTriagePipeline(fast(Settings), fake.client())
    await full.answer({"cve_id": "CVE-2017-0144"})
    await triage.answer({"cve_id": "CVE-2017-0144"})
    assert fake.count(NVD) == 1


async def test_a_cve_without_a_score_is_remembered_only_briefly():
    from intents.cve.source import SHARED, TTL_DEGRADED
    fake = FakeHttp({NVD: nvd(None, None), CIRCL: circl(None)})
    await lookup(up(fake), "CVE-2017-0144")
    assert SHARED.get("CVE-2017-0144") is not None and TTL_DEGRADED < 3600


async def test_the_nvd_api_key_is_sent_when_configured():
    fake = FakeHttp({NVD: nvd()})
    await lookup(up(fake), "CVE-2017-0144", nvd_api_key="secret")
    assert fake.calls[0].headers["apiKey"] == "secret"


@pytest.mark.parametrize("raw,cve", [("CVE-2021-44228", "CVE-2021-44228"), ("cve-2014-0160", "CVE-2014-0160"),
                                     ("what is cve-2024-3094?", "CVE-2024-3094"), ("log4shell", None), ("", None)])
def test_cve_ids(raw, cve):
    assert parse_cve_id(raw) == cve


@pytest.mark.parametrize("score,sev", [(0, "NONE"), (0.1, "LOW"), (3.9, "LOW"), (4.0, "MEDIUM"), (6.9, "MEDIUM"),
                                       (7.0, "HIGH"), (8.9, "HIGH"), (9.0, "CRITICAL"), (10, "CRITICAL")])
def test_severity_bands(score, sev):
    assert severity_for(score) == sev


async def test_pipeline_answers_with_every_spec_field():
    pl = CvePipeline(fast(Settings), FakeHttp({NVD: nvd()}).client())
    r = await pl.answer({"cve_id": "cve-2017-0144"})
    assert r.model_dump() == {"cve_id": "CVE-2017-0144", "cvss_score": 8.8, "severity": "HIGH",
                              "description": "Remote code execution.", "published": "2017-03-17",
                              "references": ["https://a.test/1", "https://a.test/2"]}


async def test_pipeline_degrades_to_nulls_for_junk_or_an_outage():
    pl = CvePipeline(fast(Settings), FakeHttp({NVD: 503, CIRCL: 503}).client())
    assert (await pl.answer({"cve_id": "CVE-2017-0144"})).cvss_score is None
    r = await pl.answer({"cve_id": "not a cve"})
    assert r.cvss_score is None and r.cve_id == "not a cve"


async def test_triage_returns_only_the_score():
    pl = VulnTriagePipeline(fast(Settings), FakeHttp({NVD: nvd()}).client())
    assert (await pl.answer({"cve_id": "CVE-2017-0144"})).model_dump() == {"cvss_base_score": 8.8}
    pl2 = VulnTriagePipeline(fast(Settings), FakeHttp({NVD: nvd(None, None), CIRCL: circl(None)}).client())
    assert (await pl2.answer({"cve_id": "CVE-2017-0999"})).cvss_base_score is None


def test_http_contracts():
    stub = StubPipeline(CveResponse, cve_id="CVE-1-1", cvss_score=9.8, severity="CRITICAL")
    with TestClient(create_app(fast(Settings), pipeline=stub)) as c:
        r = c.post("/cve-lookup", json={"cve_id": "CVE-1-1"})
    assert set(r.json()) == {"cve_id", "cvss_score", "severity", "description", "published", "references"}
    with TestClient(create_triage(fast(Settings), pipeline=StubPipeline(VulnTriageResponse, cvss_base_score=7.5))) as c:
        assert c.post("/vulnerability-triage", json={"cve_id": "CVE-1-1"}).json() == {"cvss_base_score": 7.5}


def test_severity_must_be_a_spec_enum_value():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        CveResponse(severity="SEVERE")
