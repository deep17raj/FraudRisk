"""The reasoning layer's contract with the model.

The model is allowed to select and annotate; it is not allowed to add evidence, set the
score, or grade the subject. These tests pin every one of those boundaries, including the
behaviour when the model fails, times out, refuses or answers with nonsense.
"""
from __future__ import annotations

import pytest

from fraudrisk.classify import classify
from fraudrisk.llm import LLMUnavailable, extract_json
from fraudrisk.models import Family, Strength
from fraudrisk.patterns import Pattern
from fraudrisk.reasoning import (SCHEMA, ReasoningService, template_summary, validate_summary)
from fraudrisk.scoring import compute_score
from tests.conftest import FakeLLM, echo_llm, sig

SUBJECT = classify("New account, VPN login, first transfer of $4,200 to a new payee")


def two_signals():
    return [sig(id="behav.anonymiser", severity=0.45, reliability=0.85, family=Family.ANONYMITY),
            sig(id="behav.first_transfer_new_payee", severity=0.5, reliability=0.85,
                family=Family.BEHAVIOR)]


# --------------------------------------------------------------- the model cannot add evidence

async def test_invented_signal_ids_are_dropped():
    llm = echo_llm(extra_ids=("totally.made.up", "phish.feed_hit"))
    r = await ReasoningService(llm).reason(SUBJECT, two_signals(), timeout=5.0)
    assert set(r.dropped_ids) == {"totally.made.up", "phish.feed_hit"}
    assert {s.id for s in r.signals} == {"behav.anonymiser", "behav.first_transfer_new_payee"}


async def test_an_invented_id_cannot_influence_the_score():
    """A fabricated high-severity id must not change the grade at all."""
    clean = compute_score(two_signals())
    llm = echo_llm(extra_ids=("phish.feed_hit",), strength="moderate")
    r = await ReasoningService(llm).reason(SUBJECT, two_signals(), timeout=5.0)
    assert compute_score(r.signals).score == clean.score


async def test_model_may_mark_a_signal_inapplicable_and_that_lowers_the_score():
    class Dismissive(FakeLLM):
        async def complete_json(self, system, user, schema, *, max_tokens, timeout):
            return {"signals": [{"id": "behav.anonymiser", "applies": False,
                                 "strength": "weak", "note": "server-to-server call"}],
                    "pattern": "no identifiable pattern",
                    "summary": "The anonymised origin does not bear on a server-side call."}

    signals = two_signals()
    before = compute_score(signals).score
    r = await ReasoningService(Dismissive()).reason(SUBJECT, signals, timeout=5.0)
    assert compute_score(r.signals).score < before


async def test_strength_outside_the_closed_set_is_ignored():
    class Weird(FakeLLM):
        async def complete_json(self, system, user, schema, *, max_tokens, timeout):
            return {"signals": [{"id": "behav.anonymiser", "applies": True,
                                 "strength": "catastrophic", "note": ""}],
                    "pattern": "account takeover", "summary": "A new payee received the funds."}

    r = await ReasoningService(Weird()).reason(SUBJECT, two_signals(), timeout=5.0)
    assert all(s.strength in set(Strength) for s in r.signals)
    assert next(s for s in r.signals if s.id == "behav.anonymiser").strength is Strength.MODERATE


async def test_pattern_outside_the_closed_set_becomes_none():
    r = await ReasoningService(echo_llm(pattern="alien mind control")).reason(
        SUBJECT, two_signals(), timeout=5.0)
    assert r.pattern is Pattern.NONE


async def test_a_pattern_is_not_claimed_without_a_risk_signal():
    benign = [sig(id="b1", severity=0.0, benign=True)]
    r = await ReasoningService(echo_llm(pattern="card testing")).reason(
        SUBJECT, benign, timeout=5.0)
    assert r.pattern is Pattern.NONE


# --------------------------------------------------------------- summary validation

@pytest.mark.parametrize("summary,ok", [
    ("A first transfer of $4,200 went to a new payee on a 2-day-old account.", True),
    ("The signals point to a takeover cash-out.", True),
    ("", False),
    ("This is a HIGH fraud risk subject.", False),               # grades the subject
    ("Overall risk level is critical.", False),
    ("The risk score is low here.", False),
    ("The customer lost $87,000 across 9 accounts.", False),     # numbers nobody supplied
    ("x" * 700, False),                                          # absurdly long
])
def test_validate_summary(summary, ok):
    subject = "New account, first transfer of $4,200 to a new payee, 2 days old"
    signals = [sig(text="The account is only 2 days old.")]
    assert validate_summary(summary, subject, signals)[0] is ok


async def test_a_summary_that_grades_the_subject_is_replaced_by_the_template():
    llm = echo_llm(summary="This subject is a CRITICAL fraud risk.")
    r = await ReasoningService(llm).reason(SUBJECT, two_signals(), timeout=5.0)
    assert r.mode.startswith("llm_summary_rejected")
    assert "critical" not in r.summary.lower()
    assert r.summary


async def test_a_summary_with_invented_numbers_is_replaced():
    llm = echo_llm(summary="Losses of $93,400 were recorded over 11 prior disputes.")
    r = await ReasoningService(llm).reason(SUBJECT, two_signals(), timeout=5.0)
    assert r.mode.startswith("llm_summary_rejected")
    assert "93,400" not in r.summary


async def test_numbers_from_the_signals_are_allowed():
    signals = two_signals()
    signals[0].text = "The account is only 2 days old."
    llm = echo_llm(summary="A 2-day-old account sent $4,200 to a new payee.")
    r = await ReasoningService(llm).reason(SUBJECT, signals, timeout=5.0)
    assert r.mode == "llm"
    assert "4,200" in r.summary


# --------------------------------------------------------------- failure paths

@pytest.mark.parametrize("error", [
    LLMUnavailable("no key"),
    LLMUnavailable("timeout after 5.0s"),
    LLMUnavailable("model refused"),
    LLMUnavailable("unparseable model output"),
])
async def test_every_llm_failure_falls_back_deterministically(error):
    r = await ReasoningService(FakeLLM(error=error)).reason(SUBJECT, two_signals(), timeout=5.0)
    assert r.mode.startswith("llm_failed")
    assert r.summary and r.pattern is not None
    assert len(r.signals) == 2


async def test_fallback_keeps_the_same_band_as_the_llm_path():
    """The decisive property: a missing model changes the wording, never the grade."""
    signals_llm = two_signals()
    signals_det = two_signals()
    llm_result = await ReasoningService(echo_llm()).reason(SUBJECT, signals_llm, timeout=5.0)
    det_result = await ReasoningService(
        FakeLLM(error=LLMUnavailable("down"))).reason(SUBJECT, signals_det, timeout=5.0)
    assert compute_score(llm_result.signals).level is compute_score(det_result.signals).level


async def test_reasoning_is_skipped_when_there_is_no_time_left():
    llm = echo_llm()
    r = await ReasoningService(llm).reason(SUBJECT, two_signals(), timeout=0.05)
    assert r.mode == "deterministic" and llm.calls == 0


async def test_reasoning_mode_off_never_calls_the_model():
    llm = echo_llm()
    service = ReasoningService(llm, mode="off")
    assert service.uses_llm is False
    r = await service.reason(SUBJECT, two_signals(), timeout=5.0)
    assert llm.calls == 0 and r.mode == "deterministic"


async def test_malformed_model_payloads_do_not_raise():
    for payload in ({}, {"signals": None}, {"signals": ["not-a-dict"]},
                    {"signals": [{"id": None}], "pattern": None, "summary": None},
                    {"pattern": "card testing"}):
        r = await ReasoningService(FakeLLM(response=payload)).reason(
            SUBJECT, two_signals(), timeout=5.0)
        assert r.summary, payload


async def test_no_signals_produces_an_honest_insufficient_information_summary():
    r = await ReasoningService(echo_llm()).reason(SUBJECT, [], timeout=5.0)
    assert "insufficient information" in r.summary.lower()
    assert r.pattern is Pattern.NONE


# --------------------------------------------------------------- the prompt itself

async def test_the_model_only_ever_sees_the_subject_and_the_gathered_signals():
    llm = echo_llm()
    signals = two_signals()
    await ReasoningService(llm).reason(SUBJECT, signals, timeout=5.0)
    prompt = llm.last_user_prompt
    assert SUBJECT.original in prompt
    for s in signals:
        assert f"id={s.id}" in prompt and s.text in prompt
    # it must not be handed a score or a level to anchor on
    for leak in ("risk_score", "risk_level", "LOW", "MEDIUM", "HIGH", "CRITICAL"):
        assert leak not in prompt, leak


# --------------------------------------------------------------- templates

def test_the_three_low_summaries_are_distinct_statements():
    insufficient = template_summary([], Pattern.NONE, insufficient=True)
    degraded = template_summary([], Pattern.NONE, degraded=True)
    clean = template_summary([sig(id="b", severity=0.0, benign=True,
                                  text="The account has 5 years of history.")],
                             Pattern.NONE, checked_clean=True)
    assert len({insufficient, degraded, clean}) == 3
    assert "insufficient information" in insufficient.lower()
    assert "could not be reached" in degraded.lower()
    assert "checks found" in clean.lower() and "no signal" in clean.lower()
    # and none of them may imply the subject was shown to be safe
    for summary in (insufficient, degraded, clean):
        assert "is safe" not in summary.lower()


@pytest.mark.parametrize("n_signals", [1, 2, 3, 5, 9])
def test_template_summary_stays_within_two_sentences(n_signals):
    """The intent asks for one or two sentences; an earlier version emitted one per signal."""
    signals = [sig(id=f"r{i}", severity=0.5, label=f"risk factor {i}") for i in range(n_signals)]
    for pattern in (Pattern.CARD_TESTING, Pattern.NONE):
        summary = template_summary(signals, pattern)
        sentences = [s for s in summary.replace("! ", ". ").split(". ") if s.strip()]
        assert len(sentences) <= 2, f"{n_signals} signals -> {len(sentences)} sentences: {summary}"
        assert len(summary) < 420, summary


def test_template_summary_names_the_pattern():
    s = template_summary([sig(id="r", severity=0.6, text="Fourteen attempts were declined.")],
                         Pattern.CARD_TESTING)
    assert "card testing" in s.lower()


def test_template_summary_never_grades_the_subject():
    for pattern in Pattern:
        s = template_summary([sig(id="r", severity=0.6, text="A signal fired.")], pattern)
        assert validate_summary(s, "A signal fired.", [sig(text="A signal fired.")])[0], pattern


def test_extract_json_tolerates_fences_and_chatter():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! {"a": 2} hope that helps') == {"a": 2}
    with pytest.raises(ValueError):
        extract_json("no json here")


# --------------------------------------------------------------- the client never leaks

class _Resp:
    """Minimal stand-in for an Anthropic Message."""

    def __init__(self, text="", stop_reason="end_turn"):
        self.stop_reason = stop_reason
        self.content = [type("B", (), {"type": "text", "text": text})()] if text else []


class _Messages:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.behaviour(len(self.calls), kwargs)


class _FakeSDK:
    """Fakes both client.messages and client.beta.messages."""

    def __init__(self, behaviour):
        self.messages = _Messages(behaviour)
        self.beta = type("Beta", (), {"messages": self.messages})()


def _client_with(behaviour, **settings_kw):
    from fraudrisk.config import Settings
    from fraudrisk.llm import LLMClient

    client = LLMClient(Settings(anthropic_api_key="test-key", **settings_kw))
    client._client = _FakeSDK(behaviour)
    return client


GOOD_JSON = '{"signals": [], "pattern": "card testing", "summary": "Fine."}'


async def test_complete_json_parses_a_good_reply():
    client = _client_with(lambda n, kw: _Resp(GOOD_JSON))
    data = await client.complete_json("s", "u", SCHEMA, max_tokens=100, timeout=5.0)
    assert data["pattern"] == "card testing"


async def test_fallbacks_go_to_the_beta_endpoint_with_the_beta_flag():
    """`fallbacks` is only accepted on the beta endpoint; sending it elsewhere raises."""
    client = _client_with(lambda n, kw: _Resp(GOOD_JSON), enable_fallbacks=True)
    await client.complete_json("s", "u", SCHEMA, max_tokens=100, timeout=5.0)
    sent = client._client.messages.calls[0]
    assert sent["fallbacks"] == "default"
    assert sent["betas"] == ["server-side-fallback-2026-07-01"]
    assert sent["output_config"]["format"]["type"] == "json_schema"


async def test_fallbacks_can_be_switched_off():
    client = _client_with(lambda n, kw: _Resp(GOOD_JSON), enable_fallbacks=False)
    await client.complete_json("s", "u", SCHEMA, max_tokens=100, timeout=5.0)
    assert "fallbacks" not in client._client.messages.calls[0]


async def test_a_refusal_becomes_llm_unavailable():
    client = _client_with(lambda n, kw: _Resp("", stop_reason="refusal"))
    with pytest.raises(LLMUnavailable, match="refused"):
        await client.complete_json("s", "u", SCHEMA, max_tokens=100, timeout=5.0)


async def test_a_truncated_reply_becomes_llm_unavailable():
    """max_tokens truncation leaves unparseable JSON; it must fail over explicitly."""
    client = _client_with(lambda n, kw: _Resp('{"signals": [{"id": "a"', stop_reason="max_tokens"))
    with pytest.raises(LLMUnavailable, match="truncated"):
        await client.complete_json("s", "u", SCHEMA, max_tokens=100, timeout=5.0)


async def test_an_empty_reply_becomes_llm_unavailable():
    client = _client_with(lambda n, kw: _Resp(""))
    with pytest.raises(LLMUnavailable, match="empty"):
        await client.complete_json("s", "u", SCHEMA, max_tokens=100, timeout=5.0)


async def test_unparseable_output_becomes_llm_unavailable():
    client = _client_with(lambda n, kw: _Resp("I would rather not say."))
    with pytest.raises(LLMUnavailable, match="unparseable"):
        await client.complete_json("s", "u", SCHEMA, max_tokens=100, timeout=5.0)


async def test_rejected_params_retry_plainly_and_ask_for_json_in_the_prompt():
    """A TypeError from an SDK that does not know these kwargs must not escape."""

    def behaviour(n, kw):
        if n == 1:
            raise TypeError("create() got an unexpected keyword argument 'fallbacks'")
        return _Resp(GOOD_JSON)

    client = _client_with(behaviour, enable_fallbacks=True)
    data = await client.complete_json("s", "USER-PROMPT", SCHEMA, max_tokens=100, timeout=5.0)
    assert data["pattern"] == "card testing"
    retry = client._client.messages.calls[1]
    assert "fallbacks" not in retry and "output_config" not in retry
    # the retry is unconstrained, so it must ask for JSON in the prompt itself
    assert "JSON object" in retry["messages"][0]["content"]
    assert "USER-PROMPT" in retry["messages"][0]["content"]


@pytest.mark.parametrize("error", [
    TypeError("bad kwarg"), RuntimeError("boom"), ValueError("nope"),
    KeyError("missing"), AttributeError("gone"),
])
async def test_no_unexpected_exception_escapes_the_client(error):
    def behaviour(n, kw):
        raise error

    client = _client_with(behaviour)
    with pytest.raises(LLMUnavailable):
        await client.complete_json("s", "u", SCHEMA, max_tokens=100, timeout=5.0)


@pytest.mark.parametrize("error", [RuntimeError("boom"), TypeError("bad kwarg")])
async def test_reasoning_survives_a_non_llm_unavailable_error(error):
    """The band must hold even if the reasoning stage raises something unforeseen."""
    signals = two_signals()
    expected = compute_score(two_signals()).level
    r = await ReasoningService(FakeLLM(error=error)).reason(SUBJECT, signals, timeout=5.0)
    assert r.mode.startswith("llm_failed")
    assert r.summary.strip()
    assert compute_score(r.signals).level is expected
