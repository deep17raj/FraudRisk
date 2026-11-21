"""Stage 1 - claim parsing.

Extracts the checkable assertion plus entities / numbers / years, and short-circuits claims that
are not factual assertions (opinions, predictions, open questions, fragments). Those never reach
retrieval: the pipeline answers `Unverified` with an explanatory reason instead of guessing.
"""
from __future__ import annotations

import datetime as _dt
import re

from .models import ParsedClaim
from .text_utils import STOPWORDS, content_terms, has_negation, parse_numbers

MAX_CLAIM_CHARS = 600

_PREFIXES = re.compile(
    r"^\s*(?:fact[- ]?check(?:\s*this)?\s*[:\-]|claim\s*[:\-]|true or false\s*[:\-?]?|"
    r"is it (?:true|correct|accurate) (?:that|to say that)|is it a fact that|"
    r"did you know (?:that)?|is the claim that|someone (?:said|told me|claims?) (?:that)?|"
    r"i (?:heard|read|saw) (?:that)?|people (?:say|claim) (?:that)?)\s*",
    re.I,
)

_OPEN_QUESTION = re.compile(r"^\s*(what|who|whom|whose|when|where|why|how|which)\b", re.I)
_YESNO_QUESTION = re.compile(
    r"^\s*(is|are|was|were|do|does|did|can|could|has|have|had|will|would|should)\b", re.I
)

# Subjective / evaluative language. A claim built on these without hard facts is an opinion.
_OPINION = re.compile(
    r"\b(i think|i believe|i feel|in my opinion|imo|should|ought to|deserves?|"
    r"best(?!-?sell)|worst|greatest|prettiest|ugliest|better than|worse than|overrated|underrated|"
    r"beautiful|ugly|delicious|tasty|boring|awesome|amazing|terrible|horrible|wonderful|lovely|"
    r"favou?rite|nicest|coolest|smartest|dumbest|most (?:beautiful|annoying|important|overrated)|"
    r"i love|i hate)\b",
    re.I,
)

# Lowercase-only and NOT case-insensitive: "Will Smith" is a name, not a prediction.
_FUTURE = re.compile(r"\b(will|going to|shall)\b")
# Forecast phrasings that are predictive on their own (they carry their own future verb).
_PREDICTION = re.compile(
    r"\b(is expected to|are expected to|is predicted to|are predicted to|is likely to|are likely to"
    r"|is forecast to|are forecast to|will probably|will likely|is projected to|are projected to)\b"
)
# Future TIME markers. These are predictive only alongside _FUTURE: "passed $30tn by 2022" and
# "he resigned the next year" are statements about the past that happen to name a time.
_FUTURE_TIME = re.compile(
    r"\b(next (?:year|month|week|decade|season|quarter)|by (?:the end of )?20\d\d"
    r"|within (?:the next )?\d+ years?)\b"
)
# A past-tense/reporting anchor means the sentence asserts something that already happened, so a
# bare "will" inside it is reported speech or a noun ("the will of Alfred Nobel"), not a forecast.
_PAST_ANCHOR = re.compile(
    r"\b(was|were|had|has|have|did|announced|said|stated|signed|established|founded|died|won|lost"
    r"|ruled|confirmed|reported|filed|published|agreed|voted|passed|launched|became|revealed"
    r"|pledged|promised|wrote|created|invented|discovered|is|are)\b",
    re.I,
)
# A scheduled/announced future event is a checkable present fact ("the 2028 Olympics will be held in LA").
_SCHEDULED = re.compile(
    r"\b(will be held|will take place|will be hosted|will host|will open|will premiere|will begin|will start"
    r"|will be released|will be published|is scheduled|are scheduled|has been scheduled|was scheduled"
    r"|is set to|are set to|has been announced|was announced)\b"
)
# Mask non-sentence-initial capitalised words before testing for judgement words, so that
# "Best Picture" / "The Best Man" read as names rather than as someone's opinion.
_MASK_NAMES = re.compile(r"(?<=[^.!?]\s)[A-Z][\w'\-]*")

_TIME_SENSITIVE = re.compile(
    r"\b(today|tonight|yesterday|tomorrow|this (?:week|month|year|morning|weekend|season)|"
    r"last (?:night|week)|right now|currently|at the moment|breaking|just (?:announced|happened|"
    r"released|passed|signed)|latest|recent(?:ly)?|current|now|as of|so far this)\b",
    re.I,
)

_ENTITY = re.compile(
    r"(?:[A-Z][\w'\-\.]*(?:\s+(?:of|the|de|la|von|van|and|&)\s+|\s+)?)+", re.U
)


def normalize_claim(text: str) -> str:
    """Whitespace/quote normalisation + prefix stripping. Pure and idempotent."""
    t = (text or "").replace("“", '"').replace("”", '"').replace("’", "'")
    t = re.sub(r"\s+", " ", t).strip()
    prev = None
    while prev != t:
        prev = t
        t = _PREFIXES.sub("", t).strip()
        if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
            t = t[1:-1].strip()
    return t


def cache_key_text(text: str) -> str:
    """Normalised form used as the cache key: case/punctuation/spacing-insensitive."""
    t = normalize_claim(text).lower()
    t = re.sub(r"[^\w\s%]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _extract_entities(text: str) -> list[str]:
    ents: list[str] = []
    for m in _ENTITY.finditer(text):
        e = m.group(0).strip(" .,'-")
        if not e:
            continue
        # A lone capitalised first word is usually just sentence case ("The", "Water").
        if m.start() == 0 and " " not in e and e.lower() in {"the", "a", "an", "this", "that", "it"}:
            continue
        if m.start() == 0 and " " not in e and not e.isupper():
            # sentence-initial single word: keep only if it re-appears capitalised elsewhere
            if len(re.findall(rf"\b{re.escape(e)}\b", text)) < 2:
                continue
        ents.append(e)
    seen, out = set(), []
    for e in ents:
        k = e.lower()
        if k not in seen:
            seen.add(k)
            out.append(e)
    return out


def parse_claim(raw: str, *, today: _dt.date | None = None) -> ParsedClaim:
    today = today or _dt.date.today()
    original = (raw or "").strip()
    text = normalize_claim(original)[:MAX_CLAIM_CHARS]

    def reject(reason: str) -> ParsedClaim:
        return ParsedClaim(original=original, text=text, checkable=False, reject_reason=reason)

    if not text or not re.search(r"[A-Za-z0-9]", text):
        return reject("No claim was provided, so there is nothing to check.")

    words = re.findall(r"[A-Za-z0-9]+", text)
    if len(words) < 3:
        return reject(
            "The input is too short to be a verifiable factual assertion; a checkable claim needs a "
            "subject and a specific statement about it."
        )

    nums = parse_numbers(text)
    years = [int(n.value) for n in nums if n.kind == "year"]
    has_hard_facts = bool(nums)

    if text.endswith("?") and _OPEN_QUESTION.match(text) and not _YESNO_QUESTION.match(text):
        return reject(
            "This is an open question rather than a factual assertion, so there is no claim to rate "
            "as true or false. Restate it as a specific statement to have it checked."
        )

    opinion_hits = _OPINION.findall(_MASK_NAMES.sub(" ", text))
    if opinion_hits and not has_hard_facts:
        return reject(
            "This is a subjective or evaluative statement (it turns on judgement, not on facts that "
            "can be verified), so it cannot be rated as true or false."
        )

    # Predictions can't be verified. Reject only on a POSITIVE forecast marker (a future year, "next
    # year", "likely to", ...), or on a bare "will"/"shall" with no past-tense anchor to make it a
    # report of something already said or done. Scheduled events stay checkable.
    future_year = any(y > today.year for y in years)
    forecast = bool(_PREDICTION.search(text)) or (
        bool(_FUTURE.search(text)) and (future_year or bool(_FUTURE_TIME.search(text)))
    )
    bare_future = _FUTURE.search(text) and not _PAST_ANCHOR.search(text)
    if (forecast or bare_future) and not _SCHEDULED.search(text):
        return reject(
            "This is a prediction about the future. Events that have not happened yet cannot be "
            "verified, so it cannot be rated as true or false."
        )

    entities = _extract_entities(text)
    keywords = content_terms(text)
    if len(keywords) < 2 and not entities:
        return reject("The statement is too vague to check: it does not name anything specific.")

    time_sensitive = bool(_TIME_SENSITIVE.search(text)) or today.year in years

    q_clean = re.sub(r"\?+$", "", text).strip()
    queries = [q_clean[:300]]
    # Search engines (Wikipedia's especially) rank keyword queries far better than full sentences.
    kw_q = " ".join(w for w in re.findall(r"[A-Za-z0-9%\-']+", q_clean) if w.lower() not in STOPWORDS)[:200]
    if kw_q and kw_q.lower() != q_clean.lower().rstrip(".") and len(kw_q.split()) >= 2:
        queries.append(kw_q)
    ent_q = " ".join(entities[:3])
    if ent_q and len(ent_q.split()) >= 2 and ent_q.lower() not in {q.lower() for q in queries}:
        queries.append(ent_q[:200])

    return ParsedClaim(
        original=original,
        text=q_clean,
        checkable=True,
        entities=entities,
        numbers=[f"{n.value:g}{'%' if n.kind == 'pct' else ''}" for n in nums],
        years=years,
        keywords=keywords,
        negated=has_negation(text),
        time_sensitive=time_sensitive,
        search_queries=queries,
    )
