"""Regression suite: 20 claims with ground truth run through the FULL pipeline.

Offline mode (default): replays recorded Wikipedia snapshots + hand-written fact-checker items, using
the keyless heuristic stance labeller - so it needs no network and no keys, and it measures the floor.
Live mode (`pytest -m live`): same claims against real sources and (if set) real API keys.
"""
import json
from pathlib import Path

import httpx
import pytest

from factcheck.config import Settings
from factcheck.models import Evidence, Verdict
from factcheck.pipeline import FactCheckPipeline
from factcheck.sources.factcheck_api import rating_to_stance
from tests.factcheck.conftest import FakeSource, make_settings

FIX = Path(__file__).parent / "fixtures"
CLAIMS = json.loads((FIX / "claims.json").read_text(encoding="utf-8"))["claims"]

TRUE_SIDE = {"True", "Mostly True"}
FALSE_SIDE = {"False", "Mostly False"}


def _evidence_for(c: dict) -> list[Evidence]:
    out: list[Evidence] = []
    for item in c.get("synthetic_evidence", []):
        stance, conf = rating_to_stance(item["rating"])
        out.append(Evidence(url=item["url"], title=item["title"], snippet=item["snippet"],
                            provider="google-factcheck", preset_stance=stance, preset_confidence=conf * 0.9,
                            fact_check_rating=item["rating"]))
    path = FIX / "evidence" / f"{c['id']}.json"
    if path.exists():
        for e in json.loads(path.read_text(encoding="utf-8"))["evidence"]:
            out.append(Evidence(**e))
    return out


async def _run(c: dict):
    settings = make_settings()
    async with httpx.AsyncClient() as client:
        pl = FactCheckPipeline(settings, client, sources=[FakeSource("replay", _evidence_for(c))])
        return await pl.check(c["claim"])


def _polarity_inverted(expected: str, got: str) -> bool:
    return (expected in TRUE_SIDE and got in FALSE_SIDE) or (expected in FALSE_SIDE and got in TRUE_SIDE)


@pytest.mark.parametrize("c", CLAIMS, ids=[c["id"] for c in CLAIMS])
async def test_claim_lands_in_acceptable_set(c):
    r = await _run(c)
    got = r.verdict.value
    assert not _polarity_inverted(c["expected"], got), f"POLARITY INVERSION: {c['claim']!r} expected {c['expected']} got {got}"
    assert got in c["acceptable"], (
        f"{c['claim']!r}: expected {c['expected']} (acceptable {c['acceptable']}), got {got}\n"
        f"rule={r.debug.get('rule')} reasoning={r.reasoning}\nevidence={r.debug.get('evidence')}")
    # honesty invariants
    retrieved = {e.url for e in _evidence_for(c)}
    assert set(r.sources) <= retrieved
    if got == "Unverified" and r.debug.get("rejected"):
        assert r.sources == []


async def test_summary_scorecard(capsys):
    """Prints a scorecard and enforces floor metrics so quality regressions are visible."""
    rows, exact, informative_ok, informative_total, inversions = [], 0, 0, 0, 0
    for c in CLAIMS:
        r = await _run(c)
        got = r.verdict.value
        exact += got == c["expected"]
        inversions += _polarity_inverted(c["expected"], got)
        if c["expected"] != "Unverified":
            informative_total += 1
            informative_ok += got != "Unverified" and got in c["acceptable"]
        rows.append((c["id"], c["expected"], got, r.debug.get("rule")))
    with capsys.disabled():
        print("\n  id                  expected       got            rule")
        for row in rows:
            print(f"  {row[0]:<19} {row[1]:<14} {row[2]:<14} {row[3]}")
        print(f"  exact match: {exact}/{len(CLAIMS)}   informative (non-abstaining & acceptable): "
              f"{informative_ok}/{informative_total}   polarity inversions: {inversions}")
    # Floors = the measured keyless/heuristic baseline (6/19 exact, 10/15 informative) with slack.
    # Exact match is structurally limited without keys: decide() caps heuristic-only evidence at
    # Mostly True / Mostly False, so a "True"/"False" ground truth can never be matched exactly in this
    # mode. The meaningful metrics here are therefore zero polarity inversions and the informative rate.
    # These are regression guards, NOT a claim about LLM-mode or keyed-provider accuracy.
    assert inversions == 0
    assert exact / len(CLAIMS) >= 0.26
    assert informative_ok / informative_total >= 0.60


# ------------------------------------------------------------------ live (opt-in)
@pytest.mark.live
@pytest.mark.parametrize("c", [c for c in CLAIMS if not c.get("synthetic")], ids=lambda c: c["id"])
async def test_live_claims(c):
    s = Settings.from_env()
    async with httpx.AsyncClient(http2=True, follow_redirects=True, headers={"User-Agent": s.user_agent}) as client:
        pl = FactCheckPipeline(s, client)
        r = await pl.check(c["claim"])
    got = r.verdict.value
    assert not _polarity_inverted(c["expected"], got), f"{c['claim']!r}: expected {c['expected']} got {got}: {r.reasoning}"
    assert got in c["acceptable"], f"{c['claim']!r}: got {got} ({r.reasoning})"
    assert set(r.sources) <= {e["url"] for e in r.debug.get("evidence", [])}
