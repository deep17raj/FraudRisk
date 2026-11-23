"""Rule 11, enforced: every Groq-assisted intent answers validly and honestly with no key and on every Groq failure.

For each of the five intents the same matrix runs against canned sources: no key, 401, 429, a retired model, 5xx, a
timeout, a hung response, garbage output, an ungrounded answer. In every case the HTTP answer is 200 with exactly the
spec's fields and the same non-empty keyless answer; and with a good Groq reply the model's grounded answer wins.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx
import pytest
from fastapi.testclient import TestClient

from intentkit.groq import reset_gates
from intents.chat_completion.app import ChatCompletionPipeline, create_app as chat_app
from intents.chat_completion.config import Settings as ChatSettings
from intents.event_outcome.app import EventOutcomePipeline, create_app as event_app
from intents.event_outcome.config import Settings as EventSettings
from intents.research_query.app import ResearchQueryPipeline, create_app as research_app
from intents.research_query.config import Settings as ResearchSettings
from intents.security_review.app import SecurityReviewPipeline, create_app as security_app
from intents.security_review.config import Settings as SecuritySettings
from intents.web_search.app import WebSearchPipeline, create_app as web_app
from intents.web_search.config import Settings as WebSettings
from tests.intents.helpers import FakeHttp
from tests.intents.llm_fixtures import (DDG, GNEWS, OPENALEX, POLYMARKET, PUBMED_FETCH, PUBMED_SEARCH, WIKI, Groq,
                                        ddg, groq_error, groq_reply, openalex, pubmed_xml, rss, transport, wiki)

KEY = "gsk_test_not_a_real_key"
EXACT = {
    "web_search": {"answer", "sources", "brief"}, "chat_completion": {"reply", "verdict", "brief"},
    "research_query": {"answer", "source_count", "sources", "brief"},
    "security_review": {"verdict", "vulnerability_count", "issues", "summary"},
    "event_outcome": {"event", "outcome", "resolved_yes", "winner", "resolved_at", "sources"},
}


@pytest.fixture(autouse=True)
def fresh_gates():
    reset_gates()
    yield
    reset_gates()


@dataclass
class Case:
    name: str
    app: Callable
    pipeline: Callable
    settings: Callable
    path: str
    body: dict
    routes: dict
    good: Any                                 # a Groq reply that should be used
    wrong: Any                                # a Groq reply that must be rejected (ungrounded / invalid)
    keyless: Callable[[dict], bool]           # what the answer looks like without the model
    llm: Callable[[dict], bool]               # what it looks like when the model's answer was used
    field: str = "answer"                     # the free-text field that must never be empty


KARACHI = ("Karachi", "Karachi is the capital city of the Pakistani province of Sindh. It has a population of more than twenty million people.")
CODE = "import os\ndef run(cmd):\n    os.system('ls ' + cmd)\n    return eval(cmd)\n"
CASES = [
    Case("web_search", web_app, WebSearchPipeline, WebSettings, "/web-search", {"query": "population of Karachi"},
         {WIKI: wiki(KARACHI), DDG: ddg(), GNEWS: rss()},
         groq_reply({"answer": "Karachi has more than twenty million people [1].", "used": [1]}),
         groq_reply({"answer": "Karachi is huge and also visit https://evil.example/x for more."}),
         lambda j: "capital city of the Pakistani province" in j["answer"] and j["sources"] == ["https://en.wikipedia.org/wiki/Karachi"],
         lambda j: j["answer"].startswith("Karachi has more than twenty million") and j["sources"] == ["https://en.wikipedia.org/wiki/Karachi"]),
    Case("chat_completion", chat_app, ChatCompletionPipeline, ChatSettings, "/chat-completion", {"message": "Explain what a hash table is"},
         {WIKI: wiki(("Hash table", "In computer science, a hash table is a data structure that maps keys to values using a hash function.")), DDG: ddg()},
         groq_reply({"reply": "A hash table stores key-value pairs by hashing keys to array slots.", "verdict": None}),
         groq_reply("I'm sorry, I can't help with that."),
         lambda j: "reference source says" in j["reply"] and "hash table" in j["reply"] and j["verdict"] is None,
         lambda j: j["reply"].startswith("A hash table stores key-value pairs") and j["verdict"] is None, field="reply"),
    Case("research_query", research_app, ResearchQueryPipeline, ResearchSettings, "/research-query",
         {"question": "Does intermittent fasting reduce body weight?"},
         {OPENALEX: openalex(("Intermittent fasting and body weight", "https://doi.org/10.1/abc",
                              "Intermittent fasting reduced body weight in randomized trials by several percent.", 2022, 40)),
          PUBMED_SEARCH: {"esearchresult": {"idlist": []}}, PUBMED_FETCH: pubmed_xml(), WIKI: wiki()},
         groq_reply({"answer": "Randomized trials report that intermittent fasting reduces body weight by several percent [1].", "used": [1]}),
         groq_reply({"answer": "Fasting definitely cures every disease, per many studies."}),
         lambda j: j["answer"].startswith("Based on the retrieved literature") and j["source_count"] == 1 and j["sources"] == ["https://doi.org/10.1/abc"],
         lambda j: j["answer"].startswith("Randomized trials report") and j["source_count"] == 1),
    Case("security_review", security_app, SecurityReviewPipeline, SecuritySettings, "/security-review", {"target": CODE}, {},
         groq_reply({"findings": [{"id": "CWE-22", "title": "Path traversal via user path", "severity": "high", "quote": "return eval(cmd)"},
                                  {"id": "CWE-89", "title": "Made up", "severity": "high", "quote": "this text is not in the code at all"},
                                  {"id": "BAD", "title": "Bad id", "severity": "high", "quote": "os.system('ls ' + cmd)"}]}),
         groq_reply({"findings": [{"id": "CWE-89", "title": "Invented", "severity": "critical", "quote": "nothing like this appears"}]}),
         lambda j: j["verdict"] == "Insecure" and j["vulnerability_count"] == 2 and j["issues"][0].startswith("CWE-"),
         lambda j: j["vulnerability_count"] == 3 and any("CWE-22" in i for i in j["issues"]), field="summary"),
    Case("event_outcome", event_app, EventOutcomePipeline, EventSettings, "/event-outcome",
         {"event": "Did Argentina win the 2022 World Cup final?"},
         {POLYMARKET: {"events": []}, WIKI: wiki(("2022 FIFA World Cup final", "Argentina won the 2022 World Cup final against France on 18 December 2022 on penalties.")),
          GNEWS: rss()},
         groq_reply({"outcome": "yes", "winner": "Argentina", "resolved_at": "2022-12-18", "evidence": [1]}),
         groq_reply({"outcome": "yes", "winner": "Argentina", "resolved_at": "2022-12-18", "evidence": []}),
         lambda j: j["outcome"] == "unresolved" and j["resolved_yes"] is False and j["sources"] == [],
         lambda j: j["outcome"] == "yes" and j["resolved_yes"] is True and j["winner"] == "Argentina", field="event"),
]


def run(case: Case, groq_rules=None, key: str = KEY, **settings_kw):
    groq = Groq(*groq_rules) if groq_rules is not None else None
    fake = transport(case.routes, groq)
    kw = dict(groq_api_key=key, groq_models="m1,m2", groq_timeout=2.0, global_timeout=3.0, llm_margin=1.0,
              provider_timeout=1.0, cache_ttl=60, cache_ttl_degraded=5)
    kw.update(settings_kw)
    settings = case.settings(**kw)
    pl = case.pipeline(settings, fake.client())
    t0 = time.monotonic()
    with TestClient(case.app(settings, pipeline=pl)) as c:
        r = c.post(case.path, json=case.body)
    assert r.status_code == 200, r.text
    j = r.json()
    assert set(j) == EXACT[case.name], j
    return j, groq, time.monotonic() - t0


def by_case():
    return pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])


@by_case()
def test_with_no_key_the_keyless_answer_is_complete_and_groq_is_never_called(case):
    j, groq, _ = run(case, [groq_reply("never")], key="")
    assert case.keyless(j), j
    assert j[case.field] != "" and groq.calls == []


FAILURES = {
    "unauthorized": [401], "rate_limited": [(429, {"retry-after": "20"})], "retired_model": [404],
    "retired_model_400": [httpx.Response(400, json=groq_error("model_decommissioned"))],
    "server_error": [503], "network_error": [httpx.ConnectError("down")], "timeout": [httpx.ReadTimeout("slow")],
    "empty_content": [groq_reply("")], "garbage_text": [groq_reply("I cannot help with that, sorry.")],
    "json_array": [groq_reply("[1, 2, 3]")], "no_choices": [{"choices": []}],
}


@by_case()
@pytest.mark.parametrize("failure", list(FAILURES))
def test_every_groq_failure_gives_the_same_complete_keyless_answer(case, failure):
    j, groq, took = run(case, FAILURES[failure])
    assert case.keyless(j), (failure, j)
    assert j[case.field] != "" and took < 3.0
    assert groq.calls, "the model should have been tried before falling back"


@by_case()
def test_a_hung_groq_does_not_cost_the_answer(case):
    """Groq takes far longer than the budget: the keyless answer comes back before the service-level timeout."""
    j, groq, took = run(case, [(groq_reply("{}"), 30.0)])
    assert case.keyless(j), j
    assert took < 3.0


@by_case()
def test_an_ungrounded_or_invalid_model_answer_is_discarded(case):
    j, _, _ = run(case, [case.wrong])
    assert case.keyless(j), j


@by_case()
def test_a_good_groq_answer_replaces_the_keyless_one_and_uses_only_retrieved_sources(case):
    j, groq, _ = run(case, [case.good])
    assert case.llm(j), j
    assert groq.calls[0]["json_mode"] and groq.calls[0]["auth"] == f"Bearer {KEY}"
    assert "evil.example" not in str(j)


@by_case()
def test_after_a_401_the_next_request_does_not_call_groq_again(case):
    groq = Groq(401)
    fake = transport(case.routes, groq)
    settings = case.settings(groq_api_key=KEY, groq_models="m1,m2", global_timeout=3.0, llm_margin=1.0, provider_timeout=1.0,
                             cache_ttl=0, cache_ttl_degraded=0)
    pl = case.pipeline(settings, fake.client())
    with TestClient(case.app(settings, pipeline=pl)) as c:
        c.post(case.path, json=case.body)
        n = len(groq.calls)
        r = c.post(case.path, json=case.body)
    assert r.status_code == 200 and len(groq.calls) == n and n == 1 and case.keyless(r.json())


@by_case()
def test_a_model_answer_is_cached_but_a_keyless_fallback_is_cached_briefly_when_a_key_is_set(case):
    # with a working model the pipeline caches for cache_ttl; with a failing one for cache_ttl_degraded only
    fake_ok = transport(case.routes, Groq(case.good))
    settings = case.settings(groq_api_key=KEY, groq_models="m1", global_timeout=3.0, llm_margin=1.0, provider_timeout=1.0,
                             cache_ttl=1000, cache_ttl_degraded=0)
    pl = case.pipeline(settings, fake_ok.client())
    import asyncio
    req = {k: v for k, v in case.body.items()}
    asyncio.run(pl.answer(req))
    assert pl.cache.size() == 1                                  # a model answer is kept
    reset_gates()
    fake_bad = transport(case.routes, Groq(500))
    pl2 = case.pipeline(settings, fake_bad.client())
    asyncio.run(pl2.answer(req))
    assert pl2.cache.size() == 0                                 # a degraded keyless answer is not kept (ttl 0)


@by_case()
def test_the_service_fallback_after_an_exception_is_still_valid_json_with_every_field(case):
    class Boom(case.pipeline):                                   # type: ignore[misc, valid-type]
        async def compute(self, req):
            raise RuntimeError("bug")

    settings = case.settings(groq_api_key=KEY)
    pl = Boom(settings, FakeHttp().client())
    with TestClient(case.app(settings, pipeline=pl)) as c:
        r = c.post(case.path, json=case.body)
        blank = c.post(case.path, content=b"not json")
    assert r.status_code == 200 and set(r.json()) == EXACT[case.name]
    assert blank.status_code == 200 and set(blank.json()) == EXACT[case.name]
    text = r.json()[case.field]
    assert text != "" or case.name == "event_outcome" and r.json()["event"] != ""


def test_a_prediction_question_leads_with_the_verdict_with_and_without_the_model():
    case = next(c for c in CASES if c.name == "chat_completion")
    body = {"message": "Will it rain in London tomorrow?"}
    keyless = ChatSettings(groq_api_key="", global_timeout=3.0, provider_timeout=1.0)
    pl = ChatCompletionPipeline(keyless, transport(case.routes).client())
    with TestClient(chat_app(keyless, pipeline=pl)) as c:
        j = c.post("/chat-completion", json=body).json()
    assert j["verdict"] == "Uncertain" and j["reply"].startswith("Uncertain")

    groq = Groq(groq_reply({"reply": "Rain is likely in London in November.", "verdict": "Likely"}))
    settings = ChatSettings(groq_api_key=KEY, groq_models="m1", global_timeout=3.0, llm_margin=1.0, provider_timeout=1.0)
    pl2 = ChatCompletionPipeline(settings, transport(case.routes, groq).client())
    with TestClient(chat_app(settings, pipeline=pl2)) as c:
        j2 = c.post("/chat-completion", json=body).json()
    assert j2["verdict"] == "Likely" and j2["reply"].startswith("Likely")
    assert len(j2["brief"]) <= 80

    groq3 = Groq(groq_reply({"reply": "It will probably be wet.", "verdict": "Maybe"}))        # not a spec value: derived, not raised
    pl3 = ChatCompletionPipeline(settings, transport(case.routes, groq3).client())
    with TestClient(chat_app(settings, pipeline=pl3)) as c:
        j3 = c.post("/chat-completion", json=body).json()
    assert j3["verdict"] in ("Likely", "Unlikely", "Uncertain") and j3["reply"].startswith(j3["verdict"])
