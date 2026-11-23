"""Small text helpers shared by the answer-writing intents."""
from __future__ import annotations

import html
import re
from typing import Iterable

_TAGS = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")
_WORD = re.compile(r"[a-z0-9]+")
STOPWORDS = frozenset("""a an and are as at be but by for from has have how i if in into is it its of on or that the their
there these this to was were what when where which who whom why will with you your do does did can could should would
about than then so not no yes tell me please give explain define""".split())


def strip_tags(raw: str) -> str:
    return _SPACE.sub(" ", html.unescape(_TAGS.sub(" ", raw or ""))).strip()


def clip(text: str, n: int) -> str:
    """At most n characters, cut at a word boundary, with an ellipsis only when something was cut."""
    text = _SPACE.sub(" ", (text or "").strip())
    if len(text) <= n:
        return text
    cut = text[: max(1, n - 1)]
    if " " in cut[n // 2:]:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:-") + "…"


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT.split(_SPACE.sub(" ", (text or "").strip())) if s.strip()]


def brief(text: str, limit: int = 80) -> str:
    """One plain sentence of at most `limit` characters: the first sentence, shortened if it must be."""
    flat = strip_tags(text)
    first = sentences(flat)[0] if flat else ""
    first = re.sub(r"\s*\[\d+\]", "", first).strip()
    if len(first) <= limit:
        return first
    cut = first[: limit - 1]
    if " " in cut[limit // 2:]:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:-.") + "…"


def tokens(text: str) -> list[str]:
    return [w for w in _WORD.findall((text or "").lower()) if w not in STOPWORDS and len(w) > 1]


def overlap(query: str, text: str) -> float:
    """Fraction of the query's content words that appear in `text` (0..1)."""
    q = set(tokens(query))
    return len(q & set(tokens(text))) / len(q) if q else 0.0


def best_sentences(query: str, texts: Iterable[str], n: int = 3, max_chars: int = 700) -> list[str]:
    """The `n` sentences most relevant to the query, in the order they were found, within a character budget."""
    scored: list[tuple[float, int, str]] = []
    idx = 0
    for t in texts:
        for s in sentences(t):
            if 25 <= len(s) <= 400:
                scored.append((overlap(query, s), idx, s))
            idx += 1
    picked = sorted(sorted(scored, key=lambda x: (-x[0], x[1]))[:n], key=lambda x: x[1])
    out, used = [], 0
    for _, _, s in picked:
        if used + len(s) > max_chars and out:
            break
        out.append(s)
        used += len(s)
    return out
