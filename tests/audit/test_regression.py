"""The regression corpus driven through the whole pipeline.

Every case asserts three things at once: the verdict the roll-up must reach, that the audit
cited the clause that actually decides it, and - on every single case, without exception -
that no citation is anything other than verbatim contract wording.
"""
from __future__ import annotations

import pytest

from audit.models import Verdict
from tests.audit.conftest import make_pipeline, make_settings
from tests.audit.fixtures import CASES

IDS = [c["name"] for c in CASES]


def test_the_corpus_covers_all_four_verdicts_and_is_big_enough():
    verdicts = {c["expect"] for c in CASES}
    assert verdicts == {"Compliant", "Partially compliant", "Non-compliant", "Unclear"}
    assert len(CASES) >= 10, "the brief asks for 10-15 sample contracts"
    assert len(IDS) == len(set(IDS)), "case names must be unique"


@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_verdict_matches_the_expected_outcome(case):
    pl = make_pipeline(case["payload"])
    res = await pl.audit(case["contract"])
    assert res.verdict.value == case["expect"], (
        f"{case['name']}: expected {case['expect']}, got {res.verdict.value} "
        f"(rule={res.rule})\nsummary: {res.summary}")


@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_every_citation_is_verbatim(case):
    """The invariant that must never break, on any input, for any verdict."""
    pl = make_pipeline(case["payload"])
    res = await pl.audit(case["contract"])
    for clause in res.clauses:
        assert clause in case["contract"], (
            f"{case['name']}: cited text is not in the contract: {clause!r}")


@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_the_decisive_clause_is_cited(case):
    pl = make_pipeline(case["payload"])
    res = await pl.audit(case["contract"])
    joined = "\n".join(res.clauses)
    for needle in case["must_cite"]:
        assert needle in joined, (
            f"{case['name']}: the clause that decides the audit was not cited "
            f"({needle!r} missing from {res.clauses})")


@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_the_response_is_never_empty_or_malformed(case):
    pl = make_pipeline(case["payload"])
    res = await pl.audit(case["contract"])
    payload = res.to_payload()
    assert set(payload) == {"verdict", "obligations", "clauses", "summary"}
    assert payload["verdict"] in {v.value for v in Verdict}
    assert payload["summary"].strip(), "a summary is always owed to the caller"
    assert isinstance(payload["obligations"], list) and isinstance(payload["clauses"], list)


@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_the_summary_agrees_with_the_verdict(case):
    """A summary arguing the opposite of the field beside it is worse than a plain one."""
    pl = make_pipeline(case["payload"])
    res = await pl.audit(case["contract"])
    summary = res.summary.lower()
    contradictions = {
        "Compliant": ["non-compliant", "is not met", "cannot be judged"],
        "Non-compliant": ["all obligations are met", "fully compliant"],
        "Partially compliant": ["non-compliant"],
        "Unclear": ["is compliant", "all obligations are met"],
    }[case["expect"]]
    for phrase in contradictions:
        assert phrase not in summary, f"{case['name']}: summary contradicts the verdict"


@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_obligations_name_a_party_and_a_duty(case):
    pl = make_pipeline(case["payload"])
    res = await pl.audit(case["contract"])
    for line in res.obligations:
        assert ":" in line, f"{case['name']}: obligation is not party-qualified: {line!r}"
        assert len(line) > 20, f"{case['name']}: obligation is too vague: {line!r}"


@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_audits_are_reproducible(case):
    """Identical input must give an identical answer: same verdict, same citations."""
    settings = make_settings(cache_ttl=0, cache_ttl_degraded=0)
    first = await make_pipeline(case["payload"], settings=settings).audit(case["contract"])
    second = await make_pipeline(case["payload"], settings=settings).audit(case["contract"])
    assert first.to_payload() == second.to_payload()


@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_the_deterministic_reader_also_answers_safely(case):
    """With no model at all, every case must still return a valid, verbatim-cited answer."""
    settings = make_settings(anthropic_api_key="", extraction_mode="off")
    pl = make_pipeline(settings=settings, available=False)
    res = await pl.audit(case["contract"])
    assert res.verdict in set(Verdict)
    assert res.summary.strip()
    for clause in res.clauses:
        assert clause in case["contract"], f"{case['name']}: fallback cited invented text"
