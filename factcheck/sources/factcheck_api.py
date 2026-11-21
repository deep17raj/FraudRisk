"""Google Fact Check Tools API: surfaces existing published fact-checks, mapped to a stance."""
from __future__ import annotations

import re

import httpx

from ..models import Evidence, ParsedClaim, Stance
from ..text_utils import compare_numbers, content_terms, has_negation, parse_numbers
from .base import Source

URL = "https://factchecktools.googleapis.com/v1alpha1/claims:search"

# Ordered most-specific first: "mostly false" must win over plain "false", etc.
_RATING_RULES: list[tuple[re.Pattern, Stance, float]] = [
    (re.compile(r"\b(mostly|largely)\s+(false|untrue|incorrect|wrong)\b|\bmostly\s+fals", re.I), Stance.REFUTES, 0.6),
    (re.compile(r"\b(half|partly|partially)[\s\-]*(true|false|right|correct)\b|\bmixture\b|\bmixed\b|\b50/50\b", re.I),
     Stance.MIXED, 0.75),
    (re.compile(r"\b(mostly|largely|nearly|broadly)\s+(true|correct|accurate|right)\b", re.I), Stance.CAVEATED, 0.8),
    (re.compile(r"\b(misleading|missing context|out of context|lacks? context|needs? context|distort\w*|"
                r"exaggerat\w*|cherry\w*|half[\s\-]?truth|deceptive|technically true)\b", re.I),
     Stance.MISLEADING, 0.8),
    (re.compile(r"\b(unproven|unverified|unsubstantiated|unsupported|not enough evidence|undetermined|"
                r"unclear|outdated|satire|legend)\b", re.I), Stance.NEUTRAL, 0.0),
    (re.compile(r"\b(false|fake|hoax|pants on fire|incorrect|wrong|fabricated|untrue|debunked|scam|bogus|"
                r"not true|inaccurate|baseless|no evidence)\b", re.I), Stance.REFUTES, 0.9),
    (re.compile(r"\b(true|correct|accurate|right|real|verified|confirmed|legit)\b", re.I), Stance.SUPPORTS, 0.9),
]


def rating_to_stance(rating: str) -> tuple[Stance, float]:
    r = (rating or "").strip()
    for pat, stance, conf in _RATING_RULES:
        if pat.search(r):
            return stance, conf
    return Stance.NEUTRAL, 0.0


#: Inverting a rating when the review addresses the opposite wording of the claim.
_INVERSE = {
    Stance.SUPPORTS: Stance.REFUTES,
    Stance.REFUTES: Stance.SUPPORTS,
    Stance.CAVEATED: Stance.REFUTES,
    Stance.MIXED: Stance.MIXED,
    Stance.MISLEADING: Stance.MISLEADING,
    Stance.NEUTRAL: Stance.NEUTRAL,
}


class GoogleFactCheckSource(Source):
    name = "google-factcheck"

    def __init__(self, api_key: str) -> None:
        super().__init__()
        self.api_key = api_key

    def enabled(self) -> bool:
        return bool(self.api_key)

    async def search(self, claim: ParsedClaim, client: httpx.AsyncClient, timeout: float) -> list[Evidence]:
        r = await client.get(URL, params={"query": claim.search_queries[0][:250], "languageCode": "en",
                                          "pageSize": 8, "key": self.api_key}, timeout=timeout)
        r.raise_for_status()
        want = set(content_terms(claim.text))
        claim_nums = parse_numbers(claim.text)
        out: list[Evidence] = []
        for c in r.json().get("claims", []):
            reviewed_text = c.get("text", "")
            got = set(content_terms(reviewed_text))
            if not want or not got:
                continue
            # The rating describes the REVIEWED claim. Require a tight match in BOTH directions:
            # a fuzzy hit on a different claim would otherwise import someone else's verdict.
            fwd = len(want & got) / len(want)
            rev_ov = len(want & got) / len(got)
            same_polarity = has_negation(reviewed_text) == claim.negated
            numbers_agree = compare_numbers(claim_nums, parse_numbers(reviewed_text)) != "conflict"
            trusted = fwd >= 0.75 and rev_ov >= 0.6 and numbers_agree
            if fwd < 0.4:   # the API is a fuzzy search: ignore reviews of clearly different claims
                continue
            for rev in c.get("claimReview", []):
                url = rev.get("url")
                rating = rev.get("textualRating", "")
                if not url:
                    continue
                stance, conf = rating_to_stance(rating)
                pub = (rev.get("publisher") or {}).get("name", "fact-checker")
                snippet = f'{pub} rated the claim "{reviewed_text}" as: {rating or "n/a"}.'
                if trusted and same_polarity:
                    # Same claim, same polarity, same figures: adopt the published rating directly.
                    pass
                elif trusted:
                    # Same claim but the OPPOSITE polarity: a "False" rating of "X causes Y" is
                    # evidence FOR "X does not cause Y". Invert, with reduced confidence.
                    stance, conf = _INVERSE.get(stance, Stance.NEUTRAL), conf * 0.8
                    snippet += " (This review addresses the opposite wording of the claim.)"
                else:
                    # A review of a RELATED but different claim: keep it as ordinary evidence with the
                    # rating visible, and let the stance step judge it against the user's claim.
                    stance, conf = None, 0.0
                    snippet += " (This review addresses a differently worded claim.)"
                out.append(Evidence(
                    url=url, title=rev.get("title", "") or reviewed_text, snippet=snippet,
                    provider=self.name, published=rev.get("reviewDate"),
                    preset_stance=stance,
                    preset_confidence=(conf * min(1.0, 0.5 + fwd / 2)) if stance is not None else 0.0,
                    fact_check_rating=rating,
                ))
        return out
