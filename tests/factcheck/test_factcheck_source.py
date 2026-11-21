"""Google Fact Check Tools source: a published rating must only be adopted for the SAME claim.

A `textualRating` describes the claim the fact-checker reviewed, not the user's. Because these items
bypass the heuristic cap (they are labelled `source`), importing one onto a differently worded claim
would be the worst failure this service can have: a confident, wrong, well-cited verdict.
Exercised through the real source with httpx.MockTransport - no network, no key.
"""
import httpx
import pytest

from factcheck.claim_parser import parse_claim
from factcheck.models import Stance, Verdict
from factcheck.pipeline import FactCheckPipeline
from factcheck.sources.factcheck_api import GoogleFactCheckSource
from tests.factcheck.conftest import make_settings


def _review(text, rating, domain, name="Fact Checker"):
    return {"text": text, "claimReview": [{
        "url": f"https://www.{domain}/fact-check/x", "title": text, "textualRating": rating,
        "publisher": {"name": name}, "reviewDate": "2024-02-01"}]}


def _client(claims):
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"claims": claims})))


async def _evidence(claim: str, claims: list[dict]):
    src = GoogleFactCheckSource("test-key")
    async with _client(claims) as c:
        return await src.search(parse_claim(claim), c, 5.0)


async def _verdict(claim: str, claims: list[dict]):
    src = GoogleFactCheckSource("test-key")
    async with _client(claims) as c:
        return await FactCheckPipeline(make_settings(), c, sources=[src]).check(claim)


VACCINES = [_review("Vaccines cause autism", "False", "snopes.com", "Snopes"),
            _review("Vaccines cause autism", "False", "politifact.com", "PolitiFact")]


async def test_exact_claim_adopts_the_published_rating():
    evs = await _evidence("Vaccines cause autism.", VACCINES)
    assert [e.preset_stance for e in evs] == [Stance.REFUTES, Stance.REFUTES]
    r = await _verdict("Vaccines cause autism.", VACCINES)
    assert r.verdict == Verdict.FALSE and len(r.sources) == 2


async def test_negated_claim_inverts_the_rating_instead_of_importing_it():
    """A 'False' rating of "X causes Y" is evidence FOR "X does not cause Y" - not against it."""
    evs = await _evidence("Vaccines do not cause autism.", VACCINES)
    assert [e.preset_stance for e in evs] == [Stance.SUPPORTS, Stance.SUPPORTS]
    assert all("opposite wording" in e.snippet for e in evs)
    r = await _verdict("Vaccines do not cause autism.", VACCINES)
    assert r.verdict in (Verdict.TRUE, Verdict.MOSTLY_TRUE), r.reasoning
    assert r.verdict not in (Verdict.FALSE, Verdict.MOSTLY_FALSE)


async def test_review_of_a_different_claim_is_not_adopted_as_a_verdict():
    claims = [_review("Biden signed a bill banning gas stoves", "False", "snopes.com", "Snopes")]
    evs = await _evidence("Biden signed the infrastructure bill.", claims)
    assert all(e.preset_stance is None for e in evs), "a different claim's rating must not preset a stance"
    assert all("differently worded" in e.snippet for e in evs)
    r = await _verdict("Biden signed the infrastructure bill.", claims)
    assert r.verdict == Verdict.UNVERIFIED, f"got {r.verdict.value}: {r.reasoning}"


async def test_conflicting_numbers_prevent_adoption():
    claims = [_review("Inflation reached 9 percent in 2022", "True", "politifact.com")]
    evs = await _evidence("Inflation reached 2 percent in 2022.", claims)
    assert all(e.preset_stance is None for e in evs)


async def test_unrelated_reviews_are_dropped_entirely():
    claims = [_review("The moon is made of cheese", "False", "snopes.com")]
    assert await _evidence("Inflation reached 9 percent in 2022.", claims) == []


async def test_rating_text_is_always_preserved_for_the_llm_and_the_citation():
    evs = await _evidence("Vaccines do not cause autism.", VACCINES)
    assert all(e.fact_check_rating == "False" for e in evs)
    assert all(e.url.startswith("https://www.") for e in evs)


@pytest.mark.parametrize("rating,expected", [
    ("True", Stance.REFUTES),        # inverted for the negated claim
    ("False", Stance.SUPPORTS),
    ("Half True", Stance.MIXED),
    ("Misleading", Stance.MISLEADING),
])
async def test_inversion_table(rating, expected):
    claims = [_review("Vaccines cause autism", rating, "snopes.com")]
    evs = await _evidence("Vaccines do not cause autism.", claims)
    assert evs[0].preset_stance == expected
