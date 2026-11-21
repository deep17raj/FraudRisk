"""Small, dependency-free text helpers shared by the parser and the heuristic stance labeller."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass

STOPWORDS = frozenset(
    """a an the and or but if then than that this these those of in on at to from by for with without
    about as into over under between during before after above below up down out off again further once
    is are was were be been being am do does did doing have has had having will would shall should can
    could may might must it its it's he she they them his her their theirs we us our you your i me my
    not no nor only own same so too very just also there here when where why how what which who whom
    whose all any both each few more most other some such s t don now than""".split()
)

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_WORD = re.compile(r"[A-Za-z][A-Za-z']*")

NEGATION = re.compile(
    r"\b(not|no|never|none|neither|nor|cannot|can't|isn't|aren't|wasn't|weren't|doesn't|don't|didn't"
    r"|won't|hasn't|haven't|hadn't|without|nobody|nothing)\b",
    re.I,
)


def strip_html(s: str) -> str:
    return _WS.sub(" ", html.unescape(_TAG.sub(" ", s or ""))).strip()


def split_sentences(text: str) -> list[str]:
    text = _WS.sub(" ", text or "").strip()
    if not text:
        return []
    return [s.strip() for s in _SENT_SPLIT.split(text) if s.strip()]


def stem(w: str) -> str:
    w = w.lower().strip("'")
    if w.endswith("'s"):
        w = w[:-2]
    for suf in ("ings", "ing", "edly", "ed", "es", "s", "ly"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def content_terms(text: str) -> list[str]:
    """Lower-cased, stemmed, stop-word-free word list (digits are handled by number logic)."""
    out = []
    for m in _WORD.finditer(text or ""):
        w = m.group(0).lower()
        if w in STOPWORDS or len(w) < 3:
            continue
        out.append(stem(w))
    return out


def has_negation(text: str) -> bool:
    return bool(NEGATION.search(text or ""))


# ---- numbers --------------------------------------------------------------------------------

_NUM = re.compile(
    r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?\s*(%|percent|per cent|thousand|million|billion|trillion|bn|m|k)?(?![\w])",
    re.I,
)
_SCALE = {
    "thousand": 1e3, "k": 1e3, "million": 1e6, "m": 1e6, "billion": 1e9, "bn": 1e9, "trillion": 1e12,
}


@dataclass(frozen=True)
class Num:
    value: float
    kind: str   # "year" | "pct" | "num"


def parse_numbers(text: str) -> list[Num]:
    out: list[Num] = []
    for m in _NUM.finditer(text or ""):
        whole, frac, unit = m.group(1), m.group(2), (m.group(3) or "").lower()
        try:
            v = float(whole.replace(",", "") + ("." + frac if frac else ""))
        except ValueError:
            continue
        if unit in ("%", "percent", "per cent"):
            out.append(Num(v, "pct"))
            continue
        if unit in _SCALE:
            v *= _SCALE[unit]
            out.append(Num(v, "num"))
            continue
        if "," not in whole and not frac and 1000 <= v <= 2100:
            out.append(Num(v, "year"))
        else:
            out.append(Num(v, "num"))
    return out


def compare_numbers(claim_nums: list[Num], text_nums: list[Num]) -> str:
    """Return 'none' (nothing comparable), 'match', 'close', or 'conflict'.

    A claim number is *comparable* when the text contains a number of the same kind. 'match'
    = every comparable claim number appears (within 1%); 'close' = within 10%; otherwise 'conflict'.
    """
    comparable = 0
    exact = 0
    near = 0
    for c in claim_nums:
        same = [t for t in text_nums if t.kind == c.kind]
        if not same:
            continue
        comparable += 1
        best = min(abs(t.value - c.value) / max(abs(c.value), 1e-9) for t in same)
        if c.kind == "year":
            if best == 0:
                exact += 1
            continue
        if best <= 0.01:
            exact += 1
        elif best <= 0.10:
            near += 1
    if comparable == 0:
        return "none"
    if exact == comparable:
        return "match"
    if exact + near == comparable:
        return "close"
    return "conflict"
