"""Turn retrieved documents into an answer without a model, and vet an answer a model wrote.

Nothing a model says is trusted as-is: its source numbers must point at documents we actually retrieved, any URL
it wrote that we did not supply is removed, and an answer that is not substantive is discarded in favour of the
extractive one built here.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from .retrieval import Doc
from .text import best_sentences, clip

_URL = re.compile(r"https?://[^\s)\]>\"']+")
_CITE = re.compile(r"\[(\d{1,2})\]")


def strip_foreign_urls(text: str, allowed: set[str]) -> str:
    return _URL.sub(lambda m: m.group(0) if m.group(0).rstrip(".,;") in allowed else "", text).strip()


def valid_answer(obj: Optional[dict], key: str, docs: list[Doc], *, min_chars: int = 20, max_chars: int = 1400,
                 require_citation: bool = True) -> Optional[tuple[str, list[Doc]]]:
    """(answer text, the documents it relied on) from a model's JSON object, or None when it is not usable."""
    if not obj or not isinstance(obj.get(key), str):
        return None
    text = obj[key].strip()
    used_raw: Any = obj.get("used")
    used = sorted({int(i) for i in used_raw if isinstance(i, (int, float, str)) and str(i).isdigit()
                   and 1 <= int(i) <= len(docs)}) if isinstance(used_raw, list) else []
    if not used:                                          # fall back to the citations written in the text itself
        used = sorted({int(n) for n in _CITE.findall(text) if 1 <= int(n) <= len(docs)})
    if require_citation and not used:
        return None
    text = strip_foreign_urls(text, {d.url for d in docs})
    if len(text) < min_chars:
        return None
    return clip(text, max_chars), [docs[i - 1] for i in used]


def is_headline(d: Doc) -> bool:
    """A news item: its text is just its headline, so it carries a claim worth reporting but no explanation."""
    return d.snippet == d.title or len(d.snippet) < 60


def prefer_reference(docs: list[Doc], recent: bool, max_headlines: int = 3) -> list[Doc]:
    """For a 'latest' question headlines lead, newest first. Otherwise explanatory documents lead and headlines only
    top the list up, because a headline cannot answer "who created Python" the way an encyclopedia entry does."""
    heads = [d for d in docs if is_headline(d)]
    refs = [d for d in docs if not is_headline(d)]
    if recent:
        return sorted(heads, key=lambda d: d.date, reverse=True)[:max_headlines + 2] + refs
    return refs + heads[:max_headlines]


def extractive(query: str, docs: list[Doc], *, per_doc: int = 1, max_docs: int = 3,
               max_chars: int = 900) -> tuple[str, list[Doc]]:
    """An answer assembled from the most relevant sentence(s) of the best documents, each attributed to its source.

    Headline-only documents are listed as "Recent headlines" after the explanatory text, never passed off as prose.
    """
    facts: list[str] = []
    heads: list[str] = []
    used: list[Doc] = []
    for d in docs:
        if len(used) >= max_docs:
            break
        when = f", {d.date}" if d.date else ""
        if is_headline(d):
            heads.append(f"{clip(d.title, 140)} ({d.source}{when})")
            used.append(d)
            continue
        picked = best_sentences(query, [d.snippet], n=per_doc, max_chars=380) or [clip(d.snippet, 300)]
        facts.append(f"{' '.join(picked)} ({d.source}{when})")
        used.append(d)
    parts = facts + ([f"Recent headlines: {'; '.join(heads)}."] if heads else [])
    return clip(" ".join(parts), max_chars), used
