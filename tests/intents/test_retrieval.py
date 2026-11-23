"""Source parsers, text helpers and the vetting of model-written answers."""
from __future__ import annotations

import asyncio

import pytest

from intentkit import retrieval as r
from intentkit.answers import extractive, is_headline, prefer_reference, strip_foreign_urls, valid_answer
from intentkit.retrieval import Doc, dedupe_rank, gather_docs, search_phrase
from intentkit.text import best_sentences, brief, clip, overlap, sentences, strip_tags
from intentkit.upstream import Upstream
from tests.intents.helpers import FakeHttp
from tests.intents.llm_fixtures import (DDG, GNEWS, OPENALEX, PUBMED_FETCH, PUBMED_SEARCH, WIKI, ddg, openalex,
                                        pubmed_xml, rss, wiki)


def up(fake: FakeHttp) -> Upstream:
    return Upstream(fake.client(), timeout=1.0)


# ---- text -------------------------------------------------------------------------------------------------
def test_brief_is_one_sentence_of_at_most_80_characters():
    assert brief("Paris is the capital of France. It is also its largest city.") == "Paris is the capital of France."
    long = brief("The quick brown fox jumps over the lazy dog while the rest of the pack watches from a nearby hill today.")
    assert len(long) <= 80 and long.endswith("…")
    assert brief("") == "" and brief("<b>Bold</b> claim [1] here.") == "Bold claim here."


def test_clip_cuts_at_a_word_and_marks_the_cut():
    assert clip("short", 50) == "short"
    c = clip("alpha beta gamma delta epsilon", 14)
    assert len(c) <= 14 and c.endswith("…") and " " not in c[-3:]


def test_sentence_helpers():
    assert sentences("One. Two! Three? four.") == ["One.", "Two!", "Three? four."]
    assert strip_tags("<p>Hello&nbsp;<b>world</b></p>") == "Hello world"
    assert overlap("population of Karachi", "Karachi has a large population") == 1.0
    assert overlap("", "x") == 0.0
    got = best_sentences("capital of France", ["Berlin is large. Paris is the capital of France and its biggest city."], n=1)
    assert got == ["Paris is the capital of France and its biggest city."]


# ---- providers --------------------------------------------------------------------------------------------
async def test_wikipedia_returns_documents_with_encyclopedia_urls_in_rank_order():
    fake = FakeHttp({WIKI: wiki(("Python (programming language)", "Python is a language. " * 5), ("Monty Python", "A comedy group."))})
    docs = await r.wikipedia(up(fake), "python")
    assert [d.title for d in docs] == ["Python (programming language)", "Monty Python"]
    assert docs[0].url == "https://en.wikipedia.org/wiki/Python_%28programming_language%29" and docs[0].source == "Wikipedia"


@pytest.mark.parametrize("rule", [None, 403, {"query": {}}, {"nothing": 1}, "garbage"])
async def test_providers_return_nothing_rather_than_raising(rule):
    fake = FakeHttp({WIKI: rule, DDG: rule, GNEWS: rule, PUBMED_SEARCH: rule, OPENALEX: rule})
    u = up(fake)
    assert await r.wikipedia(u, "q") == [] and await r.duckduckgo(u, "q") == [] and await r.google_news(u, "q") == []
    assert await r.pubmed(u, "q") == [] and await r.openalex(u, "q") == []


async def test_duckduckgo_instant_answer():
    fake = FakeHttp({DDG: ddg("Python is a language.", "https://en.wikipedia.org/wiki/Python", "Python")})
    d = (await r.duckduckgo(up(fake), "python"))[0]
    assert d.url.endswith("/Python") and d.snippet == "Python is a language."
    assert await r.duckduckgo(up(FakeHttp({DDG: ddg()})), "q") == []


async def test_google_news_headlines_drop_the_appended_source_and_keep_the_date():
    fake = FakeHttp({GNEWS: rss(("Fed holds rates", "https://news.google.com/rss/articles/abc", "Wed, 18 Nov 2026 22:48:00 GMT", "AP News"))})
    d = (await r.google_news(up(fake), "fed"))[0]
    assert (d.title, d.source, d.date) == ("Fed holds rates", "AP News", "2026-11-18") and d.snippet == d.title


async def test_rss_with_a_dtd_or_entities_is_refused_not_expanded():
    bomb = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><rss><channel><item><title>&a;</title><link>https://x</link></item></channel></rss>'
    assert await r.google_news(up(FakeHttp({GNEWS: bomb})), "q") == []


async def test_pubmed_joins_search_and_abstract_fetch():
    fake = FakeHttp({PUBMED_SEARCH: {"esearchresult": {"idlist": ["111", "222"]}},
                     PUBMED_FETCH: pubmed_xml(("111", "Fasting and health", "Fasting improved markers.", "2024"),
                                              ("222", "No abstract here", "", "2023"))})
    docs = await r.pubmed(up(fake), "fasting")
    assert [(d.url, d.date, d.source) for d in docs] == [("https://pubmed.ncbi.nlm.nih.gov/111/", "2024", "PubMed")]


async def test_openalex_rebuilds_the_abstract_and_uses_the_doi():
    fake = FakeHttp({OPENALEX: openalex(("Fasting", "https://doi.org/10.1/x", "Intermittent fasting improves health markers", 2020, 55))})
    d = (await r.openalex(up(fake), "fasting"))[0]
    assert d.url == "https://doi.org/10.1/x" and d.snippet.startswith("Intermittent fasting improves health markers") and "cited by 55" in d.snippet
    assert d.date == "2020"


async def test_keyed_search_providers_send_their_keys_and_map_results():
    fake = FakeHttp({
        "https://api.tavily.com/search": {"results": [{"title": "T", "url": "https://t.example", "content": "tavily text"}]},
        "https://api.search.brave.com/res/v1/web/search": {"web": {"results": [{"title": "<b>B</b>", "url": "https://b.example", "description": "brave <i>text</i>"}]}},
        "https://google.serper.dev/search": {"organic": [{"title": "S", "link": "https://s.example", "snippet": "serper text"}]}})
    u = up(fake)
    assert (await r.tavily(u, "k1", "q"))[0].source == "Tavily"
    assert (await r.brave(u, "k2", "q"))[0].snippet == "brave text"
    assert (await r.serper(u, "k3", "q"))[0].url == "https://s.example"
    auth = {str(c.url.host): (c.headers.get("authorization"), c.headers.get("x-subscription-token"), c.headers.get("x-api-key")) for c in fake.calls}
    assert auth["api.tavily.com"][0] == "Bearer k1" and auth["api.search.brave.com"][1] == "k2" and auth["google.serper.dev"][2] == "k3"


# ---- gathering and ranking ---------------------------------------------------------------------------------
async def test_gather_keeps_provider_order_and_loses_only_the_failed_provider():
    async def ok(name):
        return [Doc(name, f"https://{name}.example", name, name)]

    async def boom():
        raise RuntimeError("down")

    docs = await gather_docs([ok("a"), boom(), ok("c")], timeout=1.0)
    assert [d.title for d in docs] == ["a", "c"]


async def test_gather_stops_at_the_soft_deadline_once_something_arrived_but_waits_to_the_hard_limit_otherwise():
    async def fast():
        return [Doc("f", "https://f.example", "f", "f")]

    async def slow():
        await asyncio.sleep(2.0)
        return [Doc("s", "https://s.example", "s", "s")]

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    docs = await gather_docs([fast(), slow()], timeout=3.0, soft=0.2)
    assert [d.title for d in docs] == ["f"] and loop.time() - t0 < 1.0
    t0 = loop.time()
    assert await gather_docs([slow()], timeout=0.3, soft=0.1) == [] and loop.time() - t0 < 1.0     # nothing yet: keeps waiting


def test_ranking_drops_duplicate_urls_and_unrelated_documents_and_prefers_better_matches():
    docs = [Doc("Other", "https://a.example/x", "nothing relevant", "a"),
            Doc("Fed rate decision", "https://b.example/y#frag", "The Fed decided on interest rates", "b"),
            Doc("Fed rate decision again", "https://b.example/y", "duplicate url", "b"),
            Doc("Interest", "https://c.example", "interest only", "c")]
    out = dedupe_rank(docs, "Fed interest rate decision", 5, min_overlap=0.2)
    assert [d.url for d in out] == ["https://b.example/y#frag", "https://c.example"]    # best match first; 'Other' dropped
    assert [d.url for d in dedupe_rank(docs, "Fed interest rate decision", 5, min_overlap=0.3)] == ["https://b.example/y#frag"]


def test_search_phrase_turns_a_question_into_keywords():
    assert search_phrase("What's the latest news on the Fed's interest rate decision?") == "latest news Fed interest rate decision"
    assert search_phrase("Who created the Python programming language?") == "created Python programming language"
    assert search_phrase("???") == "???"


# ---- answers ------------------------------------------------------------------------------------------------
DOCS = [Doc("Fasting", "https://a.example/1", "Intermittent fasting improved weight in trials. Other text here.", "PubMed", "2024"),
        Doc("Headline only", "https://b.example/2", "Headline only", "AP", "2026-11-18"),
        Doc("Diets", "https://c.example/3", "Calorie restriction also lowers weight over months of study.", "OpenAlex", "2020")]


def test_a_model_answer_must_cite_real_documents_and_loses_foreign_urls():
    ok = valid_answer({"answer": "Fasting helps weight loss [1] and diets too [3]. See https://evil.example/x and https://a.example/1.", "used": [1, 3]}, "answer", DOCS)
    assert ok is not None
    text, used = ok
    assert [d.url for d in used] == ["https://a.example/1", "https://c.example/3"]
    assert "evil.example" not in text and "https://a.example/1" in text


@pytest.mark.parametrize("obj", [None, {}, {"answer": 5}, {"answer": "short"}, {"answer": "A long enough answer with no citation anywhere."},
                                 {"answer": "A long enough answer citing nothing real [9].", "used": [9]},
                                 {"answer": "A long enough answer.", "used": ["x", None, 0, -1]}])
def test_unusable_model_answers_are_rejected(obj):
    assert valid_answer(obj, "answer", DOCS) is None


def test_citations_in_the_text_are_enough_when_used_is_missing_and_ids_may_be_strings():
    assert valid_answer({"answer": "Weight falls with fasting [2][1] in trials."}, "answer", DOCS)[1][0].title == "Fasting"
    assert len(valid_answer({"answer": "Weight falls with fasting in trials.", "used": ["1", "3"]}, "answer", DOCS)[1]) == 2


def test_urls_the_model_invented_are_removed_but_supplied_ones_stay():
    assert strip_foreign_urls("a https://ok.example/p, b https://bad.example", {"https://ok.example/p"}) == "a https://ok.example/p, b"


def test_the_extractive_answer_attributes_each_sentence_and_lists_headlines_as_headlines():
    text, used = extractive("intermittent fasting weight", DOCS, max_docs=3)
    assert "(PubMed, 2024)" in text and "Recent headlines: Headline only (AP, 2026-11-18)." in text
    assert [d.url for d in used][:2] == ["https://a.example/1", "https://b.example/2"] and is_headline(DOCS[1]) and not is_headline(DOCS[0])


def test_recent_questions_lead_with_newest_headlines_and_others_lead_with_reference_text():
    heads = [Doc("Old", "u1", "Old", "A", "2026-01-01"), Doc("New", "u2", "New", "B", "2026-11-20")]
    refs = [Doc("Ref", "u3", "A long explanatory snippet that is definitely more than sixty characters in total length.", "Wikipedia")]
    assert [d.title for d in prefer_reference(heads + refs, recent=True)] == ["New", "Old", "Ref"]
    assert [d.title for d in prefer_reference(heads + refs, recent=False)] == ["Ref", "Old", "New"]
