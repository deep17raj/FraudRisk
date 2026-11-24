"""Pull the parameters out of a plain-language question sent in place of clean parameters.

A caller (or Telegraph's own spot-check) may put a whole sentence into an endpoint's main field:
"Is Viktor Bout on a sanctions list?", "What is the weather forecast for London over the next 48 hours?". Every function
here returns its input unchanged unless that input looks like a sentence, so a clean value such as "Bank of America
Corporation" or "Rio de Janeiro, Brazil" is never altered. None of them guesses: when nothing is recognised the original
text comes back and the intent answers as it always did.
"""
from __future__ import annotations

import re
from typing import Optional

_OPENERS = (r"what|what's|whats|who|whom|whose|how|when|where|which|why|will|would|can|could|should|is|are|was|were|do|does|"
            r"did|has|have|had|tell|give|show|find|look|lookup|check|screen|verify|search|get|list|count|please|i|we")
_QUESTION = re.compile(rf"^\s*(?:{_OPENERS})\b", re.I)
_SKIP_CAPS = {"what", "what's", "will", "is", "are", "was", "how", "does", "do", "did", "tell", "give", "show", "the", "i",
              "please", "can", "could", "would", "should", "who", "when", "where", "which", "why", "has", "have", "look",
              "find", "check", "screen", "verify", "search", "get", "list", "celsius", "fahrenheit", "utc", "today",
              "tomorrow", "tonight", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
              "january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
              "november", "december", "sec", "edgar", "lei", "sla", "ofac", "sdn", "gleif", "usd", "api", "ok"}


def is_sentence(text: str) -> bool:
    t = (text or "").strip()
    n = len(t.split())
    return (n >= 4 and (t.endswith("?") or bool(_QUESTION.match(t)))) or (n >= 3 and bool(_QUESTION.match(t)))


_ABBREVIATION = re.compile(r"\b(?:inc|corp|ltd|co|llc|lp|llp|bros|jr|sr|s\.a|n\.v|b\.v|p\.l\.c)\.$", re.I)


def _tidy(s: str) -> str:
    out = re.sub(r"\s+", " ", (s or "").strip().strip("\"'`“”‘’,;:!?()[]")).strip()
    return out if _ABBREVIATION.search(out) else out.rstrip(".").strip()


_CAP = r"[A-Z][\w'’.\-&]*"
_LINK = r"(?:of|de|del|la|le|el|da|do|di|du|von|van|upon|on|the|and|y)"
_CAP_RUN = re.compile(rf"{_CAP}(?:[\s,]+(?:{_LINK}\s+)?{_CAP})*")


def capitalized_runs(text: str) -> list[str]:
    """Runs of capitalised words ("New York City", "Paris, France"), ignoring a sentence-opening question word."""
    out = []
    for m in _CAP_RUN.finditer(text or ""):
        run = m.group(0).strip(" ,")
        words = run.split()
        while words and words[0].lower().strip(",") in _SKIP_CAPS:
            words.pop(0)
        while words and words[-1].lower().strip(",") in _SKIP_CAPS:
            words.pop()
        if words:
            out.append(" ".join(words).strip(" ,"))
    return out


# ---- sanctions -------------------------------------------------------------------------------------------
_SANCTIONS = [
    re.compile(r"^(?:please\s+)?(?:is|are|was|were)\s+(?:the\s+)?(?:(?:company|entity|person|individual|organi[sz]ation|firm|"
               r"party|business)\s+)?(?P<n>.+?)\s+(?:currently\s+|presently\s+|still\s+)?"
               r"(?:on|in|listed|sanctioned|under|subject|designated|included|appear|blacklisted|banned|flagged)\b", re.I),
    re.compile(r"^(?:does|do|did)\s+(?:the\s+)?(?P<n>.+?)\s+(?:currently\s+)?(?:appear|show|feature|figure|have|match|"
               r"exist|come\s+up|turn\s+up|get)\b", re.I),
    re.compile(r"^(?:please\s+)?(?:screen|check|search|verify|look\s*up|lookup|run|test)\s+(?:whether\s+|if\s+)?(?:the\s+)?"
               r"(?:(?:name|entity|person|individual|company|party)\s+)?(?P<n>.+?)\s+(?:against|on|in|for|is|are|appears?|"
               r"has|have)\b", re.I),
    re.compile(r"^(?:please\s+)?(?:screen|check|search|verify|look\s*up|lookup)\s+(?P<n>.+?)[\s.?!]*$", re.I),
]


def sanctions_name(text: str) -> str:
    """The party named in "Is X on a sanctions list?" / "Screen X against OFAC"; the input when it is already a name."""
    if not is_sentence(text):
        return text
    for pat in _SANCTIONS:
        m = pat.match(text.strip())
        if m and _tidy(m.group("n")):
            return _tidy(m.group("n"))
    return text


# ---- weather ---------------------------------------------------------------------------------------------
_PLACE_AFTER = re.compile(rf"\b(?:in|for|at|near|around|of|across|within)\s+({_CAP_RUN.pattern})")
_PLACE_SUBJECT = re.compile(rf"^\s*(?:will|is|does|do|can|would|could)\s+({_CAP_RUN.pattern})")


def place_of(text: str) -> str:
    """The place named in a weather question; the input when it is already a place name."""
    if not is_sentence(text):
        return text
    for pat in (_PLACE_AFTER, _PLACE_SUBJECT):
        m = pat.search(text)
        if m:
            runs = capitalized_runs(m.group(1))
            if runs:
                return runs[0]
    runs = capitalized_runs(text)
    return runs[0] if runs else text


def hours_of(text: str) -> Optional[int]:
    """A forecast window written in a question ("next 48 hours", "3 days", "tomorrow", "this week"), else None."""
    s = (text or "").lower()
    m = re.search(r"\b(\d{1,3})\s*[- ]?\s*(?:hours?|hrs?|h)\b", s)
    if m:
        return min(240, max(1, int(m.group(1))))
    m = re.search(r"\b(\d{1,2})\s*[- ]?\s*days?\b", s)
    if m:
        return min(240, max(1, int(m.group(1)) * 24))
    if re.search(r"\b(?:this|next|coming|the)\s+week\b|\bweek\b", s):
        return 168
    if re.search(r"\b(?:tomorrow|tonight|next day|today)\b", s):
        return 24
    return None


# ---- dns -------------------------------------------------------------------------------------------------
_EXPLICIT = re.compile(r"\b(AAAA|CNAME|MX|TXT|SOA|CAA|SRV|PTR)\b", re.I)
_EXPLICIT_UPPER = re.compile(r"\b(NS|A)\b(?=\s+(?:records?|lookup|query))")
_KEYWORDS = (("MX", r"mail\s+(?:server|exchange)|email\s+(?:server|routing)"), ("NS", r"name\s*servers?|delegat"),
             ("TXT", r"\bspf\b|\bdkim\b|\bdmarc\b|text\s+record|verification\s+record"), ("AAAA", r"ipv6"),
             ("CNAME", r"alias|canonical\s+name"), ("PTR", r"reverse\s+(?:dns|lookup)"), ("SOA", r"start\s+of\s+authority"),
             ("A", r"ipv4|ip\s+address|resolve"))


def dns_type_of(text: str) -> Optional[str]:
    """The DNS record type a question asks about ("Does google.com have an MX record?" -> "MX"), else None."""
    t = text or ""
    found = [(m.start(), m.group(1).upper()) for m in _EXPLICIT.finditer(t)]
    found += [(m.start(), m.group(1)) for m in _EXPLICIT_UPPER.finditer(t)]
    if found:
        return min(found)[1]
    for rtype, pat in _KEYWORDS:
        if re.search(pat, t, re.I):
            return rtype
    return None


# ---- periods, shared by SLA and filings ----------------------------------------------------------------
_PERIOD = re.compile(
    r"(?:(?:over|in|during|for|within|across|throughout|since|from)\s+)?(?:the\s+)?"
    r"(?:(?:last|past|previous)\s+(?:\d+\s*)?(?:hours?|days?|weeks?|months?|quarters?|years?)|"
    r"(?:last|this|past)\s+(?:year|quarter|month|week)|all[\s-]*time|year[\s-]to[\s-]date|ytd|"
    r"q[1-4]\s*(?:of\s*)?\d{4}|\d{4}\s*q[1-4]|"
    r"(?:since\s+|from\s+|in\s+|during\s+|after\s+)?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{4}|"
    r"(?:in\s+)?(?:19|20)\d{2}|24\s*hours?)", re.I)


def period_of(text: str) -> tuple[str, str]:
    """(the period phrase, the text without it): "...in the last 12 months" -> ("in the last 12 months", "...")."""
    m = _PERIOD.search(text or "")
    if not m:
        return "", text or ""
    return m.group(0).strip(), (text[:m.start()] + " " + text[m.end():])


_PCT = re.compile(r"\d{1,3}(?:\.\d+)?\s*%")
_FRAME_WORDS = {"did", "does", "do", "has", "have", "had", "is", "was", "were", "are", "the", "its", "their", "it", "meet",
                "meets", "met", "meeting", "hit", "reach", "reached", "exceed", "sla", "uptime", "availability", "of", "over",
                "in", "for", "during", "last", "past", "target", "compliance", "compliant", "service", "status", "what",
                "whats", "how", "much", "many", "a", "an", "and", "to", "at", "on", "about", "with", "by", "from", "tell",
                "me", "please", "was", "be", "show", "give", "check", "whether", "if", "published", "stated", "agreed",
                "sec", "edgar", "filing", "filings", "filed", "file", "files", "make", "made", "submit", "submitted",
                "report", "reports", "reported", "form", "forms", "regulatory", "count", "number", "total", "list",
                "any", "all", "that", "which", "mention", "mentioning", "mentions", "regarding", "concerning", "company",
                "corp", "i", "we", "can", "could", "you", "find", "get", "look", "up", "search"}
_FORMS = re.compile(r"\b(?:\d{1,2}-[KQ]|10-K|10-Q|8-K|S-1|13F|20-F|6-K|DEF\s*14A|Schedule\s*13[DG]|Form\s*\d+\w*)\b", re.I)


def _subject(text: str) -> str:
    """What is left of a sentence once percentages, periods, form names and framing words are removed."""
    s = _PCT.sub(" ", text or "")
    s = _PERIOD.sub(" ", s)
    s = _FORMS.sub(" ", s)
    runs = capitalized_runs(re.sub(r"[?!]", " ", s))
    runs = [r for r in runs if r.lower() not in _FRAME_WORDS]
    if runs:
        return re.sub(r"['’]s$", "", runs[0])
    words = [w for w in re.findall(r"[\w.&'’\-]+", s) if w.lower() not in _FRAME_WORDS]
    return re.sub(r"['’]s$", "", " ".join(words))


def sla_parts(service: str, period: str) -> tuple[str, str]:
    """(service, period) from a question like "Did GitHub meet 99.9% uptime over the last 30 days?"."""
    if not is_sentence(service):
        return service, period
    phrase, _ = period_of(service)
    name = _subject(service)
    return (name or service), (period or phrase)


def filings_parts(entity: str, period: str) -> tuple[str, str]:
    """(entity, period) from "How many SEC filings did Tesla make in the last 12 months?"."""
    if not is_sentence(entity):
        return entity, period
    phrase, _ = period_of(entity)
    name = _subject(entity)
    return (name or entity), (period or phrase)


# ---- corporate registry --------------------------------------------------------------------------------
_REGISTRY_TAIL = (r"(?:\s+(?:in|on|from|at|via|using|within|with)\s+(?:the\s+)?(?:corporate\s+|company\s+|business\s+|global\s+|"
                  r"legal\s+entity\s+)?(?:registry|register|gleif|lei|database|records?).*)?")
_REGISTRY = [
    re.compile(r"^(?:please\s+)?(?:look\s*up|lookup|find|search(?:\s+for)?|get|show(?:\s+me)?|check|retrieve)\s+(?:the\s+)?"
               r"(?:(?:registry\s+(?:record|entry|details?)|registration\s+(?:record|details?)|company|corporate\s+record|"
               r"lei(?:\s+record)?)\s+(?:for|of)\s+|company\s+)?(?P<n>.+?)" + _REGISTRY_TAIL + r"[\s?!]*$", re.I),
    re.compile(r"\b(?:lei|legal\s+entity\s+identifier|legal\s+name|registered\s+address|incorporation\s+date|"
               r"date\s+of\s+incorporation|jurisdiction|registration\s+(?:number|id|details?))\s+(?:of|for)\s+"
               r"(?P<n>.+?)" + _REGISTRY_TAIL + r"[\s?!]*$", re.I),
    re.compile(r"^(?:is|was)\s+(?P<n>.+?)\s+(?:an?\s+)?(?:active|registered|incorporated|a\s+registered|a\s+legal)\b", re.I),
    re.compile(r"^(?:what|where|when)\s+(?:is|was)\s+(?P<n>.+?)(?:'s|’s)\s+(?:lei|legal|registered|incorporat|jurisdiction)", re.I),
]


def registry_company(text: str) -> str:
    """The company named in "Look up Microsoft Corporation in the corporate registry"; the input when already a name."""
    if not is_sentence(text):
        return text
    for pat in _REGISTRY:
        m = pat.search(text.strip())
        if m and _tidy(m.group("n")):
            return _tidy(m.group("n"))
    return text
