"""Verbatim clause citation: the one thing this miner must never get wrong.

A fabricated clause is the heaviest penalty this Intent carries, so nothing reaches the
`clauses` list unless it was *located in the source document* and is emitted as a slice of
that document - `source[a:b]` - rather than as the model's rendering of it.

A naive `quote in source` test is not good enough. Real quotes come back with curly quotes
straightened, line wraps collapsed, PDF hyphenation joined, or a "Clause 4.1 states that"
preamble glued on, and every one of those would be thrown away, leaving `clauses` empty and
the audit ungrounded. So the source is normalised once into a parallel view that keeps, for
every normalised character, the offset of the original character it came from. A quote is
normalised the same way, found in that view, and then mapped back through the offset table
to produce an exact slice of the untouched source.

Nothing is ever paraphrased or repaired: when a quote cannot be located, it is dropped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: Shorter than this and a "quote" is neither useful evidence nor safely locatable.
MIN_QUOTE_CHARS = 12
#: Bounds the search below so a pathological quote cannot spend the request's time budget.
_FIND_BUDGET = 400

# A quote that does not occur outright is salvaged only if what *does* occur is still
# recognisably that clause. Without these floors an invented clause gets laundered into a
# citation: "The Supplier shall indemnify the Buyer against all losses" would be trimmed
# down to the three words "The Supplier shall", which occur in almost every contract and
# are evidence of nothing. A salvaged run must therefore be substantial in its own right
# and must account for most of what the model actually claimed.
_MIN_SALVAGE_CHARS = 30
_MIN_SALVAGE_WORDS = 5
_MIN_SALVAGE_COVERAGE = 0.5

# Characters a PDF, a word processor or a copy-paste introduces that are not in the model's
# rendering of the same clause (or vice versa). `None` means "drop this character entirely".
_MAP: dict[str, str | None] = {
    # Quote marks are dropped from the matching view rather than folded to ASCII. A model
    # rendering a clause routinely loses the inline quotes the document puts around a
    # defined term - `within "thirty (30) days" of invoice` comes back without them - and
    # that difference sits in the *middle* of the quote, where edge trimming cannot reach
    # it. Dropping them on both sides makes the two agree; the slice returned to the caller
    # is still taken from the untouched source, so the real quote marks are preserved.
    "'": None,
    '"': None,
    "‘": None,
    "’": None,
    "‚": None,
    "‛": None,
    "′": None,
    "´": None,
    "“": None,
    "”": None,
    "„": None,
    "‟": None,
    "″": None,
    "«": None,
    "»": None,
    "‐": "-",
    "‑": "-",
    "‒": "-",
    "–": "-",
    "—": "-",
    "―": "-",
    "−": "-",
    "⁃": "-",
    " ": " ",
    " ": " ",
    " ": " ",
    " ": " ",
    " ": " ",
    " ": " ",
    " ": " ",
    "　": " ",
    "\t": " ",
    "­": None,
    "​": None,
    "‌": None,
    "‍": None,
    "﻿": None,
    "…": "...",
}
_HYPHENS = "-‐‑­‒–"


def normalize(src: str) -> tuple[str, list[int]]:
    """Return a normalised view of `src` plus, per normalised char, its source offset.

    Whitespace runs collapse to one space, typographic variants fold to ASCII, zero-width
    characters vanish, and a hyphen at a line break is joined up ("oblig-\\nation" ->
    "obligation"). `offsets[i]` is always a valid index into `src`, which is what lets a
    match be turned back into an exact source slice.
    """
    chars: list[str] = []
    offs: list[int] = []
    i, n = 0, len(src)
    prev_space = True  # leading whitespace is dropped
    while i < n:
        ch = src[i]
        # Line-wrap / PDF hyphenation: drop the hyphen and the break that follows it.
        if ch in _HYPHENS and i + 1 < n and src[i + 1] in "\r\n":
            j = i + 1
            while j < n and src[j].isspace():
                j += 1
            i = j
            prev_space = False
            continue
        rep = _MAP.get(ch, ch)
        if rep is None:
            i += 1
            continue
        for c in rep:
            if c.isspace():
                if not prev_space:
                    chars.append(" ")
                    offs.append(i)
                    prev_space = True
            else:
                chars.append(c)
                offs.append(i)
                prev_space = False
        i += 1
    while chars and chars[-1] == " ":
        chars.pop()
        offs.pop()
    return "".join(chars), offs


def _trim_to(slice_text: str, limit: int) -> str:
    """Shorten a located slice at a sentence or word boundary.

    The result is still a contiguous slice of the source, so it stays verbatim.
    """
    if len(slice_text) <= limit:
        return slice_text
    head = slice_text[:limit]
    for boundary in (". ", "; ", ", "):
        cut = head.rfind(boundary)
        if cut > limit // 2:
            return head[: cut + 1].rstrip()
    cut = head.rfind(" ")
    return (head[:cut] if cut > limit // 2 else head).rstrip()


@dataclass
class GroundedSource:
    """A contract document prepared for citation lookup."""

    original: str
    norm: str
    lower: str
    offsets: list[int]

    @classmethod
    def build(cls, text: str) -> "GroundedSource":
        norm, offs = normalize(text)
        return cls(original=text, norm=norm, lower=norm.lower(), offsets=offs)

    def _slice(self, idx: int, length: int) -> str:
        start = self.offsets[idx]
        end = self.offsets[idx + length - 1] + 1
        return self.original[start:end]

    def locate(self, quote: str) -> str | None:
        """Find `quote` in the source and return it as an exact source slice, or None.

        Tolerates a quote whose edges the model added or lost - a "Clause 4.1 provides
        that" preamble, a trailing half sentence - by shrinking the window from the left,
        and for each start from the right, keeping the longest run that genuinely occurs in
        the source. Never returns text that is not a contiguous slice of the source.
        """
        nq, _ = normalize(quote or "")
        if len(nq) < MIN_QUOTE_CHARS:
            return None

        idx = self.lower.find(nq.lower())
        if idx >= 0:
            return self._slice(idx, len(nq))

        words = nq.split(" ")
        if len(words) < _MIN_SALVAGE_WORDS:
            return None
        floor = max(_MIN_SALVAGE_CHARS, int(len(nq) * _MIN_SALVAGE_COVERAGE))
        budget = _FIND_BUDGET
        for start in range(len(words)):
            for end in range(len(words), start, -1):
                if budget <= 0:
                    return None
                if end - start < _MIN_SALVAGE_WORDS:
                    break
                cand = " ".join(words[start:end])
                if len(cand) < floor:
                    break
                budget -= 1
                hit = self.lower.find(cand.lower())
                if hit >= 0:
                    return self._slice(hit, len(cand))
        return None

    def has_clause_label(self, ref: str) -> bool:
        """True only if this clause reference really appears in the source document.

        Stops an invented "(Clause 7.3)" from being printed next to an otherwise sound
        obligation: either the reference reads literally in the text, or its number is
        introduced there by a clause-like word or stands at the head of a line.
        """
        ref = (ref or "").strip()
        if not ref:
            return False
        nref, _ = normalize(ref)
        if len(nref) >= 3 and nref.lower() in self.lower:
            return True
        num = re.search(r"\d+(?:\.\d+)*[a-z]?|\b[ivxlc]+\b", nref, re.I)
        if not num:
            return False
        tok = re.escape(num.group(0))
        near = (
            r"(?:clause|section|article|paragraph|para|art\.?|sch(?:edule)?|§)"
            rf"\s*\(?{tok}\)?\b"
        )
        if re.search(near, self.lower, re.I):
            return True
        return bool(re.search(rf"(?:^|\n)\s*\(?{tok}\)?[\.\)\s]", self.original, re.I | re.M))


def ground_quotes(
    source: GroundedSource, quotes: list[str], *, max_items: int, max_chars: int
) -> tuple[list[str], int]:
    """Turn candidate quotes into a clean citation list. Returns (clauses, n_dropped).

    Order is preserved, each entry is a verbatim source slice, duplicates and entries
    already contained in a longer citation are removed, and anything unlocatable is counted
    out rather than softened.
    """
    located: list[str] = []
    dropped = 0
    for q in quotes:
        hit = source.locate(q)
        if hit is None:
            dropped += 1
            continue
        located.append(_trim_to(hit.strip(), max_chars))

    out: list[str] = []
    keys: list[str] = []
    for text in located:
        key = normalize(text)[0].lower()
        if not key:
            continue
        if any(key in k for k in keys):
            continue
        # A new, longer citation supersedes any shorter one it already contains.
        for i in range(len(keys) - 1, -1, -1):
            if keys[i] in key:
                keys.pop(i)
                out.pop(i)
        keys.append(key)
        out.append(text)
        if len(out) >= max_items:
            break
    return out, dropped
