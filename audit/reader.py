"""Deterministic reading of the contract text: no model involved.

Two jobs, both of which run on every request:

1. `find_unsupplied_documents` - the evidence behind an `Unclear` verdict. A contract that
   hands its real terms to a schedule nobody supplied cannot be audited, and saying so is
   the honest answer. This is a regex over dependency phrasing ("as set out in Schedule A")
   paired with a check for whether that document actually appears as a heading in the text,
   so the finding is backed by the source rather than by the model's say-so.

2. `modal_obligations` - the fallback extractor. With no API key, or after a timeout, a
   refusal or a truncated reply, the service still answers from the text itself: sentences
   carrying an obligation modal ("shall", "must", "agrees to") are real contract wording, so
   they ground trivially and give honest, if coarser, obligations and citations.
"""
from __future__ import annotations

import re

from .models import Obligation, Status

_DOC_KINDS = r"Schedule|Exhibit|Annex|Annexure|Appendix|Attachment|Addendum|Statement\s+of\s+Work|SOW"
_DOC_ID = r"[A-Z0-9][A-Za-z0-9.\-]{0,12}"

#: "as set out in Schedule A", "in accordance with Exhibit 2" - the text *depends* on it.
_DEPENDENCY_RE = re.compile(
    r"(?:as\s+(?:set\s+out|set\s+forth|specified|described|provided|listed|defined|detailed)"
    r"(?:\s+\w+){0,2}?\s+in|in\s+accordance\s+with|pursuant\s+to|subject\s+to|"
    r"attached\s+(?:hereto\s+)?as|set\s+forth\s+in|as\s+per|governed\s+by|"
    r"more\s+particularly\s+described\s+in)\s+"
    rf"(?:the\s+)?(?P<doc>(?:{_DOC_KINDS})(?:\s+{_DOC_ID})?)",
    re.I,
)
#: An explicit admission that the document is not here.
_NOT_ATTACHED_RE = re.compile(
    rf"(?P<doc>(?:{_DOC_KINDS})(?:\s+{_DOC_ID})?)\s*\(?\s*(?:not\s+(?:attached|included|"
    r"provided|supplied)|to\s+be\s+(?:agreed|provided|attached|supplied|determined))",
    re.I,
)

_SENT_SPLIT = re.compile(r"(?<=[.!?;])\s+(?=[A-Z0-9(\"'“])")
#: A contract is line-oriented: a blank line, a numbered clause head or an all-caps heading
#: each start a new block. Splitting on sentence enders alone glues a title onto the first
#: clause ("SERVICES AGREEMENT 1.1 The Supplier shall ..."), which then reads as the party.
_BLOCK_SPLIT = re.compile(
    r"\n\s*\n"
    r"|\n(?=\s*(?:\d+(?:\.\d+)*[a-z]?|\([a-z]\)|\([ivxlc]+\))[\.\)]\s)"
    r"|\n(?=\s*(?:ARTICLE|CLAUSE|SECTION|SCHEDULE|EXHIBIT|ANNEX|APPENDIX|ATTACHMENT)\b)"
)
#: A line in all caps is a heading. It is forced into a block of its own before splitting,
#: because a variable-width look-behind cannot express "the line that just ended was a
#: heading" - and without it the title glues onto the first clause and reads as the party.
_HEADING_LINE = re.compile(r"^([A-Z][A-Z \t&'’,.\-]{5,})[ \t]*$", re.M)
#: Belt and braces for a heading that shares its line with the clause that follows it.
_LEADING_HEADING = re.compile(r"^[A-Z][A-Z \t&'’,.\-]{5,}?(?=\s+\d|\s+[A-Z][a-z])")


def _blocks(text: str) -> list[str]:
    """Split a contract into clause-sized blocks, then into sentences within each block."""
    out: list[str] = []
    marked = _HEADING_LINE.sub(lambda m: m.group(1) + "\n", text)
    for block in _BLOCK_SPLIT.split(marked):
        if not block or not block.strip():
            continue
        for sent in _SENT_SPLIT.split(block):
            if sent and sent.strip():
                out.append(sent.strip())
    return out
_MODAL_RE = re.compile(
    r"\b(shall\s+not|shall|must\s+not|must|may\s+not|agrees\s+to|undertakes\s+to|"
    r"covenants\s+to|is\s+required\s+to|are\s+required\s+to|is\s+responsible\s+for|"
    r"are\s+responsible\s+for|will\s+not|will)\b",
    re.I,
)
_DEADLINE_RE = re.compile(
    r"(?:(?:on\s+or\s+)?(?:by|before|no\s+later\s+than|not\s+later\s+than|on)\s+"
    r"(?:the\s+)?(?:\d{1,2}\s+\w+\s+\d{4}|\w+\s+\d{1,2},?\s+\d{4}|\d{4}-\d{2}-\d{2}|"
    r"\d{1,2}/\d{1,2}/\d{2,4})"
    r"|within\s+\w+(?:\s*\(\d+\))?\s+(?:calendar\s+|business\s+|working\s+)?"
    r"(?:days|weeks|months|years|hours)"
    r"|(?:each|every|per)\s+(?:month|quarter|year|week)"
    r"|monthly|quarterly|annually)",
    re.I,
)
_CLAUSE_HEAD_RE = re.compile(
    r"(?:^|\n)\s*(?:(?:clause|section|article|paragraph)\s+)?"
    r"(?P<ref>\d+(?:\.\d+){0,3}[a-z]?|\([a-z]\)|\([ivxlc]+\))[\.\)\s]",
    re.I,
)


def find_unsupplied_documents(text: str) -> list[str]:
    """Documents the contract leans on that are not present in the text we were given."""
    found: dict[str, str] = {}
    for m in list(_DEPENDENCY_RE.finditer(text)) + list(_NOT_ATTACHED_RE.finditer(text)):
        doc = re.sub(r"\s+", " ", m.group("doc")).strip(" .,;:")
        found.setdefault(doc.lower(), doc)

    missing: list[str] = []
    for key, doc in found.items():
        # Supplied if the document appears as its own heading somewhere in the text, rather
        # than only in the sentence that points at it.
        heading = re.compile(rf"(?:^|\n)\s*(?:{re.escape(doc)})\b[\s:.—-]*(?:\n|$|[A-Z\"'])",
                             re.I)
        if heading.search(text):
            continue
        missing.append(doc)
    return missing[:6]


def _clause_ref_at(text: str, pos: int) -> str:
    """The nearest clause number heading at or above `pos`, or "" if there is none."""
    best = ""
    for m in _CLAUSE_HEAD_RE.finditer(text, 0, pos + 1):
        best = m.group("ref")
    return best


def modal_obligations(text: str, limit: int = 10) -> list[Obligation]:
    """Obligation-bearing sentences, read straight out of the contract.

    Every `quote` is a sentence taken verbatim from the text, so citation grounding is
    guaranteed to succeed. Status is always `unverifiable`: this path reads duties, it does
    not judge performance, and claiming otherwise would be the fabrication we exist to avoid.
    """
    out: list[Obligation] = []
    pos = 0
    for sent in _blocks(text):
        at = text.find(sent, pos)
        if at >= 0:
            pos = at
        if len(sent) < 25 or len(sent) > 1200:
            continue
        m = _MODAL_RE.search(sent)
        if not m:
            continue
        party = sent[: m.start()].strip(" \t,;:-—")
        party = re.sub(r"^(?:\d+(?:\.\d+)*[a-z]?[\.\)]?\s*)+", "", party).strip()
        party = _LEADING_HEADING.sub("", party).strip()
        party = re.sub(r"^(?:\d+(?:\.\d+)*[a-z]?[\.\)]?\s*)+", "", party).strip()
        party = re.sub(r"\s+", " ", party)
        if not party or len(party) > 120:
            party = "The contract"
        duty = re.sub(r"\s+", " ", f"{m.group(1).lower()} {sent[m.end():].strip()}").strip(" .;,")
        if not duty:
            continue
        deadline = _DEADLINE_RE.search(sent)
        out.append(Obligation(
            party=party,
            duty=duty[:300],
            status=Status.UNVERIFIABLE,
            deadline=(deadline.group(0) if deadline else ""),
            clause_ref=_clause_ref_at(text, at if at >= 0 else 0),
            reason="read from the contract wording; performance is not evidenced in the text",
            quote=sent,
        ))
        if len(out) >= limit:
            break
    return out
