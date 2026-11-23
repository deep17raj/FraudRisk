from __future__ import annotations

import socket

import httpx
import pytest
from fastapi.testclient import TestClient

from intents.return_policy.app import ReturnPolicyPipeline, ReturnPolicyResponse, create_app as create_returns, retailer_name
from intents.return_policy.config import Settings as ReturnSettings
from intents.return_policy.parse import (best_window, conditions_of, find_windows, number_value, page_text, policy_links,
                                         registrable)
from intents.sla_compliance.app import SlaPipeline, SlaResponse, create_app as create_sla, published_period, target_in
from intents.sla_compliance.config import Settings as SlaSettings
from intents.token_holders.app import TokenHoldersPipeline, TokenHoldersResponse, create_app as create_holders, parse_chain
from intents.token_holders.config import Settings as HolderSettings
from tests.intents.helpers import FakeHttp, StubPipeline, fast

USDT = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
ETH_BS = "https://eth.blockscout.com/api/v2/tokens"
PLORER = "https://api.ethplorer.io/getTokenInfo/"
ROUTESCAN = "https://api.routescan.io/v2/network/mainnet/evm"


# ---- token holders ------------------------------------------------------------------------------------------
def holders_http(blockscout=None, ethplorer=None, routescan=None, listing=None) -> FakeHttp:
    def blockscout_rule(req: httpx.Request):
        if req.url.path.endswith("/api/v2/tokens"):
            return listing
        return blockscout
    return FakeHttp({ETH_BS: blockscout_rule, PLORER: ethplorer, ROUTESCAN: routescan,
                     "https://optimism.blockscout.com/api/v2/tokens": {"holders_count": "671913", "symbol": "USDC"},
                     "https://base.blockscout.com/api/v2/tokens": 403})


async def holders(fake, token, chain="ethereum"):
    return (await TokenHoldersPipeline(fast(HolderSettings), fake.client()).answer({"token": token, "chain": chain})).model_dump()


async def test_the_primary_source_answers_and_the_count_is_an_integer():
    j = await holders(holders_http({"holders_count": "17214727", "symbol": "USDT"}, {"holdersCount": 15663732}), USDT)
    assert j == {"token": USDT, "chain": "ethereum", "holders_count": 17214727, "token_symbol": "USDT"}


async def test_sources_are_tried_in_order_and_never_averaged():
    both = holders_http({"holders_count": "100"}, {"holdersCount": 80, "symbol": "X"}, {"count": 60, "countType": "approx"})
    assert (await holders(both, USDT))["holders_count"] == 100
    no_primary = holders_http(None, {"holdersCount": 80, "symbol": "X"}, {"count": 60})
    assert (await holders(no_primary, USDT))["holders_count"] == 80
    only_approx = holders_http(None, None, {"count": 60, "countType": "approx"})
    assert (await holders(only_approx, USDT))["holders_count"] == 60
    assert (await holders(holders_http(), USDT))["holders_count"] is None


@pytest.mark.parametrize("value,expected", [("17,214,727", 17214727), (5, 5), ("abc", None), (None, None), ("-3", None), ("99999999999", None)])
async def test_odd_counts_are_parsed_or_dropped(value, expected):
    assert (await holders(holders_http({"holders_count": value}), USDT))["holders_count"] == expected


async def test_a_symbol_picks_the_verified_reputable_token_with_the_biggest_market_cap_not_a_copycat():
    listing = {"items": [
        {"symbol": "USDC", "name": "USDC (fake)", "address_hash": "0x" + "1" * 40, "holders_count": "999", "circulating_market_cap": "5", "reputation": "scam"},
        {"symbol": "USDC", "name": "USD Coin", "address_hash": "0x" + "a" * 40, "holders_count": "9622443", "circulating_market_cap": "68000000000", "reputation": "ok"},
        {"symbol": "SUSDC", "name": "Spark USDC", "address_hash": "0x" + "b" * 40, "holders_count": "4248", "circulating_market_cap": "205680090", "reputation": "ok"}]}
    j = await holders(holders_http(listing=listing), "usdc")
    assert (j["holders_count"], j["token_symbol"]) == (9622443, "USDC")
    assert (await holders(holders_http(listing={"items": []}), "nonexistent"))["holders_count"] is None
    assert (await holders(holders_http(listing={"items": [{"symbol": "ABC", "address_hash": "0x" + "c" * 40}]}), "usdc"))["holders_count"] is None


async def test_an_address_in_the_request_is_always_used_as_given():
    fake = holders_http({"holders_count": "7", "symbol": "T"})
    await holders(fake, f"the token {USDT} please")
    assert any(USDT in str(c.url) for c in fake.calls)


@pytest.mark.parametrize("token,chain", [("", "ethereum"), (USDT, "dogechain"), (USDT, "")])
async def test_missing_token_or_unsupported_chain_is_null(token, chain):
    j = await holders(holders_http({"holders_count": "7"}), token, chain)
    assert j["holders_count"] is None


async def test_other_chains_use_their_own_explorer_and_a_bot_challenge_is_a_null_not_a_workaround():
    assert (await holders(holders_http(), "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85", "Optimism"))["holders_count"] == 671913
    base = await holders(holders_http(), "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "base")
    assert base["holders_count"] is None and base["chain"] == "base"


@pytest.mark.parametrize("raw,chain", [("ETH", "ethereum"), ("Ethereum Mainnet", "ethereum"), ("OP", "optimism"), ("polygon pos", "polygon"),
                                       ("Arbitrum One", "arbitrum"), ("avax", "avalanche"), ("tokens on base", "base"), ("nope", None), ("", None)])
def test_chain_names(raw, chain):
    assert parse_chain(raw) == chain


def test_holders_http_contract():
    with TestClient(create_holders(fast(HolderSettings), pipeline=StubPipeline(TokenHoldersResponse, holders_count=5))) as c:
        r = c.post("/token-holders", json={"token": USDT, "chain": "ethereum"})
    assert set(r.json()) == {"token", "chain", "holders_count", "token_symbol"}


# ---- SLA compliance -------------------------------------------------------------------------------------------
SUMMARY = "https://raw.githubusercontent.com/upptime/upptime/HEAD/history/summary.json"
ROWS = [{"name": "Google", "url": "https://www.google.com", "slug": "google", "uptime": "99.97%", "uptimeDay": "70.68%",
         "uptimeWeek": "94.75%", "uptimeMonth": "98.79%", "uptimeYear": "99.83%"},
        {"name": "Hacker News", "url": "https://news.ycombinator.com", "slug": "hacker-news", "uptime": "99.15%", "uptimeDay": "100.00%",
         "uptimeWeek": "100.00%", "uptimeMonth": "100.00%", "uptimeYear": "100.00%"}]


async def sla(rows, service, period="", **kw):
    pl = SlaPipeline(fast(SlaSettings, **kw), FakeHttp({SUMMARY: rows}).client())
    return (await pl.answer({"service": service, "period": period})).model_dump()


@pytest.mark.parametrize("text,expected", [
    ("last 24 hours", "day"), ("past day", "day"), ("1 day", "day"), ("last 7 days", "week"), ("last week", "week"), ("30 days", "month"),
    ("last 30 days", "month"), ("last month", "month"), ("last 365 days", "year"), ("last year", "year"), ("12 months", "year"),
    ("all time", "all"), ("overall", "all"), ("last 14 days", None), ("last 90 days", None), ("last 2 weeks", None), ("", None), ("someday", None)])
def test_only_the_published_windows_are_recognised(text, expected):
    assert published_period(text) == expected


async def test_the_published_figure_is_returned_verbatim_for_each_window():
    figures = {"last 24 hours": 70.68, "last 7 days": 94.75, "last month": 98.79, "last year": 99.83, "all time": 99.97}
    for period, value in figures.items():
        assert (await sla(ROWS, "Google", period))["uptime_pct"] == value, period


@pytest.mark.parametrize("service", ["Google", "google", "google.com", "https://www.google.com/", "GOOGLE "])
async def test_services_are_matched_by_name_slug_or_site(service):
    assert (await sla(ROWS, service, "last month"))["uptime_pct"] == 98.79
    assert (await sla(ROWS, "hacker news", "last month"))["uptime_pct"] == 100.0


async def test_an_unpublished_window_or_unmonitored_service_is_null_never_computed():
    assert (await sla(ROWS, "Google", "last 14 days"))["uptime_pct"] is None
    assert (await sla(ROWS, "Google", ""))["uptime_pct"] is None
    assert (await sla(ROWS, "GitHub API", "last month"))["uptime_pct"] is None
    assert (await sla(None, "Google", "last month"))["uptime_pct"] is None


async def test_the_target_comes_only_from_the_request_and_compliance_needs_both_numbers():
    met = await sla(ROWS, "Hacker News 99.9%", "last month")
    assert (met["uptime_pct"], met["sla_target_pct"], met["compliant"]) == (100.0, 99.9, True)
    missed = await sla(ROWS, "Google 99.9%", "last month")
    assert (missed["uptime_pct"], missed["sla_target_pct"], missed["compliant"]) == (98.79, 99.9, False)
    no_target = await sla(ROWS, "Hacker News", "last month")
    assert (no_target["sla_target_pct"], no_target["compliant"]) == (None, False)          # "cannot confirm", not "met"
    assert (await sla(ROWS, "Google", "last month, SLA 98%"))["compliant"] is True
    assert target_in("no number here") is None and target_in("0%") is None and target_in("150%") is None


async def test_the_summary_is_cached_between_requests():
    fake = FakeHttp({SUMMARY: ROWS})
    pl = SlaPipeline(fast(SlaSettings), fake.client())
    await pl.answer({"service": "Google", "period": "last month"})
    await pl.answer({"service": "Hacker News", "period": "last week"})
    assert fake.count(SUMMARY) == 1


def test_sla_http_contract():
    with TestClient(create_sla(fast(SlaSettings), pipeline=StubPipeline(SlaResponse, uptime_pct=99.5))) as c:
        r = c.post("/sla-compliance", json={"service": "Google", "period": "last 30 days"})
    assert set(r.json()) == {"service", "uptime_pct", "sla_target_pct", "compliant", "period"}


# ---- return policy: parsing -----------------------------------------------------------------------------------
@pytest.mark.parametrize("text,days", [
    ("We have a 30-day return policy.", [30]), ("Items may be returned within 30 days of receipt.", [30]),
    ("You may return unopened items within thirty (30) days of delivery.", [30]), ("Returns are accepted within two weeks of purchase.", [14]),
    ("You can return it within one month of delivery for a refund.", [30]), ("Return within 14 calendar days from the date of delivery.", [14]),
    ("Your refund will be processed within 5 business days.", []), ("Refunds take 5 to 10 business days to appear on your card statement.", []),
    ("Standard shipping takes 3 days; returns are free.", []), ("Returns policy: see below.", []), ("We deliver within 2 days.", []),
    ("Return it within 60 days of purchase for a full refund. Refunds are processed within 5 business days.", [60])])
def test_only_return_windows_are_extracted(text, days):
    assert find_windows(text) == days


def test_best_window_prefers_the_most_stated_then_the_earliest():
    assert best_window([[30, 14], [30]]) == 30 and best_window([[14], [30]]) == 14 and best_window([[], []]) is None
    assert number_value("Thirty") == 30 and number_value("45") == 45 and number_value("lots") is None


def test_conditions_are_real_sentences_about_returns_and_deduplicated():
    text = ("Items must be returned unopened and in their original packaging. Items must be returned unopened and in their original packaging. "
            "A restocking fee of 15% applies to opened products. We love our customers a lot. Return shipping is paid by the buyer.")
    got = conditions_of([text])
    assert len(got) == 3 and any("restocking" in c for c in got) and not any("love" in c for c in got)


def test_page_text_drops_scripts_and_chrome_and_policy_links_stay_on_site():
    html = ("<html><nav>Menu returns</nav><script>var returns=1</script><body><p>Return within 30 days.</p>"
            "<a href='/pages/returns'>Returns & Refunds</a><a href='https://evil.example/refund'>Refund</a><a href='mailto:returns@x.com'>Mail returns</a>"
            "<a href='/about'>About</a></body></html>")
    assert "Menu" not in page_text(html) and "var returns" not in page_text(html) and "Return within 30 days." in page_text(html)
    assert policy_links(html, "https://shop.example/") == ["https://shop.example/pages/returns"]


@pytest.mark.parametrize("host,reg", [("www.adafruit.com", "adafruit.com"), ("store.arduino.cc", "arduino.cc"), ("shop.example.co.uk", "example.co.uk"), ("frame.work", "frame.work")])
def test_registrable_domain(host, reg):
    assert registrable(host) == reg


def test_retailer_names_are_cleaned_of_question_words():
    assert retailer_name("What is Adafruit's return policy?") == "Adafruit" and retailer_name("Best Buy") == "Best Buy"


# ---- return policy: the pipeline -------------------------------------------------------------------------------
@pytest.fixture
def public_dns(monkeypatch):
    def getaddrinfo(host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


def wikidata(entities: dict, search: list):
    def rule(req: httpx.Request):
        return {"search": [{"id": i} for i in search]} if req.url.params["action"] == "wbsearchentities" else {"entities": entities}
    return rule


def entity(label, desc, site):
    return {"labels": {"en": {"value": label}}, "descriptions": {"en": {"value": desc}},
            "claims": {"P856": [{"mainsnak": {"datavalue": {"value": site}}}]} if site else {}}


def returns_pipeline(routes) -> ReturnPolicyPipeline:
    return ReturnPolicyPipeline(fast(ReturnSettings, provider_timeout=2.0, global_timeout=10.0), FakeHttp(routes).client())


POLICY = "<html><body><h1>Returns</h1><p>Items may be returned within 30 days of receipt. Items must be unopened and in original packaging.</p></body></html>"


async def test_a_name_is_resolved_through_wikidata_to_the_company_and_its_page_is_parsed(public_dns):
    routes = {"https://www.wikidata.org/w/api.php": wikidata({"Q1": entity("Acme", "American electronics company", "https://www.acme.example/"),
                                                              "Q2": entity("Acme", "male given name", "https://given.example/")}, ["Q2", "Q1"]),
              "https://www.acme.example/": "<html><a href='/pages/returns'>Return policy</a></html>",
              "https://www.acme.example/pages/returns": POLICY}
    j = (await returns_pipeline(routes).answer({"retailer": "Acme"})).model_dump()
    assert j["return_window_days"] == 30 and j["verified"] is True and j["sources"] == ["https://www.acme.example/pages/returns"]
    assert any("unopened" in c for c in j["conditions"])


async def test_a_url_or_domain_needs_no_wikidata_and_common_paths_are_probed(public_dns):
    fake = FakeHttp({"https://shop.example/": "<html>home</html>", "https://shop.example/refund-policy": POLICY})
    pl = ReturnPolicyPipeline(fast(ReturnSettings, provider_timeout=2.0, global_timeout=10.0), fake.client())
    j = (await ReturnPolicyPipeline(fast(ReturnSettings, provider_timeout=2.0, global_timeout=10.0), fake.client()).answer({"retailer": "shop.example"})).model_dump()
    assert j["return_window_days"] == 30 and j["verified"] is True
    assert not any(c.url.host == "www.wikidata.org" for c in fake.calls)


async def test_known_retailers_use_their_locator_page(public_dns):
    routes = {"https://www.wikidata.org/w/api.php": wikidata({"Q1": entity("Adafruit", "electronics company", "http://adafruit.com")}, ["Q1"]),
              "https://adafruit.com/": "<html>home</html>", "https://www.adafruit.com/shipping": POLICY}
    j = (await returns_pipeline(routes).answer({"retailer": "Adafruit"})).model_dump()
    assert j["return_window_days"] == 30 and j["sources"] == ["https://www.adafruit.com/shipping"]


async def test_blocked_or_unparsable_pages_give_a_null_window_and_unverified(public_dns):
    routes = {"https://www.wikidata.org/w/api.php": wikidata({"Q1": entity("Blocked", "retailer", "https://blocked.example/")}, ["Q1"]),
              "https://blocked.example/": 403, "https://blocked.example/returns": 403}
    j = (await returns_pipeline(routes).answer({"retailer": "Blocked"})).model_dump()
    assert (j["return_window_days"], j["verified"], j["sources"]) == (None, False, [])
    page = "<html><p>We accept returns of unused items. Contact support to start a return.</p></html>"
    routes2 = {"https://shop2.example/": page, "https://shop2.example/returns": page}
    j2 = (await returns_pipeline(routes2).answer({"retailer": "https://shop2.example"})).model_dump()
    assert j2["return_window_days"] is None and j2["verified"] is False and "https://shop2.example/returns" in j2["sources"]


async def test_a_window_from_a_third_party_domain_is_not_verified(public_dns):
    routes = {"https://shop3.example/": "<html><a href='https://shop3.example/returns'>Returns</a></html>",
              "https://shop3.example/returns": httpx.Response(302, headers={"location": "https://other.example/policy"}),
              "https://other.example/policy": POLICY}
    j = (await returns_pipeline(routes).answer({"retailer": "https://shop3.example"})).model_dump()
    assert j["return_window_days"] == 30 and j["verified"] is False


async def test_an_unknown_retailer_and_an_empty_one_are_empty_answers(public_dns):
    routes = {"https://www.wikidata.org/w/api.php": wikidata({}, [])}
    assert (await returns_pipeline(routes).answer({"retailer": "Zzzz Nonexistent"})).return_window_days is None
    assert (await returns_pipeline(routes).answer({"retailer": ""})).retailer == ""


async def test_a_private_address_is_never_fetched(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port))])
    fake = FakeHttp({"http://intranet.example/": POLICY})
    pl = ReturnPolicyPipeline(fast(ReturnSettings, provider_timeout=2.0, global_timeout=10.0), fake.client())
    j = (await pl.answer({"retailer": "http://intranet.example/"})).model_dump()
    assert j["return_window_days"] is None and not any(c.url.host == "intranet.example" for c in fake.calls)


def test_returns_http_contract():
    with TestClient(create_returns(fast(ReturnSettings), pipeline=StubPipeline(ReturnPolicyResponse, retailer="x"))) as c:
        r = c.post("/return-policy", json={"retailer": "x"})
    assert set(r.json()) == {"retailer", "return_window_days", "conditions", "verified", "sources"}
