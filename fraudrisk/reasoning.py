"""Stage 3 - the reasoning layer, and stage 6 - summary synthesis.

The model receives ONLY the subject and the signals the sources actually gathered. Its job is
the analyst's job: decide which of those signals genuinely bear on THIS subject, how strongly
each one does so in context, name the fraud pattern they collectively match, and write the
one-or-two sentence summary.

What it cannot do is add evidence. The structured-output schema gives it no field for new
findings - only signal ids drawn from the supplied list, a strength from a closed set, and a
pattern from a closed set. Ids it invents are dropped in code, and the summary is validated
against the signal text before it is used. The score itself is never the model's to set:
that is `scoring.compute_score`, which runs on the annotated signals afterwards.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field

from .llm import LLMClient, LLMUnavailable
from .models import ParsedSubject, Signal, Strength
from .patterns import PATTERN_GLOSS, Pattern, parse_pattern

log = logging.getLogger("fraudrisk.reasoning")

#: Generous ceiling: thinking cannot be disabled on current models, so the reply needs room
#: for reasoning tokens plus the JSON. Truncation would otherwise fall back on every call.
REASONING_MAX_TOKENS = 8000

_STRENGTHS = [s.value for s in Strength]
_PATTERNS = [p.value for p in Pattern]

SCHEMA = {
    "type": "object",
    "properties": {
        "signals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "A signal id copied exactly from the list."},
                    "applies": {"type": "boolean",
                                "description": "False if this signal does not genuinely bear on this subject."},
                    "strength": {"type": "string", "enum": _STRENGTHS},
                    "note": {"type": "string",
                             "description": "At most 12 words on why it applies this strongly. May be empty."},
                },
                "required": ["id", "applies", "strength", "note"],
                "additionalProperties": False,
            },
        },
        "pattern": {"type": "string", "enum": _PATTERNS},
        "summary": {"type": "string",
                    "description": "One or two sentences naming the decisive signals and the pattern."},
    },
    "required": ["signals", "pattern", "summary"],
    "additionalProperties": False,
}

SYSTEM = """You are a senior payments fraud analyst reviewing one subject.

You are given the subject text and a numbered list of SIGNALS that independent sources have
already gathered. Your task has three parts:

1. For each signal, decide whether it genuinely bears on THIS subject, and how strongly it
   does so in context: weak, moderate or strong. A signal can be factually true and still be
   weak evidence here (for example, a hosting-range IP matters far less for a server-to-server
   API call than for a consumer login). Mark a signal `applies: false` when it is real but
   irrelevant to this subject. Benign observations stay in the list: mark them applies true
   with strength weak if they genuinely describe this subject.
2. Name the single fraud pattern the applicable signals collectively match, chosen from the
   allowed list. Use "no identifiable pattern" when the signals do not form one.
3. Write a summary of one or two sentences, as a fraud analyst would: name the decisive
   signals concretely and say what pattern they match.

Hard rules:
- You may ONLY reference signal ids from the list given to you. Never invent a signal, a
  number, a country, an amount or a source that is not in the subject or the signal text.
- Do NOT state or imply a risk level or a score in the summary. The grade is computed
  separately from your annotations; a level in your text would contradict it.
- If the signals are thin or only benign, say so plainly. An honest "little to go on" is
  correct and valuable; inventing a red flag to sound confident is a serious error.
- No markdown, no preamble, no hedging boilerplate. Plain declarative sentences."""

#: Level words the summary must not contain - the computed level is the only grade.
_LEVEL_WORDS = re.compile(
    r"\b(low|medium|moderate|high|critical|severe|elevated)\s+(?:fraud\s+)?"
    r"(?:risk|score|rating|grade|level|concern)\b|\brisk\s+(?:is|level\s+is|score\s+is)\s+"
    r"(?:low|medium|moderate|high|critical)\b", re.I)

#: Numbers in the summary must come from the subject or a signal, never from the model.
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


@dataclass
class Reasoning:
    signals: list[Signal]
    pattern: Pattern = Pattern.NONE
    summary: str = ""
    mode: str = "deterministic"            # llm | deterministic | llm_failed:<why>
    dropped_ids: list[str] = field(default_factory=list)
    notes: dict[str, str] = field(default_factory=dict)


def _render_signals(signals: list[Signal]) -> str:
    lines = []
    for s in signals:
        kind = "benign observation" if s.benign else "risk signal"
        lines.append(f"- id={s.id} [{kind}; source={s.source}; family={s.family.value}] {s.text}")
    return "\n".join(lines)


def _numbers_in(text: str) -> set[str]:
    return {n.replace(",", "") for n in _NUMBER.findall(text or "")}


def validate_summary(summary: str, subject: str, signals: list[Signal]) -> tuple[bool, str]:
    """Reject a summary that grades the subject or cites a number nobody supplied."""
    s = (summary or "").strip()
    if not s:
        return False, "empty"
    if len(s) > 600:
        return False, "too_long"
    if _LEVEL_WORDS.search(s):
        return False, "states_risk_level"
    allowed = _numbers_in(subject) | {n for sig in signals for n in _numbers_in(sig.text)}
    # tolerate small ordinals the model may use to count signals ("two signals")
    allowed |= {"1", "2", "3", "4", "5"}
    for n in _numbers_in(s):
        if n not in allowed:
            return False, f"unsupported_number:{n}"
    return True, "ok"


def _join(parts: list[str]) -> str:
    parts = [p for p in parts if p]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def template_summary(signals: list[Signal], pattern: Pattern, *,
                     insufficient: bool = False, degraded: bool = False,
                     checked_clean: bool = False) -> str:
    """Deterministic summary, used when the model is unavailable or its output is rejected.

    The three LOW outcomes read differently on purpose: nothing to assess, checked and clean,
    and could-not-check are genuinely different statements about the evidence.
    """
    if degraded:
        return ("The external signal sources could not be reached in time, so this subject was "
                "assessed only on what its own description supports; no risk signal was "
                "confirmed and the assessment should be treated as provisional.")
    if insufficient:
        return ("The subject provides no checkable identifier and no concrete behavioural "
                "detail, so there is insufficient information to identify any fraud signal; "
                "this is an absence of evidence, not evidence of safety.")

    risky = [s for s in signals if s.weight > 0]
    benign = [s for s in signals if s.benign or s.weight == 0]

    if checked_clean or not risky:
        if benign:
            shorts = _join([s.short for s in benign[:3]])
            more = (f", plus {len(benign) - 3} further benign observation"
                    f"{'s' if len(benign) > 4 else ''}") if len(benign) > 3 else ""
            return (f"Checks found {shorts}{more}; no signal associated with a known fraud "
                    f"pattern applies to this subject.")
        return ("No risk signal applies to this subject on the evidence gathered, so it is "
                "graded at the floor rather than on any positive indication of safety.")

    # One or two sentences, as the intent specifies: the decisive signals named compactly,
    # then the pattern they match.
    top = risky[:3]
    shorts = _join([s.short for s in top])
    rest = len(risky) - len(top)
    tail = f", alongside {rest} further risk signal{'s' if rest > 1 else ''}" if rest > 0 else ""
    gloss = PATTERN_GLOSS.get(pattern, PATTERN_GLOSS[Pattern.NONE])
    plural = len(top) > 1 or rest > 0
    lead = "The decisive signals are" if plural else "The decisive signal is"
    if pattern is Pattern.NONE:
        closing = ("These are adverse but do not together form one recognised fraud pattern."
                   if plural else
                   "It is adverse but does not on its own match a recognised fraud pattern.")
        return f"{lead} {shorts}{tail}. {closing}"
    link = "which taken together are" if plural else "which is"
    return f"{lead} {shorts}{tail}, {link} {gloss}."


class ReasoningService:
    def __init__(self, llm: LLMClient, mode: str = "auto") -> None:
        self.llm = llm
        self.mode = mode

    @property
    def uses_llm(self) -> bool:
        return self.mode != "off" and self.llm.available

    async def reason(self, parsed: ParsedSubject, signals: list[Signal], timeout: float,
                     *, degraded: bool = False) -> Reasoning:
        """Annotate the gathered signals and write the summary.

        On every failure path the deterministic result is returned instead, built from the
        SAME signals at their source defaults - so the computed band does not move when the
        model is slow, missing or wrong, only the wording does.
        """
        from .sources.rules import vote_pattern

        if not signals:
            return Reasoning(signals=[], pattern=Pattern.NONE,
                             summary=template_summary([], Pattern.NONE,
                                                      insufficient=not degraded, degraded=degraded),
                             mode="deterministic")

        if not self.uses_llm or timeout <= 0.3:
            pattern = vote_pattern(signals)
            return Reasoning(signals=signals, pattern=pattern,
                             summary=template_summary(signals, pattern, degraded=degraded),
                             mode="deterministic")

        user = (f"SUBJECT:\n{parsed.original}\n\n"
                f"SIGNALS GATHERED ({len(signals)}):\n{_render_signals(signals)}\n\n"
                f"Allowed patterns: {', '.join(_PATTERNS)}\n"
                f"Annotate every signal id above, name the pattern, and write the summary.")
        try:
            data = await self.llm.complete_json(SYSTEM, user, SCHEMA,
                                                max_tokens=REASONING_MAX_TOKENS, timeout=timeout)
            return self._apply(data, parsed, signals, degraded=degraded)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # LLMUnavailable is the expected shape, but ANY failure in the reasoning stage
            # must degrade to the deterministic answer rather than fail the assessment: the
            # signals are already gathered and the score does not depend on the model.
            level = log.warning if isinstance(exc, LLMUnavailable) else log.exception
            level("reasoning fell back", extra={"fields": {
                "event": "llm_fallback", "reason": f"{type(exc).__name__}: {exc}"}})
            pattern = vote_pattern(signals)
            return Reasoning(signals=signals, pattern=pattern,
                             summary=template_summary(signals, pattern, degraded=degraded),
                             mode=f"llm_failed:{type(exc).__name__}")

    def _apply(self, data: dict, parsed: ParsedSubject, signals: list[Signal],
               *, degraded: bool) -> Reasoning:
        from .sources.rules import vote_pattern

        by_id = {s.id: s for s in signals}
        dropped: list[str] = []
        notes: dict[str, str] = {}
        touched: set[str] = set()

        for item in data.get("signals") or []:
            if not isinstance(item, dict):
                continue
            sid = str(item.get("id", "")).strip()
            sig = by_id.get(sid)
            if sig is None:
                if sid:
                    dropped.append(sid)      # the model invented an id: ignore it entirely
                continue
            touched.add(sid)
            applies = item.get("applies")
            if applies is False:
                # A `verified` signal is a checked fact about this exact entity (a live
                # phishing-feed hit, membership of the Tor exit list). The model may judge it
                # less important in context - that is what `strength` is for - but it cannot
                # delete the evidence, so dismissal is refused and floored at weak instead.
                if sig.verified:
                    sig.strength = Strength.WEAK
                    sig.note = "model judged this verified fact weak in context"
                    notes[sid] = sig.note
                    dropped.append(f"{sid}:dismissal_refused")
                    continue
                sig.applicable = False
            raw_strength = str(item.get("strength", "")).strip().lower()
            if raw_strength in _STRENGTHS:
                sig.strength = Strength(raw_strength)
            note = str(item.get("note", "") or "").strip()[:140]
            if note:
                sig.note = note
                notes[sid] = note

        pattern = parse_pattern(data.get("pattern"))
        # A pattern with no applicable risk signal behind it would be an unsupported claim.
        if pattern is not Pattern.NONE and not any(s.weight > 0 for s in signals):
            pattern = Pattern.NONE

        summary = str(data.get("summary") or "").strip()
        ok, why = validate_summary(summary, parsed.original, signals)
        mode = "llm"
        if not ok:
            log.warning("summary rejected", extra={"fields": {
                "event": "summary_rejected", "reason": why, "summary": summary[:300]}})
            fallback_pattern = pattern if pattern is not Pattern.NONE else vote_pattern(signals)
            summary = template_summary(signals, fallback_pattern, degraded=degraded)
            pattern = fallback_pattern
            mode = f"llm_summary_rejected:{why}"

        return Reasoning(signals=signals, pattern=pattern, summary=summary, mode=mode,
                         dropped_ids=dropped, notes=notes)
