"""Read a return window and its conditions out of a policy page's text, and find the page.

Only statements about RETURNING / REFUNDING within a period count as a window. Periods that describe something else
(how long shipping, processing or the bank takes, "5 to 10 business days") are ignored, because mistaking "refund
processed in 5 business days" for "returns accepted for 5 days" would be a confident wrong answer.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Optional
from urllib.parse import urljoin, urlparse

from intentkit.text import clip, sentences, strip_tags

_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
                 "eleven": 11, "twelve": 12, "fourteen": 14, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
                 "forty-five": 45, "sixty": 60, "ninety": 90}
_NUM = r"(\d{1,3}|" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) + r")"
_PERIOD = re.compile(_NUM + r"(?:\s*\(\s*\d{1,3}\s*\))?[\s-]*(calendar\s+|business\s+|working\s+)?(day|week|month)s?\b", re.I)
_RETURNISH = re.compile(r"\b(return(?:s|ed|able)?|refund(?:s|ed|able)?|exchange[sd]?|money[\s-]?back|send (?:it |them )?back)\b", re.I)
_BASIS = re.compile(r"\b(receipt|receiv\w*|deliver\w*|purchase\w*|shipp?(?:ed|ment)\b|order(?:ed)?|window|guarantee|policy|"
                    r"within|from the date|of the date|after (?:you|the))\b", re.I)
_NOT_A_WINDOW = re.compile(r"\b(process(?:ed|ing)?|transit|arriv\w*|bank|credit card|statement|appear|post(?:ed)?|"
                           r"ship(?:s|ping)? (?:in|within|takes)|estimated|usually takes|may take|will be (?:issued|credited))\b", re.I)
_RANGE = re.compile(r"\d+\s*(?:to|-|–|or)\s*\d+\s*(?:calendar\s+|business\s+|working\s+)?days", re.I)
_STRONG = re.compile(r"\b(unopened|unused|original packaging|original condition|in (?:its )?original|restocking|store credit|"
                     r"non-?refundable|final sale|proof of purchase|return label|rma)\b", re.I)
_WEAK = re.compile(r"\b(receipt|not eligible|cannot be returned|can(?:not|'t) be refunded|damaged|opened|must be|only accept|"
                   r"free of charge|return shipping)\b", re.I)


def number_value(token: str) -> Optional[int]:
    t = token.strip().lower()
    return int(t) if t.isdigit() else _NUMBER_WORDS.get(t)


def find_windows(text: str) -> list[int]:
    """Every return window, in days, stated in the text (weeks x7, months x30), in the order they appear."""
    out: list[int] = []
    for s in sentences(text):
        if not _RETURNISH.search(s) or _NOT_A_WINDOW.search(s) or not _BASIS.search(s):
            continue
        clean = _RANGE.sub(" ", s)                            # "5 to 10 business days" is a duration, not a window
        for m in _PERIOD.finditer(clean):
            n, kind, unit = number_value(m.group(1)), (m.group(2) or "").lower().strip(), m.group(3).lower()
            if n is None or kind in ("business", "working") or n == 0:
                continue
            days = n * (7 if unit == "week" else 30 if unit == "month" else 1)
            if 1 <= days <= 3650:
                out.append(days)
    return out


def best_window(per_page: list[list[int]]) -> Optional[int]:
    """The most frequently stated window across pages (earlier pages break ties), else None."""
    flat = [d for page in per_page for d in page]
    if not flat:
        return None
    counts = Counter(flat)
    top = max(counts.values())
    return next(d for d in flat if counts[d] == top)


def conditions_of(texts: list[str], limit: int = 6) -> list[str]:
    seen, out = set(), []
    for t in texts:
        for s in sentences(t):
            if (_STRONG.search(s) or (_RETURNISH.search(s) and _WEAK.search(s))) and 30 <= len(s) <= 400:
                key = re.sub(r"\W+", " ", s.lower())[:80]
                if key not in seen:
                    seen.add(key)
                    out.append(clip(s, 220))
            if len(out) >= limit:
                return out
    return out


def page_text(html: str) -> str:
    """Readable text of a page body: scripts, styles and site chrome removed."""
    body = re.sub(r"(?is)<(script|style|noscript|nav|footer|header|svg)[^>]*>.*?</\1>", " ", html)
    return strip_tags(body)


def policy_links(html: str, base: str, limit: int = 4) -> list[str]:
    """Same-site links whose address or text mentions returns or refunds."""
    host = (urlparse(base).hostname or "").lower()
    out: list[str] = []
    for href, text in re.findall(r"""<a\b[^>]*href=["']([^"'#]+)["'][^>]*>(.*?)</a>""", html, re.I | re.S):
        if href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        if re.search(r"return|refund", f"{href} {strip_tags(text)}", re.I):
            u = urljoin(base, href)
            p = urlparse(u)
            if p.scheme in ("http", "https") and (p.hostname or "").lower().endswith(host.removeprefix("www.")) and u not in out:
                out.append(u.split("#")[0])
    return out[:limit]


def registrable(host: str) -> str:
    parts = (host or "").lower().strip(".").split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in {"co", "com", "org", "net", "ac", "gov"}:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])
