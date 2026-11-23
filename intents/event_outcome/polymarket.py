"""Settled prediction markets (Polymarket's public Gamma API) as evidence of an event's outcome.

A binary market counts as settled when it is closed and its Yes price has converged to ~1 (the event happened) or ~0 (it
did not). A closed market stuck near 0.5 was cancelled or split, so it is not evidence of anything. A market only
matches an event when their wording is close AND every number and month named in the event also appears in the market
(so "September 2025" is never answered from a "December 2025" market).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from intentkit.text import tokens
from intentkit.upstream import Upstream

SEARCH = "https://gamma-api.polymarket.com/public-search"
_MONTHS = ("january february march april may june july august september october november december "
           "jan feb mar apr jun jul aug sep sept oct nov dec").split()
_NEGATION = {"not", "no", "never", "fail", "fails", "failed", "without"}
_NOISE = {"will", "did", "does", "do", "happen", "happened", "market", "resolve", "resolved", "yes", "question"}


@dataclass(frozen=True)
class Settled:
    question: str
    yes: bool
    resolved_at: str
    url: str
    source: str            # the market's own resolution source, when it names one


def stem(w: str) -> str:
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w


def content_tokens(text: str) -> set[str]:
    return {stem(t) for t in tokens(text) if t not in _NOISE}


def anchors(text: str) -> set[str]:
    """Numbers and month names: the parts of a question that must agree exactly."""
    low = text.lower()
    nums = set(re.findall(r"\b\d[\d.,:]*\d\b|\b\d\b", low))
    months = {m[:3] for m in _MONTHS if re.search(rf"\b{m}\b", low)}
    return nums | months


def negations(text: str) -> set[str]:
    """Negation words in the raw text (the token helper drops them as stopwords, so this must look at the text)."""
    return set(re.findall(r"[a-z]+", text.lower())) & _NEGATION


def similarity(event: str, question: str) -> float:
    a, b = content_tokens(event), content_tokens(question)
    if not a or not b:
        return 0.0
    if negations(event) != negations(question):     # "will X" and "will X NOT" are opposite questions
        return 0.0
    if not anchors(event) <= anchors(question):
        return 0.0
    return len(a & b) / len(a | b)


def _prices(m: dict) -> Optional[tuple[str, float]]:
    """('Yes' | 'No', price) of the winning side of a binary market, else None."""
    try:
        outcomes = json.loads(m["outcomes"]) if isinstance(m.get("outcomes"), str) else m.get("outcomes")
        prices = [float(p) for p in (json.loads(m["outcomePrices"]) if isinstance(m.get("outcomePrices"), str) else m.get("outcomePrices"))]
    except (KeyError, ValueError, TypeError):
        return None
    if not (isinstance(outcomes, list) and len(outcomes) == 2 and len(prices) == 2):
        return None
    names = [str(o).strip().lower() for o in outcomes]
    if sorted(names) != ["no", "yes"]:
        return None
    yes = prices[names.index("yes")]
    return ("Yes", yes) if yes >= 0.99 else ("No", 1 - yes) if yes <= 0.01 else None


def settled_markets(payload: Any) -> list[tuple[dict, dict]]:
    """(event, market) pairs for every closed binary market in a search response."""
    out = []
    for ev in (payload.get("events") if isinstance(payload, dict) else None) or []:
        if not isinstance(ev, dict):
            continue
        for m in ev.get("markets") or []:
            if isinstance(m, dict) and m.get("closed") and m.get("question"):
                out.append((ev, m))
    return out


async def find_settled(up: Upstream, event: str, threshold: float, timeout: float = 5.0) -> Optional[Settled]:
    d = await up.get_json(SEARCH, params={"q": event[:200], "keep_closed_markets": 1, "limit_per_type": 8},
                          timeout=timeout, key="polymarket")
    best: Optional[tuple[float, dict, dict]] = None
    for ev, m in settled_markets(d):
        verdict = _prices(m)
        if verdict is None:
            continue
        score = similarity(event, m["question"])
        if score >= threshold and (best is None or score > best[0]):
            best = (score, ev, m)
    if best is None:
        return None
    _, ev, m = best
    verdict = _prices(m)
    slug = ev.get("slug") or m.get("slug") or ""
    closed = str(m.get("closedTime") or m.get("endDate") or "")[:10]
    src = m.get("resolutionSource") or ""
    return Settled(m["question"], verdict[0] == "Yes" if verdict else False, closed if re.fullmatch(r"\d{4}-\d{2}-\d{2}", closed) else "",
                   f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com", src if src.startswith("http") else "")
