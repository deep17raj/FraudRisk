from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from intents.corporate_registry.app import API, RegistryPipeline, RegistryResponse, country_code, create_app as create_reg
from intents.corporate_registry.config import Settings as RegSettings
from intents.sec_filings.app import FTS, SUBMISSIONS, TICKERS, FilingsPipeline, FilingsResponse, create_app as create_sec
from intents.sec_filings.config import Settings as SecSettings
from intents.sec_filings.periods import parse_period
from intents.tvl.app import LLAMA, TvlPipeline, TvlResponse, create_app as create_tvl, split_request
from intents.tvl.config import Settings as TvlSettings
from tests.intents.helpers import FakeHttp, StubPipeline, fast

TODAY = date(2026, 11, 22)


# ---- periods ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("last 12 months", (date(2025, 11, 22), TODAY)), ("past year", (date(2025, 11, 22), TODAY)),
    ("last 30 days", (date(2026, 10, 23), TODAY)), ("last 2 weeks", (date(2026, 11, 8), TODAY)),
    ("last 6 months", (date(2026, 5, 22), TODAY)), ("2024", (date(2024, 1, 1), date(2024, 12, 31))),
    ("2026", (date(2026, 1, 1), TODAY)), ("Q3 2025", (date(2025, 7, 1), date(2025, 9, 30))),
    ("2025 Q1", (date(2025, 1, 1), date(2025, 3, 31))), ("ytd", (date(2026, 1, 1), TODAY)),
    ("since January 2025", (date(2025, 1, 1), TODAY)), ("March 2024", (date(2024, 3, 1), date(2024, 3, 31))),
    ("2025-01-01 to 2025-06-30", (date(2025, 1, 1), date(2025, 6, 30))),
    ("", (date(2025, 11, 22), TODAY)), ("whenever", (date(2025, 11, 22), TODAY)),
])
def test_periods(raw, expected):
    assert parse_period(raw, TODAY) == expected


# ---- SEC filings ------------------------------------------------------------------------------------------
TICKERS_JSON = {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
                "1": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"},
                "2": {"cik_str": 1318605, "ticker": "TSLA", "title": "Tesla, Inc."}}


def submissions(n=3):
    dates = ["2026-11-19", "2026-08-01", "2025-12-05", "2024-01-02"][:n]
    return {"filings": {"recent": {
        "accessionNumber": [f"0001140361-26-04495{i}" for i in range(n)], "filingDate": dates,
        "form": ["4", "10-Q", "8-K", "10-K"][:n], "primaryDocument": ["f4.xml", "q.htm", "k8.htm", "k.htm"][:n],
        "primaryDocDescription": ["FORM 4", "10-Q", "Current report", "Annual report"][:n]}}}


def sec_http(subs=None, fts=None, tickers=TICKERS_JSON) -> FakeHttp:
    return FakeHttp({TICKERS: tickers, SUBMISSIONS.format(cik=320193): subs if subs is not None else submissions(),
                     FTS: fts})


async def filings(fake, entity, period="last 12 months", **kw):
    return await FilingsPipeline(fast(SecSettings, **kw), fake.client()).answer({"entity": entity, "period": period})


async def test_a_registrants_filings_in_the_period_newest_first_with_urls(monkeypatch):
    import intents.sec_filings.app as mod
    monkeypatch.setattr(mod, "parse_period", lambda raw: (date(2025, 11, 22), date(2026, 11, 22)))
    r = await filings(sec_http(), "Apple Inc")
    assert r.filing_count == 3                       # the 2024 filing is outside the period
    assert r.filings[0] == "4: FORM 4 (2026-11-19)" and r.filings[2] == "8-K: Current report (2025-12-05)"
    assert r.urls[0] == "https://www.sec.gov/Archives/edgar/data/320193/000114036126044950/f4.xml"
    assert len(r.filings) == len(r.urls)


@pytest.mark.parametrize("entity", ["Apple", "apple inc.", "AAPL", "aapl"])
async def test_the_registrant_is_found_by_name_or_ticker(entity, monkeypatch):
    import intents.sec_filings.app as mod
    monkeypatch.setattr(mod, "parse_period", lambda raw: (date(2025, 11, 22), date(2026, 11, 22)))
    assert (await filings(sec_http(), entity)).filing_count == 3


async def test_the_count_is_complete_but_the_lists_are_capped(monkeypatch):
    import intents.sec_filings.app as mod
    monkeypatch.setattr(mod, "parse_period", lambda raw: (date(2025, 1, 1), date(2026, 12, 31)))
    n = 40
    subs = {"filings": {"recent": {"accessionNumber": [f"0000000000-26-{i:06d}" for i in range(n)],
                                   "filingDate": ["2026-01-01"] * n, "form": ["8-K"] * n,
                                   "primaryDocument": ["d.htm"] * n, "primaryDocDescription": [""] * n}}}
    r = await filings(sec_http(subs=subs), "Apple Inc", max_listed=5)
    assert r.filing_count == 40 and len(r.filings) == 5 == len(r.urls)


FTS_HITS = {"hits": {"total": {"value": 2, "relation": "eq"}, "hits": [
    {"_id": "0000894189-26-011015:doc.htm", "_source": {"ciks": ["0001924868"], "form": "8-K",
                                                        "display_names": ["Tidal Trust II  (CIK 0001924868)"], "file_date": "2026-03-31"}}]}}


async def test_a_concern_that_is_not_a_registrant_uses_full_text_search():
    fake = sec_http(fts=FTS_HITS)
    r = await filings(fake, "lithium battery recall")
    assert r.filing_count == 2 and r.filings == ["8-K Tidal Trust II (2026-03-31)"]
    assert r.urls == ["https://www.sec.gov/Archives/edgar/data/1924868/000089418926011015/doc.htm"]
    assert fake.count(FTS) == 1 and 'q="lithium battery recall"' in str(fake.calls[-1].url).replace("%22", '"').replace("+", " ")


async def test_edgar_being_down_is_null_not_zero():
    r = await filings(sec_http(subs=None, fts=None, tickers=None), "Apple Inc")
    assert r.filing_count is None
    r2 = await filings(FakeHttp({TICKERS: TICKERS_JSON, SUBMISSIONS.format(cik=320193): None}), "Apple Inc")
    assert r2.filing_count is None and r2.entity == "Apple Inc"


async def test_a_filings_question_in_the_entity_field_gives_the_entity_and_the_period(monkeypatch):
    import intents.sec_filings.app as mod
    seen = []
    monkeypatch.setattr(mod, "parse_period", lambda raw: (seen.append(raw), (date(2025, 11, 22), date(2026, 11, 22)))[1])
    r = await FilingsPipeline(fast(SecSettings), sec_http().client()).answer(
        {"entity": "How many SEC filings did Apple make in the last 12 months?", "period": ""})
    assert r.filing_count == 3 and r.entity == "Apple"
    assert "in the last 12 months" in seen


async def test_an_empty_entity_is_null():
    assert (await filings(sec_http(), "")).filing_count is None


def test_sec_http_contract():
    with TestClient(create_sec(fast(SecSettings), pipeline=StubPipeline(FilingsResponse, entity="x", filing_count=3))) as c:
        r = c.post("/regulatory-filings", json={"entity": "x", "period": "2025"})
    assert set(r.json()) == {"entity", "filing_count", "filings", "urls"}


# ---- corporate registry -----------------------------------------------------------------------------------
def lei_record(name, lei="L" * 18 + "01", country="US", status="ACTIVE", jurisdiction=None, created="1977-01-03T00:00:00Z"):
    return {"attributes": {"lei": lei, "entity": {
        "legalName": {"name": name}, "jurisdiction": jurisdiction or country, "status": status, "creationDate": created,
        "legalAddress": {"addressLines": ["1 Main St"], "city": "Town", "region": f"{country}-CA", "postalCode": "12345", "country": country}}}}


def gleif(by_name=None, fulltext=None, lei=None):
    def rule(request):
        p = request.url.params
        if request.url.path.endswith("/lei-records") or request.url.path.endswith("lei-records/"):
            if "filter[entity.legalName]" in p:
                v = by_name(p["filter[entity.legalName]"]) if callable(by_name) else by_name
            else:
                v = fulltext
            return None if v is None else {"data": v}
        return {"data": lei} if lei else 404
    return FakeHttp({API: rule})


async def lookup(fake, company, jurisdiction="", **kw):
    return await RegistryPipeline(fast(RegSettings, **kw), fake.client()).answer({"company": company, "jurisdiction": jurisdiction})


async def test_the_registered_entity_is_returned_with_every_spec_field():
    r = await lookup(gleif(by_name=[lei_record("Apple Inc.")]), "Apple Inc", "United States")
    assert r.model_dump() == {"legal_name": "Apple Inc.", "registration_id": "LLLLLLLLLLLLLLLLLL01", "active": True,
                              "jurisdiction": "US", "registered_address": "1 Main St, Town, US-CA, 12345, US",
                              "incorporated": "1977-01-03"}


async def test_a_registry_question_in_the_company_field_finds_the_company():
    fake = gleif(by_name=[lei_record("Apple Inc.")])
    r = await lookup(fake, "What is the LEI of Apple Inc.?")
    assert r.legal_name == "Apple Inc." and r.active is True
    assert any(c.url.params.get("filter[entity.legalName]") == "Apple Inc." for c in fake.calls)


async def test_the_best_name_wins_and_the_jurisdiction_breaks_ties():
    recs = [lei_record("Apple Ford, Inc."), lei_record("Apple Inc.", lei="A" * 18 + "01", country="IE"),
            lei_record("Apple Inc.", lei="B" * 18 + "01", country="US")]
    assert (await lookup(gleif(by_name=recs), "Apple Inc", "United States")).registration_id == "B" * 18 + "01"
    assert (await lookup(gleif(by_name=recs), "Apple Inc", "Ireland")).registration_id == "A" * 18 + "01"


async def test_legal_form_variants_are_searched_too():
    fake = gleif(by_name=lambda q: [lei_record("Siemens Aktiengesellschaft", country="DE")] if "Aktiengesellschaft" in q else [])
    r = await lookup(fake, "Siemens AG", "Germany")
    assert r.legal_name == "Siemens Aktiengesellschaft"


async def test_an_inactive_entity_is_not_active():
    r = await lookup(gleif(by_name=[lei_record("Old Co Ltd", status="INACTIVE")]), "Old Co Ltd")
    assert r.legal_name == "Old Co Ltd" and r.active is False


async def test_a_weak_match_is_not_returned():
    r = await lookup(gleif(by_name=[lei_record("Completely Different Holdings")], fulltext=[]), "Apple Inc")
    assert r == RegistryResponse()


async def test_full_text_search_is_the_fallback_when_the_name_filter_finds_nothing():
    r = await lookup(gleif(by_name=[], fulltext=[lei_record("Zeta Imports")]), "Zeta Imports")
    assert r.legal_name == "Zeta Imports"


async def test_an_lei_is_looked_up_directly():
    fake = gleif(lei=lei_record("Apple Inc.", lei="HWUPKR0MPOU8FGXBT394"))
    r = await lookup(fake, "hwupkr0mpou8fgxbt394")
    assert r.legal_name == "Apple Inc." and fake.calls[0].url.path.endswith("/HWUPKR0MPOU8FGXBT394")


async def test_gleif_down_or_empty_input_is_the_empty_answer():
    assert await lookup(gleif(by_name=None, fulltext=None), "Apple Inc") == RegistryResponse()
    assert await lookup(gleif(by_name=[lei_record("X")]), "") == RegistryResponse()


@pytest.mark.parametrize("raw,code", [("United States", "US"), ("UK", "GB"), ("de", "DE"), ("US-DE", "US"), ("Delaware", "US"),
                                      ("Federal Republic of Germany", "DE"), ("Narnia", None), ("", None)])
def test_country_codes(raw, code):
    assert country_code(raw) == code


def test_registry_http_contract():
    with TestClient(create_reg(fast(RegSettings), pipeline=StubPipeline(RegistryResponse, legal_name="X"))) as c:
        r = c.post("/corporate-registry", json={"company": "X", "jurisdiction": "US"})
    assert set(r.json()) == {"legal_name", "registration_id", "active", "jurisdiction", "registered_address", "incorporated"}


# ---- TVL --------------------------------------------------------------------------------------------------
def llama(tvl=None, chains=None, protocols=None, protocol=None) -> FakeHttp:
    tvl = tvl if tvl is not None else {"aave": 16e9, "uniswap": 3e9}

    def tvl_rule(request):
        slug = request.url.path.rsplit("/", 1)[1]
        return tvl.get(slug, 400)

    return FakeHttp({f"{LLAMA}/tvl/": tvl_rule,
                     f"{LLAMA}/v2/chains": chains if chains is not None else [{"name": "Ethereum", "tvl": 36e9}, {"name": "Solana", "tvl": 4.6e9}],
                     f"{LLAMA}/protocols": protocols if protocols is not None else [{"name": "Lido", "slug": "lido", "symbol": "LDO", "tvl": 16.5e9},
                                                                                    {"name": "Aave V3", "slug": "aave-v3", "symbol": "AAVE", "tvl": 15e9}],
                     f"{LLAMA}/protocol/": protocol if protocol is not None else {"currentChainTvls": {"Arbitrum": 4e8, "Ethereum": 13e9}}})


async def tvl_of(fake, protocol):
    return await TvlPipeline(fast(TvlSettings), fake.client()).answer({"protocol": protocol})


async def test_protocol_by_slug():
    r = await tvl_of(llama(), "Uniswap")
    assert (r.protocol_or_chain, r.tvl_usd, r.chain_tvl_usd) == ("Uniswap", 3e9, None) and r.as_of.endswith("Z")


async def test_a_chain_name_returns_the_chain_tvl():
    r = await tvl_of(llama(), "ethereum")
    assert (r.protocol_or_chain, r.tvl_usd) == ("Ethereum", 36e9)


async def test_a_protocol_on_a_named_chain_also_returns_its_value_on_that_chain():
    r = await tvl_of(llama(), "Aave on Arbitrum")
    assert (r.tvl_usd, r.chain_tvl_usd) == (16e9, 4e8)
    assert (await tvl_of(llama(), "Aave on Nowhere")).chain_tvl_usd is None


async def test_an_inexact_name_falls_back_to_the_protocol_list():
    r = await tvl_of(llama(), "Lido")
    assert (r.protocol_or_chain, r.tvl_usd) == ("Lido", 16.5e9)


async def test_unknown_protocol_and_outages_are_null():
    assert (await tvl_of(llama(), "nonexistent-zzz")).tvl_usd is None
    down = FakeHttp({})
    assert (await tvl_of(down, "aave")).tvl_usd is None
    assert (await tvl_of(llama(), "")).tvl_usd is None


def test_requests_are_split_into_a_name_and_a_chain():
    assert split_request("Aave on Arbitrum") == ("Aave", "Arbitrum")
    assert split_request("What is the TVL of Uniswap V3?") == ("Uniswap V3", "")
    assert split_request("lido (ethereum)") == ("lido", "ethereum")


def test_tvl_http_contract():
    with TestClient(create_tvl(fast(TvlSettings), pipeline=StubPipeline(TvlResponse, tvl_usd=1.0))) as c:
        r = c.post("/tvl-lookup", json={"protocol": "aave"})
    assert set(r.json()) == {"protocol_or_chain", "tvl_usd", "chain_tvl_usd", "as_of"}
