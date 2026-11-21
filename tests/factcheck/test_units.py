"""Unit tests for the parser, credibility, stance, agreement, synthesis, resilience and cache stages."""
import asyncio
import datetime as dt

import httpx
import pytest

from factcheck import agreement as agr
from factcheck.cache import TTLCache
from factcheck.claim_parser import cache_key_text, normalize_claim, parse_claim
from factcheck.credibility import CredibilityScorer
from factcheck.llm import LLMClient
from factcheck.models import Stance, Tier, Verdict, VerdictDecision
from factcheck.resilience import CircuitBreaker, CircuitOpen, call_with_retry
from factcheck.sources.factcheck_api import rating_to_stance
from factcheck.stance import HeuristicStanceLabeller
from factcheck.synthesis import Synthesizer, clean_llm_reasoning, select_sources
from tests.factcheck.conftest import ev, make_settings, scored


# ---------------------------------------------------------------- claim parser
@pytest.mark.parametrize("text", [
    "The best pizza in the world is in Naples.",
    "I think the new design is ugly.",
    "Cats are better than dogs.",
    "Everyone should learn to code.",
])
def test_opinions_are_not_checkable(text):
    p = parse_claim(text)
    assert not p.checkable and "subjective" in p.reject_reason


def test_opinion_with_hard_numbers_is_still_checkable():
    assert parse_claim("The best-selling album of all time sold 70 million copies.").checkable


@pytest.mark.parametrize("text", ["What is the capital of France?", "Why is the sky blue?", "Who invented the telephone?"])
def test_open_questions_rejected(text):
    p = parse_claim(text)
    assert not p.checkable and "question" in p.reject_reason


def test_yes_no_question_is_checkable():
    assert parse_claim("Is the Great Wall of China visible from space?").checkable


@pytest.mark.parametrize("text", ["Bitcoin will reach $1,000,000 by 2030.", "The team will win the title next year."])
def test_predictions_rejected(text):
    p = parse_claim(text, today=dt.date(2026, 11, 17))
    assert not p.checkable and "future" in p.reject_reason


@pytest.mark.parametrize("text", ["", "   ", "???", "Paris", "hello there"])
def test_empty_and_fragments_rejected(text):
    assert not parse_claim(text).checkable


def test_extracts_entities_numbers_years():
    p = parse_claim("Albert Einstein won the Nobel Prize in Physics in 1921, receiving 121,000 kronor.")
    assert p.checkable
    assert any("Einstein" in e for e in p.entities)
    assert 1921 in p.years
    assert p.search_queries and p.search_queries[0].startswith("Albert Einstein")


def test_time_sensitivity_detected():
    assert parse_claim("The president signed the bill today.").time_sensitive
    assert parse_claim("Inflation is currently above 5 percent.").time_sensitive
    assert not parse_claim("The Eiffel Tower is in Paris.").time_sensitive


def test_negation_and_prefix_stripping():
    p = parse_claim('Fact check: "The Moon is not made of cheese."')
    assert p.checkable and p.negated and p.text.startswith("The Moon")
    assert normalize_claim("Is it true that the sky is blue") == "the sky is blue"


def test_cache_key_normalisation():
    assert cache_key_text("The Earth  is ROUND!") == cache_key_text("the earth is round")
    assert cache_key_text("Is it true that the earth is round?") == cache_key_text("the earth is round")


# ---------------------------------------------------------------- credibility
@pytest.mark.parametrize("url,tier", [
    ("https://www.reuters.com/fact-check/x", Tier.HIGH),
    ("https://www.cdc.gov/page", Tier.HIGH),
    ("https://physics.mit.edu/x", Tier.HIGH),
    ("https://en.wikipedia.org/wiki/X", Tier.HIGH),
    ("https://www.nytimes.com/a", Tier.MEDIUM),
    ("https://news.bbc.co.uk/a", Tier.MEDIUM),
    ("https://random-blog.example/post", Tier.LOW),
    ("https://www.reddit.com/r/x", Tier.LOW),
    ("https://www.infowars.com/x", Tier.REJECTED),
    ("https://subdomain.naturalnews.com/x", Tier.REJECTED),
    ("https://www.theonion.com/x", Tier.REJECTED),
])
def test_credibility_tiers(url, tier):
    assert CredibilityScorer().tier_for(url) == tier


def test_credibility_no_substring_false_positives():
    s = CredibilityScorer()
    assert s.tier_for("https://notreuters.com/x") == Tier.LOW
    assert s.tier_for("https://reuters.com.evil.example/x") == Tier.LOW


def test_fact_checker_detection():
    s = CredibilityScorer()
    assert s.is_fact_checker("https://www.snopes.com/fact-check/x")
    assert not s.is_fact_checker("https://www.nytimes.com/x")


# ---------------------------------------------------------------- stance (heuristic)
def _label(claim_text, title, snippet, url="https://en.wikipedia.org/wiki/X", provider="wikipedia"):
    s = CredibilityScorer()
    item = scored(Stance.NEUTRAL, domain=s.domain_of(url), tier=s.tier_for(url))
    item.evidence = ev(url, title, snippet, provider=provider)
    HeuristicStanceLabeller(s).label_one(parse_claim(claim_text), item)
    return item


def test_heuristic_myth_page_refutes():
    it = _label("Humans only use 10% of their brains.", "Ten-percent-of-the-brain myth",
                "The ten percent of the brain myth is a widely perpetuated urban legend that most or all humans use only 10% of their brains.")
    assert it.stance == Stance.REFUTES


def test_heuristic_topical_overlap_is_not_support():
    it = _label("The Great Wall of China is visible from space with the naked eye.", "Great Wall of China",
                "The Great Wall of China is a series of fortifications built across the historical northern borders of China.")
    assert it.stance == Stance.NEUTRAL


def test_heuristic_close_restatement_supports():
    it = _label("The Eiffel Tower is located in Paris.", "Eiffel Tower",
                "The Eiffel Tower is a wrought-iron lattice tower located in Paris, France.")
    assert it.stance == Stance.SUPPORTS


def test_heuristic_number_conflict_refutes_and_close_is_caveated():
    wrong = _label("Mount Everest is 12,000 metres tall.", "Mount Everest", "Mount Everest is 8,849 metres tall above sea level.")
    assert wrong.stance == Stance.REFUTES
    close = _label("Mount Everest is 8,700 metres tall.", "Mount Everest", "Mount Everest is 8,849 metres tall above sea level.")
    assert close.stance == Stance.CAVEATED


def test_heuristic_flags_misleading_vocabulary():
    it = _label("Crime doubled last year in Springfield.", "Crime in Springfield",
                "The claim that crime doubled last year in Springfield is misleading and lacks context about reporting changes.")
    assert it.stance == Stance.MISLEADING


def test_heuristic_ambiguous_when_claim_itself_says_hoax():
    it = _label("The moon landing was a hoax.", "Moon landing conspiracy theories",
                "Moon landing conspiracy theories claim the landings were a hoax staged by NASA.")
    assert it.stance == Stance.NEUTRAL


def test_google_factcheck_ratings_map_to_stances():
    assert rating_to_stance("False")[0] == Stance.REFUTES
    assert rating_to_stance("Pants on Fire")[0] == Stance.REFUTES
    assert rating_to_stance("Mostly False")[0] == Stance.REFUTES and rating_to_stance("Mostly False")[1] < 0.8
    assert rating_to_stance("True")[0] == Stance.SUPPORTS
    assert rating_to_stance("Mostly True")[0] == Stance.CAVEATED
    assert rating_to_stance("Half True")[0] == Stance.MIXED
    assert rating_to_stance("Misleading")[0] == Stance.MISLEADING
    assert rating_to_stance("Missing Context")[0] == Stance.MISLEADING
    assert rating_to_stance("Unproven")[0] == Stance.NEUTRAL


# ---------------------------------------------------------------- agreement
CLAIM = parse_claim("The Eiffel Tower is located in Paris.")


def test_agreement_caps_a_single_domain():
    items = [scored(Stance.SUPPORTS, domain="en.wikipedia.org", id=i) for i in range(1, 6)]
    a = agr.analyze(CLAIM, items)
    assert a.support_w == pytest.approx(1.0)      # five pages from one domain still count as one HIGH source
    assert a.support_domains == 1


def test_agreement_neutral_and_irrelevant_do_not_count():
    items = [scored(Stance.NEUTRAL, id=1), scored(Stance.SUPPORTS, relevance=0.1, id=2), scored(Stance.SUPPORTS, conf=0.05, id=3)]
    a = agr.analyze(CLAIM, items)
    assert a.decisive_w == 0 and a.level == "insufficient"


def test_agreement_contradiction_surfaces_both_sides():
    items = [scored(Stance.SUPPORTS, domain="a.gov", id=1), scored(Stance.REFUTES, domain="b.gov", id=2)]
    a = agr.analyze(CLAIM, items)
    assert a.level == "contradiction"
    assert [i.domain for i in a.supporting] == ["a.gov"] and [i.domain for i in a.refuting] == ["b.gov"]
    assert a.support_high and a.refute_high


def test_agreement_tier_weights_order_sources():
    hi = agr.analyze(CLAIM, [scored(Stance.SUPPORTS, tier=Tier.HIGH, domain="a.gov")])
    lo = agr.analyze(CLAIM, [scored(Stance.SUPPORTS, tier=Tier.LOW, domain="blog.example")])
    assert hi.support_w > lo.support_w
    assert lo.credible_decisive == 0 and hi.credible_decisive == 1


def test_agreement_rejected_tier_never_counts():
    a = agr.analyze(CLAIM, [scored(Stance.SUPPORTS, tier=Tier.REJECTED, domain="infowars.com")])
    assert a.decisive_w == 0


def test_freshness_penalises_undated_and_old_evidence_for_time_sensitive_claims():
    today = dt.date(2026, 11, 17)
    live = parse_claim("The president signed the bill today.")
    fresh = ev("https://a.gov/x", published="2026-11-16")
    old = ev("https://a.gov/y", published="2025-01-01")
    undated = ev("https://a.gov/z")
    assert agr.freshness(fresh, live, today) == 1.0
    assert agr.freshness(old, live, today) < agr.freshness(undated, live, today) < 1.0
    assert agr.freshness(old, CLAIM, today) == 1.0   # atemporal claim: age is irrelevant


# ---------------------------------------------------------------- synthesis: no fabricated URLs
class ScriptedLLM(LLMClient):
    def __init__(self, reply=None, exc=None):
        super().__init__(make_settings(anthropic_api_key="test"))
        self.reply, self.exc = reply, exc

    async def complete(self, system, user, *, max_tokens, timeout):
        if self.exc:
            raise self.exc
        return self.reply


def _setup():
    items = [scored(Stance.SUPPORTS, domain="a.gov", id=1), scored(Stance.SUPPORTS, domain="b.edu", id=2)]
    a = agr.analyze(CLAIM, items)
    d = VerdictDecision(Verdict.TRUE, "R4_true", "Multiple independent sources support the claim.")
    return items, a, d


async def test_llm_cannot_inject_urls_or_unknown_sources():
    items, a, d = _setup()
    reply = ('{"reasoning": "Both a.gov and b.edu confirm it, see https://evil.example/fake for more. '
             'The sources agree on the location.", "source_ids": [1, 99, "x", 2]}')
    reasoning, urls, mode = await Synthesizer(ScriptedLLM(reply)).synthesize(CLAIM, d, a, items, 2.0)
    assert mode == "llm"
    assert "evil.example" not in reasoning and "http" not in reasoning
    retrieved = {i.evidence.url for i in items}
    assert urls and set(urls) <= retrieved and len(urls) == len(set(urls))


async def test_llm_url_in_source_ids_is_ignored_and_falls_back_to_retrieved():
    items, a, d = _setup()
    reply = '{"reasoning": "Both a.gov and b.edu confirm the claim. They agree on all points.", "source_ids": ["https://evil.example"]}'
    _, urls, _ = await Synthesizer(ScriptedLLM(reply)).synthesize(CLAIM, d, a, items, 2.0)
    assert set(urls) == {i.evidence.url for i in items}


@pytest.mark.parametrize("reply", ["not json at all", '{"reasoning": 5}', '{"reasoning": "short"}', ""])
async def test_malformed_llm_output_falls_back_to_template(reply):
    items, a, d = _setup()
    reasoning, urls, mode = await Synthesizer(ScriptedLLM(reply)).synthesize(CLAIM, d, a, items, 2.0)
    assert mode.startswith("template") and len(reasoning) > 30 and urls


async def test_llm_exception_falls_back_to_template():
    items, a, d = _setup()
    _, _, mode = await Synthesizer(ScriptedLLM(exc=asyncio.TimeoutError())).synthesize(CLAIM, d, a, items, 2.0)
    assert mode == "template(llm_failed)"


async def test_no_key_uses_template_and_names_supporting_sources():
    items, a, d = _setup()
    reasoning, urls, mode = await Synthesizer(LLMClient(make_settings())).synthesize(CLAIM, d, a, items, 2.0)
    assert mode == "template" and "a.gov" in reasoning and "b.edu" in reasoning


def test_clean_llm_reasoning_validation():
    assert clean_llm_reasoning("A" * 10) is None
    assert clean_llm_reasoning("**Bold** claim is supported by example.org. It agrees with others.") == \
        "Bold claim is supported by example.org. It agrees with others."
    assert clean_llm_reasoning("One. Two. Three. Four. Five. Six. Seven. Eight, and some filler to pass length.") is None


def test_unverified_selects_no_sources_when_nothing_relevant():
    a = agr.analyze(CLAIM, [scored(Stance.NEUTRAL, relevance=0.1)])
    assert select_sources(Verdict.UNVERIFIED, a, []) == []


# ---------------------------------------------------------------- resilience
def test_circuit_breaker_opens_then_half_opens_then_closes():
    now = [0.0]
    b = CircuitBreaker(failure_threshold=3, cooldown_s=10, clock=lambda: now[0])
    assert b.state == "closed"
    for _ in range(3):
        assert b.allow()
        b.record_failure()
    assert b.state == "open" and not b.allow()
    now[0] = 11
    assert b.state == "half_open" and b.allow() and not b.allow()   # a single probe at a time
    b.record_success()
    assert b.state == "closed" and b.allow()


def test_circuit_breaker_failed_probe_reopens():
    now = [0.0]
    b = CircuitBreaker(failure_threshold=1, cooldown_s=5, clock=lambda: now[0])
    b.record_failure()
    now[0] = 6
    assert b.allow()
    b.record_failure()
    assert b.state == "open"


async def test_retry_recovers_from_one_transient_failure():
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom")
        return "ok"

    assert await call_with_retry(flaky, CircuitBreaker(), backoff_s=0.01) == "ok" and calls["n"] == 2


async def test_non_transient_errors_are_not_retried_and_do_not_trip_breaker():
    calls = {"n": 0}
    b = CircuitBreaker(failure_threshold=1)

    async def bad():
        calls["n"] += 1
        raise ValueError("bug")

    with pytest.raises(ValueError):
        await call_with_retry(bad, b, backoff_s=0.01)
    assert calls["n"] == 1 and b.state == "closed"


async def test_open_circuit_fails_fast():
    b = CircuitBreaker(failure_threshold=1, cooldown_s=60)
    b.record_failure()

    async def never():
        raise AssertionError("must not be called")

    with pytest.raises(CircuitOpen):
        await call_with_retry(never, b)


# ---------------------------------------------------------------- cache
def test_cache_ttl_and_lru():
    now = [0.0]
    c = TTLCache(max_items=2, clock=lambda: now[0])
    c.set("a", 1, 10)
    c.set("b", 2, 10)
    c.set("c", 3, 10)
    assert c.get("a") is None and c.get("b") == 2 and c.get("c") == 3   # LRU evicted "a"
    now[0] = 11
    assert c.get("b") is None
    c.set("z", 1, 0)
    assert c.get("z") is None                                           # ttl<=0 is never stored


async def test_single_flight_runs_factory_once():
    c = TTLCache()
    n = {"calls": 0}

    async def factory():
        n["calls"] += 1
        await asyncio.sleep(0.05)
        return "v"

    results = await asyncio.gather(*[c.single_flight("k", factory) for _ in range(10)])
    assert results == ["v"] * 10 and n["calls"] == 1


# ---------------------------------------------------------------- LLM stance labeller (scripted client, no network)
from factcheck.stance import LLMStanceLabeller, StanceService


def _items():
    c = parse_claim("The Eiffel Tower is located in Paris.")
    a = scored(Stance.NEUTRAL, domain="a.gov", id=1)
    b = scored(Stance.NEUTRAL, domain="b.edu", id=2)
    a.evidence.title = b.evidence.title = "Eiffel Tower"
    a.evidence.snippet = b.evidence.snippet = "The Eiffel Tower is a wrought-iron lattice tower located in Paris."
    return c, [a, b]


async def test_llm_labels_applied_and_validated():
    c, items = _items()
    reply = ('```json\n{"labels":[{"id":1,"stance":"Refutes","relevance":0.9,"confidence":0.8,"note":"x"},'
             '{"id":2,"stance":"bogus","relevance":1,"confidence":1},{"id":99,"stance":"supports","relevance":1,"confidence":1},'
             '{"id":2,"stance":"misleading","relevance":5,"confidence":-3}]}\n```')
    await LLMStanceLabeller(ScriptedLLM(reply)).label(c, items, 2.0)
    assert items[0].stance == Stance.REFUTES and items[0].stance_confidence == 0.8
    assert items[1].stance == Stance.MISLEADING and items[1].relevance == 1.0 and items[1].stance_confidence == 0.0   # clamped


async def test_stance_service_falls_back_to_heuristic_when_llm_fails():
    c, items = _items()
    svc = StanceService(CredibilityScorer(), ScriptedLLM(exc=asyncio.TimeoutError()), mode="auto")
    mode = await svc.label(c, items, 2.0)
    assert mode == "heuristic(llm_failed)"
    assert all(i.relevance > 0 for i in items)              # heuristic labels are still in place


async def test_stance_service_heuristic_mode_never_calls_llm():
    c, items = _items()
    llm = ScriptedLLM(exc=AssertionError("must not be called"))
    assert await StanceService(CredibilityScorer(), llm, mode="heuristic").label(c, items, 2.0) == "heuristic"
    assert await StanceService(CredibilityScorer(), LLMClient(make_settings()), mode="auto").label(c, items, 2.0) == "heuristic"


async def test_stance_service_unusable_llm_output_keeps_heuristic():
    """An unusable reply must NOT wipe the heuristic labels it is falling back to."""
    c, items = _items()
    mode = await StanceService(CredibilityScorer(), ScriptedLLM('{"labels":[]}'), mode="auto").label(c, items, 2.0)
    assert mode == "heuristic(llm_failed)"
    assert any(i.stance != Stance.NEUTRAL and i.stance_confidence > 0 for i in items),         "heuristic labels were erased before the fallback could use them"
    assert all(i.labeller == "heuristic" for i in items)


# ---------------------------------------------------------------- parser must-stay-checkable regressions
@pytest.mark.parametrize("text", [
    "Parasite won the Academy Award for Best Picture.",          # "Best" inside a proper name
    "Will Smith slapped Chris Rock at the Oscars.",              # "Will" is a given name
    "The 2028 Olympics will be held in Los Angeles.",            # scheduled => a checkable present fact
    "The Best Man is a 1999 film.",
    "Best Buy was founded in 1966.",
    "Apple is the most valuable company in the world by market capitalisation.",
    "The next total solar eclipse in Spain is scheduled for August 2026.",
])
def test_named_entities_and_scheduled_events_stay_checkable(text):
    p = parse_claim(text, today=dt.date(2026, 11, 17))
    assert p.checkable, f"{text!r} was wrongly rejected: {p.reject_reason}"


@pytest.mark.parametrize("text", [
    "Bitcoin will reach $1,000,000 by 2030.",
    "The team will win the title next year.",
    "House prices are likely to fall next quarter.",
])
def test_genuine_predictions_still_rejected(text):
    assert not parse_claim(text, today=dt.date(2026, 11, 17)).checkable


# ---------------------------------------------------------------- heuristic: attributed claims are not support
@pytest.mark.parametrize("title,snippet", [
    ("Activist says vaccines cause autism", "At a rally, the activist said vaccines cause autism, repeating a widely shared statement."),
    ("Candidate claims vaccines cause autism", "The candidate argues vaccines cause autism and called for new studies."),
    ("Group maintains vaccines cause autism", "Speakers maintained that vaccines cause autism, according to organisers."),
])
def test_reporting_someone_elses_claim_is_not_evidence_for_it(title, snippet):
    it = _label("Vaccines cause autism.", title, snippet, url="https://www.nytimes.com/x", provider="tavily-general")
    assert it.stance == Stance.NEUTRAL, it.note


def test_direct_statement_from_news_still_supports():
    it = _label("The Eiffel Tower is located in Paris.", "Eiffel Tower reopens",
                "The Eiffel Tower, located in Paris, France, reopened to visitors on Monday.",
                url="https://www.nytimes.com/y", provider="tavily-general")
    assert it.stance == Stance.SUPPORTS


def test_labeller_is_recorded_for_published_ratings():
    from factcheck.models import Evidence
    s = CredibilityScorer()
    item = scored(Stance.NEUTRAL, domain="snopes.com", tier=Tier.HIGH)
    item.evidence = Evidence(url="https://www.snopes.com/fact-check/x", title="t", snippet="rated False",
                             provider="google-factcheck", preset_stance=Stance.REFUTES, preset_confidence=0.9,
                             fact_check_rating="False")
    HeuristicStanceLabeller(s).label_one(parse_claim("Vaccines cause autism."), item)
    assert item.labeller == "source" and item.stance == Stance.REFUTES


async def test_llm_mode_neutralises_items_it_did_not_label():
    """Heuristic guesses must not sit in the same weighting as model labels."""
    c, items = _items()
    extra = scored(Stance.NEUTRAL, domain="c.org", id=3)
    extra.evidence.title = "Eiffel Tower"
    extra.evidence.snippet = "The Eiffel Tower is located in Paris."
    items.append(extra)
    reply = '{"labels":[{"id":1,"stance":"supports","relevance":0.9,"confidence":0.8}]}'
    svc = StanceService(CredibilityScorer(), ScriptedLLM(reply), mode="auto")
    assert await svc.label(c, items, 2.0) == "llm"
    assert items[0].labeller == "llm" and items[0].stance == Stance.SUPPORTS
    assert all(i.stance == Stance.NEUTRAL and i.stance_confidence == 0.0 for i in items[1:])


# ---------------------------------------------------------------- attribution guard: precision + recall
def test_attribution_guard_is_token_level_not_boolean():
    """A claim that merely CONTAINS an attribution word must not switch the whole guard off."""
    from factcheck.stance import _is_attributed
    # "United States" must not read as the speech verb "states", in either direction
    assert _is_attributed("Speakers maintained that the 2020 United States election was stolen.",
                          "The 2020 United States election was stolen.")
    assert not _is_attributed("The Statue of Liberty stands in New York Harbor, United States.",
                              "The Statue of Liberty is in the United States.")
    # a claim that is itself about what someone said keeps its own attribution words
    assert not _is_attributed("The minister said the policy would be reversed.",
                              "The minister said the policy would be reversed.")


def test_attributed_claim_about_an_entity_with_a_colliding_name_abstains():
    it = _label("The 2020 United States election was stolen.",
                "Group says the 2020 United States election was stolen",
                "Speakers maintained that the 2020 United States election was stolen, according to organisers.",
                url="https://www.politico.com/x", provider="tavily-general")
    assert it.stance == Stance.NEUTRAL, it.note


def test_us_located_fact_still_supports():
    it = _label("The Statue of Liberty is located in the United States.", "Statue of Liberty",
                "The Statue of Liberty is a colossal statue on Liberty Island in New York Harbor, United States.",
                url="https://www.britannica.com/a", provider="tavily-general")
    assert it.stance == Stance.SUPPORTS, it.note


@pytest.mark.parametrize("title,is_generic", [
    ("List of highest mountains on Earth", True),
    ("Timeline of Mount Everest expeditions", True),
    ("History of the Great Wall of China", True),
    ("Listeria", False),          # must not match "list"
    ("Historical revisionism", False),
    ("Indexation", False),
    ("Categorical imperative", False),
])
def test_generic_page_detection_has_word_boundaries(title, is_generic):
    from factcheck.stance import _GENERIC_PAGE
    assert bool(_GENERIC_PAGE.match(title)) is is_generic


@pytest.mark.parametrize("text", [
    "Biden announced he will not seek re-election.",
    "The will of Alfred Nobel established the Nobel Prizes.",
    "The company said it will open a factory in Ohio.",
])
def test_reported_future_intentions_stay_checkable(text):
    p = parse_claim(text, today=dt.date(2026, 11, 17))
    assert p.checkable, f"{text!r} wrongly rejected: {p.reject_reason}"


# ---------------------------------------------------------------- parser: time markers need a future verb
@pytest.mark.parametrize("text", [
    "US national debt passed $30 trillion by 2022.",
    "He resigned the next year.",
    "The firm went bankrupt within 2 years.",
    "Hate speech is protected by the First Amendment.",
    "US presidents must be natural-born citizens.",
])
def test_past_facts_with_time_words_are_not_predictions(text):
    p = parse_claim(text, today=dt.date(2026, 11, 17))
    assert p.checkable, f"{text!r} wrongly rejected: {p.reject_reason}"


@pytest.mark.parametrize("text", [
    "Bitcoin will reach $1,000,000 by 2030.",
    "The team will win the title next year.",
    "House prices are likely to fall next quarter.",
    "Stocks are expected to rally.",
])
def test_forecasts_are_still_rejected(text):
    assert not parse_claim(text, today=dt.date(2026, 11, 17)).checkable
