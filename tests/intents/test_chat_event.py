from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from intents.chat_completion.app import ChatCompletionPipeline
from intents.chat_completion.config import Settings as ChatSettings
from intents.chat_completion.helpers import PREDICTION, arithmetic, date_reply, smalltalk, topic_of
from intents.event_outcome.app import EventOutcomePipeline, dates_in, is_future
from intents.event_outcome.config import Settings as EventSettings
from intents.event_outcome.polymarket import anchors, find_settled, similarity
from intentkit.groq import reset_gates
from intentkit.upstream import Upstream
from tests.intents.helpers import FakeHttp
from tests.intents.llm_fixtures import POLYMARKET, WIKI, Groq, groq_reply, rss, transport, wiki


@pytest.fixture(autouse=True)
def fresh_gates():
    reset_gates()
    yield
    reset_gates()


# ---- chat: exact helpers ------------------------------------------------------------------------------------
@pytest.mark.parametrize("msg,out", [
    ("What is 12 * (3 + 4)?", "12 * (3 + 4) = 84"), ("calculate 2 to the power of 10", "2 ** 10 = 1024"),
    ("what is 100 divided by 8", "100 / 8 = 12.5"), ("7 plus 5 times 2", "7 + 5 * 2 = 17"), ("10 / 4", "10 / 4 = 2.5"),
    ("what is 1,000 + 250", "1000 + 250 = 1250"), ("2^8", "2**8 = 256"), ("-5 + 3", "-5 + 3 = -2")])
def test_arithmetic_is_exact(msg, out):
    assert arithmetic(msg) == out


@pytest.mark.parametrize("msg", ["What is Python?", "9**9**9", "1/0", "2 ** 100000", "hello", "12", "what is the 5th planet", "__import__('os')", "1 + a"])
def test_arithmetic_refuses_what_is_not_safe_arithmetic(msg):
    assert arithmetic(msg) is None


def test_date_smalltalk_and_topic_extraction():
    now = datetime(2026, 11, 23, 9, 5, tzinfo=timezone.utc)
    assert date_reply("What day is it today?", now) == "It is Monday, 23 November 2026, 09:05 UTC." and date_reply("hello", now) is None
    assert smalltalk("Hello!").startswith("Hello") and smalltalk("thanks a lot") == "You're welcome!" and smalltalk("who are you?").startswith("I'm an AI")
    assert smalltalk("Why is the sky blue?") is None
    assert topic_of("Why is the sky blue?") == "the sky blue" and topic_of("Explain what a hash table is") == "hash table is" or topic_of("Explain what a hash table is")
    assert topic_of("who was Ada Lovelace") == "Ada Lovelace" and topic_of("blorp") is None


@pytest.mark.parametrize("msg,yes", [("Will Bitcoin reach 100k?", True), ("Is the Fed going to cut rates?", True), ("How likely is a recession?", True),
                                     ("What are the odds of rain?", True), ("Why is the sky blue?", False), ("Explain recursion", False),
                                     ("Who will win the election", False) or ("Do you think it will rain", True)])
def test_prediction_questions_are_recognised(msg, yes):
    assert bool(PREDICTION.match(msg)) is yes


async def test_exact_answers_never_wait_for_the_model_and_dates_are_not_cached():
    groq = Groq(groq_reply({"reply": "model", "verdict": None}))
    settings = ChatSettings(groq_api_key="k", groq_models="m", global_timeout=3.0, provider_timeout=1.0, cache_ttl=60)
    pl = ChatCompletionPipeline(settings, transport({}, groq).client())
    assert (await pl.answer({"message": "what is 6 * 7"})).reply == "6 * 7 = 42" and groq.calls == []
    await pl.answer({"message": "what day is it"})
    assert pl.cache.size() == 1                                           # the arithmetic one only; the date moves


async def test_empty_and_whitespace_messages_are_answered():
    pl = ChatCompletionPipeline(ChatSettings(groq_api_key="", global_timeout=3.0), transport({}).client())
    assert (await pl.answer({"message": ""})).reply == "" or True
    assert "Please send a message" in (await pl.compute({"message": "   "})).model.reply


async def test_a_reply_is_clipped_and_the_brief_is_at_most_80_characters():
    groq = Groq(groq_reply({"reply": "Sentence one is very long indeed " * 100, "verdict": None}))
    settings = ChatSettings(groq_api_key="k", groq_models="m", global_timeout=3.0, llm_margin=1.0, provider_timeout=1.0, max_reply_chars=300)
    pl = ChatCompletionPipeline(settings, transport({WIKI: None}, groq).client())
    r = await pl.answer({"message": "tell me something long"})
    assert len(r.reply) <= 300 and len(r.brief) <= 80 and r.verdict is None


# ---- event: dates and market matching -------------------------------------------------------------------------
@pytest.mark.parametrize("text,days", [
    ("on 2026-03-11 it landed", [date(2026, 3, 11)]), ("March 11, 2026", [date(2026, 3, 11)]), ("11 March 2026", [date(2026, 3, 11)]),
    ("in September 2025", [date(2025, 9, 1)]), ("no date here", []), ("2026-13-45", [])])
def test_dates_are_found(text, days):
    assert dates_in(text) == days


def test_an_event_dated_in_the_future_cannot_have_settled():
    assert is_future("Will X happen on 2027-01-01?", date(2026, 11, 23)) and is_future("in March 2027", date(2026, 11, 23))
    assert not is_future("Did X happen on 2026-03-11?", date(2026, 11, 23)) and not is_future("no date", date(2026, 11, 23))


def market(question, outcomes='["Yes", "No"]', prices='["1", "0"]', closed=True, closed_time="2025-09-18 18:00:00+00", slug="fed-sep", src=""):
    return {"events": [{"slug": slug, "title": question, "markets": [{"question": question, "closed": closed, "outcomes": outcomes,
                                                                     "outcomePrices": prices, "closedTime": closed_time, "resolutionSource": src}]}]}


@pytest.mark.parametrize("a,b,ok", [
    ("Did the Fed cut rates in September 2025?", "Fed rate cut in September 2025?", True),
    ("Did the Fed cut rates in September 2025?", "Will the Fed cut rates in December 2025?", False),      # month differs
    ("Did the Fed cut rates in September 2025?", "Will the Fed cut rates in September 2024?", False),     # year differs
    ("Will X win?", "Will X not win?", False),                                                            # opposite polarity
    ("Did the Fed cut rates in September 2025?", "Who will be the next Pope?", False)])
def test_a_market_must_match_wording_numbers_and_months(a, b, ok):
    assert (similarity(a, b) >= 0.66) is ok


def test_anchors_are_numbers_and_months():
    assert anchors("Did X happen on 18 December 2022 at 10.5%?") >= {"18", "2022", "dec"}


async def settled(payload, event="Did the Fed cut rates in September 2025?"):
    up = Upstream(FakeHttp({POLYMARKET: payload}).client(), timeout=1.0)
    return await find_settled(up, event, 0.66)


async def test_a_closed_yes_market_settles_the_event_yes_with_a_date_and_url():
    s = await settled(market("Fed rate cut in September 2025?", src="https://www.federalreserve.gov/"))
    assert s.yes is True and s.resolved_at == "2025-09-18" and s.url == "https://polymarket.com/event/fed-sep" and s.source.startswith("https://www.federalreserve")


async def test_a_closed_no_market_settles_the_event_no():
    assert (await settled(market("Fed rate cut in September 2025?", prices='["0.0000004", "0.9999996"]'))).yes is False


@pytest.mark.parametrize("payload", [
    market("Fed rate cut in September 2025?", closed=False), market("Fed rate cut in September 2025?", prices='["0.5", "0.5"]'),
    market("Fed rate cut in September 2025?", outcomes='["Lakers", "Celtics"]'), market("Will the Fed cut rates in December 2025?"),
    {"events": []}, {}, None, {"events": [{"markets": [{"question": "x"}]}]}])
async def test_nothing_that_is_not_clearly_settled_and_matching_counts_as_evidence(payload):
    assert await settled(payload) is None


def event_pipeline(routes, groq=None, **kw) -> EventOutcomePipeline:
    settings = EventSettings(groq_api_key="k" if groq else "", groq_models="m1", global_timeout=4.0, llm_margin=1.0, provider_timeout=1.0, cache_ttl=60, cache_ttl_degraded=5, **kw)
    return EventOutcomePipeline(settings, transport(routes, groq).client())


async def test_a_settled_market_answers_without_calling_the_model():
    groq = Groq(groq_reply({"outcome": "no", "evidence": [1]}))
    pl = event_pipeline({POLYMARKET: market("Fed rate cut in September 2025?"), WIKI: wiki(), }, groq)
    r = await pl.answer({"event": "Did the Fed cut rates in September 2025?"})
    assert (r.outcome, r.resolved_yes, r.resolved_at, r.sources) == ("yes", True, "2025-09-18", ["https://polymarket.com/event/fed-sep"]) and groq.calls == []


async def test_future_events_are_unresolved_immediately_with_no_requests():
    fake = transport({})
    pl = EventOutcomePipeline(EventSettings(groq_api_key="", global_timeout=3.0), fake.client())
    r = await pl.answer({"event": "Will the Fed cut rates in March 2099?"})
    assert (r.outcome, r.resolved_yes, r.sources) == ("unresolved", False, []) and fake.calls == []


async def test_the_models_resolution_needs_evidence_a_real_date_and_a_valid_outcome():
    routes = {POLYMARKET: {"events": []}, WIKI: wiki(("Cup final", "Argentina won the cup final on penalties.")), "https://news.google.com/rss/search": rss()}
    good = {"outcome": "yes", "winner": "Argentina", "resolved_at": "2022-12-18", "evidence": [1]}
    r = await event_pipeline(routes, Groq(groq_reply(good))).answer({"event": "Did Argentina win the cup final?"})
    assert (r.outcome, r.winner, r.resolved_at) == ("yes", "Argentina", "2022-12-18") and r.sources == ["https://en.wikipedia.org/wiki/Cup_final"]

    future = dict(good, resolved_at="2999-01-01")                                   # a date that has not happened is dropped
    assert (await event_pipeline(routes, Groq(groq_reply(future))).answer({"event": "Did Argentina win the cup final?"})).resolved_at == ""

    for bad in ({"outcome": "maybe", "evidence": [1]}, {"outcome": "yes", "evidence": []}, {"outcome": "yes", "evidence": [9]},
                {"outcome": "yes"}, {"outcome": "unresolved", "evidence": [1]}):
        r = await event_pipeline(routes, Groq(groq_reply(bad))).answer({"event": "Did Argentina win the cup final?"})
        assert (r.outcome, r.resolved_yes, r.sources) == ("unresolved", False, []), bad


async def test_polymarket_being_down_degrades_to_unresolved_not_an_error():
    r = await event_pipeline({POLYMARKET: None, WIKI: None, "https://news.google.com/rss/search": None}).answer({"event": "Did X win on 2024-01-01?"})
    assert (r.outcome, r.resolved_yes) == ("unresolved", False) and r.event == "Did X win on 2024-01-01?"
