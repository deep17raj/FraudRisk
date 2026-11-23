"""Real sources, real network, no stubs. Deselected by default; run with `python -m pytest -m live tests/intents`.

Each intent is asked the questions Telegraph itself asks (the `question_keys` in docs/intent_specs/) plus a
few negatives. Assertions are on facts that are stable (a CVE's score, whether google.com has a valid
certificate), or on cross-checks against an independent source, never on a value that moves.
"""
from __future__ import annotations

import importlib
import os

import httpx
import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.live


def served(package: str):
    """The real app of an intents/<package> (real pipeline, real sources), started once per module."""
    app_mod = importlib.import_module(f"{package}.app")
    settings = importlib.import_module(f"{package}.config").Settings.from_env()
    return TestClient(app_mod.create_app(settings))


@pytest.fixture(scope="module")
def crypto():
    with served("intents.crypto_price") as c:
        yield c


@pytest.fixture(scope="module")
def sanctions():
    with served("intents.sanctions") as c:
        yield c


@pytest.fixture(scope="module")
def threat():
    with served("intents.threatintel") as c:
        yield c


@pytest.fixture(scope="module")
def urlscan():
    with served("intents.urlscan") as c:
        yield c


# ---- CRYPTO_PRICE_USD (question keys BTC ETH SOL BNB XRP) --------------------------------------------------
@pytest.mark.parametrize("symbol", ["BTC", "ETH", "SOL", "BNB", "XRP"])
def test_CRYPTO_PRICE_USD_agrees_with_an_independent_exchange(crypto, symbol):
    got = crypto.post("/crypto-price", json={"symbol": symbol}).json()["price_usd"]
    ref = float(httpx.get(f"https://api.coinbase.com/v2/prices/{symbol}-USD/spot", timeout=15).json()["data"]["amount"])
    assert got is not None and abs(got - ref) / ref < 0.02, (got, ref)


def test_CRYPTO_PRICE_USD_unknown_asset_is_null(crypto):
    assert crypto.post("/crypto-price", json={"symbol": "NOTACOINXYZ"}).json() == {"price_usd": None}


# ---- SANCTIONS_SCREENING_MATCH_GENERAL --------------------------------------------------------------------
def test_SANCTIONS_SCREENING_MATCH_GENERAL_known_and_unknown_names(sanctions):
    hit = sanctions.post("/sanctions-screening", json={"name": "Banco Nacional de Cuba"}).json()
    assert hit["matched"] and hit["match_score"] == 1.0 and "OFAC SDN" in hit["lists"] and hit["matches"]
    assert sanctions.post("/sanctions-screening", json={"name": "Vladimir Putin"}).json()["matched"]
    clear = sanctions.post("/sanctions-screening", json={"name": "Taylor Swift"}).json()
    assert not clear["matched"] and clear["matches"] == [] and clear["lists"]


# ---- THREAT_INTELLIGENCE_INDICATOR (question keys 8.8.8.8 1.1.1.1 185.220.101.4 malware.wicar.org) ----------
@pytest.mark.parametrize("indicator,malicious", [("8.8.8.8", False), ("1.1.1.1", False), ("185.220.101.4", True),
                                                 ("malware.wicar.org", True)])
def test_THREAT_INTELLIGENCE_INDICATOR_question_keys(threat, indicator, malicious):
    assert threat.post("/threat-intelligence", json={"indicator": indicator}).json() == {"malicious": malicious}


# ---- URL_SCAN_VERDICT (question keys google.com, example.com, the EICAR URL) -------------------------------
@pytest.mark.parametrize("url,safe", [("https://www.google.com/", True), ("https://example.com/", True),
                                      ("http://malware.wicar.org/data/eicar.com", False)])
def test_URL_SCAN_VERDICT_question_keys(urlscan, url, safe):
    assert urlscan.post("/url-scan", json={"url": url}).json() == {"safe": safe}


# ---- SSL_VERIFICATION_CERT (question keys google.com github.com expired.badssl.com self-signed.badssl.com) -----
@pytest.mark.parametrize("host,valid", [("google.com", True), ("github.com", True), ("expired.badssl.com", False),
                                        ("self-signed.badssl.com", False), ("wrong.host.badssl.com", False)])
def test_SSL_VERIFICATION_CERT_question_keys(host, valid):
    with served("intents.ssl_cert") as c:
        assert c.post("/ssl-verification", json={"host": host}).json() == {"certificate_valid": valid}


def test_SSL_VERIFICATION_CERT_never_connects_to_private_addresses():
    with served("intents.ssl_cert") as c:
        for host in ("127.0.0.1", "localhost", "10.0.0.1", "169.254.169.254"):
            assert c.post("/ssl-verification", json={"host": host}).json() == {"certificate_valid": False}


# ---- CVE_LOOKUP_CVSS / VULNERABILITY_TRIAGE_GENERAL (question keys Log4Shell, XZ, Heartbleed, EternalBlue) ----
@pytest.mark.parametrize("cve,score", [("CVE-2021-44228", 10.0), ("CVE-2024-3094", 10.0), ("CVE-2014-0160", 7.5),
                                       ("CVE-2017-0144", 8.8)])
def test_CVE_LOOKUP_CVSS_and_VULNERABILITY_TRIAGE_GENERAL_question_keys(cve, score):
    with served("intents.cve") as c:
        full = c.post("/cve-lookup", json={"cve_id": cve}).json()
    assert full["cvss_score"] == score and full["severity"] in ("HIGH", "CRITICAL") and full["description"]
    with served("intents.vulntriage") as c:
        assert c.post("/vulnerability-triage", json={"cve_id": cve}).json() == {"cvss_base_score": score}


def test_CVE_LOOKUP_CVSS_unknown_cve_is_null_not_zero():
    with served("intents.cve") as c:
        r = c.post("/cve-lookup", json={"cve_id": "CVE-2099-99999"}).json()
    assert r["cvss_score"] is None and r["severity"] is None


# ---- WEATHER_CHECK_TEMPERATURE (question keys London Karachi Tokyo) / WEATHER_FORECAST_GENERAL --------------
@pytest.mark.parametrize("place", ["London", "Karachi", "Tokyo"])
def test_WEATHER_CHECK_TEMPERATURE_question_keys(place):
    with served("intents.weather_temperature") as c:
        t = c.post("/weather-temperature", json={"location": place}).json()["temperature_c"]
    assert t is not None and -50 < t < 55


def test_WEATHER_FORECAST_GENERAL_has_every_field():
    with served("intents.weather_forecast") as c:
        r = c.post("/weather-forecast", json={"location": "London", "window_hours": 24}).json()
    assert r["temp_max_c"] >= r["temp_min_c"] and r["location"].startswith("London") and r["condition"] and r["when"]
    assert 0 <= r["precipitation_probability_pct"] <= 100


# ---- FX (question keys CAD EUR SEK; EUR GBP JPY) -----------------------------------------------------------
@pytest.mark.parametrize("intent,pkg,path,code", [("FX_NOW_CAD", "intents.fx_cad", "/fx-now-cad", "CAD"),
                                                   ("FX_NOW_EUR", "intents.fx_eur", "/fx-now-eur", "EUR"),
                                                   ("FX_NOW_SEK", "intents.fx_sek", "/fx-now-sek", "SEK")])
def test_FX_NOW_question_keys_agree_with_the_ecb(intent, pkg, path, code):
    ref = httpx.get("https://api.frankfurter.dev/v1/latest", params={"base": "USD", "symbols": code}, timeout=15).json()["rates"][code]
    with served(pkg) as c:
        got = c.post(path, json={"currency": code}).json()["rate"]
    assert got is not None and abs(got - ref) / ref < 0.005


def test_CURRENCY_EXCHANGE_CONVERT_question_keys():
    with served("intents.currency_convert") as c:
        for code in ("EUR", "GBP", "JPY"):
            r = c.post("/currency-convert", json={"from_currency": "USD", "to_currency": code, "amount": 100}).json()
            unit = c.post("/currency-convert", json={"from_currency": "USD", "to_currency": code}).json()
            assert r["converted_amount"] == pytest.approx(100 * unit["converted_amount"], rel=1e-4)
            back = c.post("/currency-convert", json={"from_currency": code, "to_currency": "USD", "amount": r["converted_amount"]}).json()
            assert back["converted_amount"] == pytest.approx(100, rel=1e-3)


# ---- GAS_PRICE_GENERAL --------------------------------------------------------------------------------------
@pytest.mark.parametrize("chain", ["ethereum", "base", "arbitrum", "polygon"])
def test_GAS_PRICE_GENERAL_supported_chains(chain):
    with served("intents.gas_price") as c:
        r = c.post("/gas-price", json={"chain": chain}).json()
    assert r["chain"] == chain and r["gas_price_gwei"] is not None and r["gas_price_gwei"] >= 0 and r["as_of"]
    assert r["transfer_fee_usd"] is not None and r["transfer_fee_usd"] >= 0


# ---- DNS_RECORD_LOOKUP_GENERAL ------------------------------------------------------------------------------
@pytest.mark.parametrize("name,rtype,expected", [("google.com", "MX", True), ("google.com", "A", True),
                                                 ("example.com", "TXT", True), ("google.com", "SOA", True),
                                                 ("nonexistent-xyz-123456.com", "A", False)])
def test_DNS_RECORD_LOOKUP_GENERAL(name, rtype, expected):
    with served("intents.dns_lookup") as c:
        assert c.post("/dns-record", json={"name": name, "record_type": rtype}).json() == {"has_record": expected}


# ---- REGULATORY_FILING_MONITOR_GENERAL ----------------------------------------------------------------------
def test_REGULATORY_FILING_MONITOR_GENERAL_registrant_and_ticker():
    with served("intents.sec_filings") as c:
        a = c.post("/regulatory-filings", json={"entity": "Apple Inc", "period": "last 12 months"}).json()
        b = c.post("/regulatory-filings", json={"entity": "AAPL", "period": "last 12 months"}).json()
    assert a["filing_count"] and a["filing_count"] > 0 and a["urls"][0].startswith("https://www.sec.gov/")
    assert a["filing_count"] == b["filing_count"] and len(a["filings"]) == len(a["urls"])


# ---- CORPORATE_REGISTRY_LOOKUP_GENERAL ----------------------------------------------------------------------
def test_CORPORATE_REGISTRY_LOOKUP_GENERAL_known_and_unknown_companies():
    with served("intents.corporate_registry") as c:
        apple = c.post("/corporate-registry", json={"company": "Apple Inc", "jurisdiction": "United States"}).json()
        siemens = c.post("/corporate-registry", json={"company": "Siemens AG", "jurisdiction": "Germany"}).json()
        fake = c.post("/corporate-registry", json={"company": "Totally Fake Zzyzx Holdings", "jurisdiction": "France"}).json()
    assert apple["registration_id"] == "HWUPKR0MPOU8FGXBT394" and apple["active"] is True
    assert siemens["legal_name"] == "Siemens Aktiengesellschaft" and siemens["active"] is True
    assert fake["registration_id"] == "" and fake["active"] is False


# ---- TVL_LOOKUP_GENERAL -------------------------------------------------------------------------------------
def test_TVL_LOOKUP_GENERAL_protocol_chain_and_unknown():
    with served("intents.tvl") as c:
        aave = c.post("/tvl-lookup", json={"protocol": "aave"}).json()
        eth = c.post("/tvl-lookup", json={"protocol": "Ethereum"}).json()
        none = c.post("/tvl-lookup", json={"protocol": "nonexistent-protocol-zzz"}).json()
    assert aave["tvl_usd"] > 1e8 and eth["tvl_usd"] > 1e9 and none["tvl_usd"] is None


# ---- Groq-assisted intents, keyless path (always) and with a real Groq key (only if GROQ_API_KEY is exported) ------
def keyless(package: str):
    """The real app with the model switched off, so the public-sources path is what is tested."""
    app_mod = importlib.import_module(f"{package}.app")
    settings = importlib.import_module(f"{package}.config").Settings.from_env()
    return TestClient(app_mod.create_app(type(settings)(**{**settings.__dict__, "groq_api_key": ""})))


needs_groq = pytest.mark.skipif(not os.environ.get("GROQ_API_KEY"), reason="export GROQ_API_KEY to run against real Groq")
CODE_SAMPLE = "import os\ndef run(c):\n    os.system('ls ' + c)\n    return eval(c)\n"
SQL_SAMPLE = "def q(db, u):\n    return db.execute('SELECT * FROM t WHERE n=' + u)\n"


def test_WEB_SEARCH_GENERAL_keyless_answers_from_real_sources():
    with keyless("intents.web_search") as c:
        j = c.post("/web-search", json={"query": "population of Karachi"}).json()
        news = c.post("/web-search", json={"query": "latest news on the Federal Reserve interest rate decision"}).json()
        none = c.post("/web-search", json={"query": "zzzzqqqq nonsense xyzzy"}).json()
    assert "Karachi" in j["answer"] and j["sources"] and all(u.startswith("http") for u in j["sources"]) and 0 < len(j["brief"]) <= 80
    assert news["sources"] and "headlines" in news["answer"].lower() or news["sources"]
    assert none["sources"] == [] and "No sources were found" in none["answer"]


def test_CHAT_COMPLETION_GENERAL_keyless_exact_helpers_references_and_predictions():
    with keyless("intents.chat_completion") as c:
        assert c.post("/chat-completion", json={"message": "What is 12 * (3 + 4)?"}).json()["reply"] == "12 * (3 + 4) = 84"
        ref = c.post("/chat-completion", json={"message": "Explain what a hash table is"}).json()
        pred = c.post("/chat-completion", json={"message": "Will Bitcoin reach 200k next year?"}).json()
    assert "hash table" in ref["reply"].lower() and len(ref["brief"]) <= 80
    assert pred["verdict"] == "Uncertain" and pred["reply"].startswith("Uncertain")


def test_RESEARCH_QUERY_GENERAL_keyless_cites_real_scholarly_sources():
    with keyless("intents.research_query") as c:
        j = c.post("/research-query", json={"question": "What does the evidence say about the health effects of intermittent fasting?"}).json()
    assert j["source_count"] >= 1 and len(j["sources"]) == j["source_count"] and j["answer"].startswith("Based on the retrieved literature")
    assert all(u.startswith("https://") for u in j["sources"])


def test_SECURITY_REVIEW_GENERAL_finds_known_vulnerabilities_code_weaknesses_and_site_issues():
    with keyless("intents.security_review") as c:
        deps = c.post("/security-review", json={"target": "lodash@4.17.15"}).json()
        code = c.post("/security-review", json={"target": CODE_SAMPLE}).json()
        site = c.post("/security-review", json={"target": "https://expired.badssl.com/"}).json()
        addr = c.post("/security-review", json={"target": "0x1234567890123456789012345678901234567890"}).json()
        private = c.post("/security-review", json={"target": "http://169.254.169.254/latest/meta-data/"}).json()
    assert deps["verdict"] == "Insecure" and deps["vulnerability_count"] >= 3 and any(i.startswith(("CVE-", "GHSA-")) for i in deps["issues"])
    assert code["verdict"] == "Insecure" and {i.split(":")[0] for i in code["issues"]} >= {"CWE-78", "CWE-95"}
    assert site["verdict"] == "Insecure" and any(i.startswith("CWE-295") for i in site["issues"])
    assert addr["verdict"] is None and private["verdict"] is None


def test_EVENT_OUTCOME_RESOLUTION_GENERAL_never_resolves_the_future_or_the_unverifiable():
    with keyless("intents.event_outcome") as c:
        future = c.post("/event-outcome", json={"event": "Will the Fed cut rates in March 2099?"}).json()
        unknown = c.post("/event-outcome", json={"event": "Did my neighbour's cat win a prize on 2024-05-05?"}).json()
    assert future["outcome"] == "unresolved" and future["resolved_yes"] is False
    assert unknown["outcome"] == "unresolved" and unknown["sources"] == []


@needs_groq
def test_the_groq_assisted_intents_with_a_real_key():
    """Needs GROQ_API_KEY in the environment. Checks the real API shape end to end; model ids come from GROQ_MODELS."""
    with served("intents.chat_completion") as c:
        j = c.post("/chat-completion", json={"message": "Why is the sky blue?"}).json()
    assert len(j["reply"]) > 40 and "reference source" not in j["reply"] and len(j["brief"]) <= 80
    with served("intents.web_search") as c:
        w = c.post("/web-search", json={"query": "What is the capital of Australia?"}).json()
    assert "Canberra" in w["answer"] and w["sources"]
    with served("intents.research_query") as c:
        r = c.post("/research-query", json={"question": "Does regular exercise reduce symptoms of depression?"}).json()
    assert r["source_count"] >= 1 and len(r["answer"]) > 80
    with served("intents.security_review") as c:
        s = c.post("/security-review", json={"target": SQL_SAMPLE}).json()
    assert s["verdict"] in ("Needs changes", "Insecure") and s["vulnerability_count"] >= 1


# ---- TOKEN_HOLDER_COUNT_GENERAL / SLA_COMPLIANCE_GENERAL / RETURN_POLICY_VERIFY_GENERAL ---------------------------------
def test_TOKEN_HOLDER_COUNT_GENERAL_addresses_symbols_and_unsupported_cases():
    with served("intents.token_holders") as c:
        usdt = c.post("/token-holders", json={"token": "0xdAC17F958D2ee523a2206206994597C13D831ec7", "chain": "ethereum"}).json()
        op = c.post("/token-holders", json={"token": "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85", "chain": "optimism"}).json()
        avax = c.post("/token-holders", json={"token": "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E", "chain": "avalanche"}).json()
        none = c.post("/token-holders", json={"token": "0x" + "1" * 40, "chain": "dogechain"}).json()
    assert usdt["token_symbol"] == "USDT" and usdt["holders_count"] > 5_000_000
    assert op["token_symbol"] == "USDC" and op["holders_count"] > 100_000 and avax["holders_count"] > 100_000
    assert none["holders_count"] is None


def test_SLA_COMPLIANCE_GENERAL_returns_the_published_upptime_figure_and_nothing_computed():
    summary = httpx.get("https://raw.githubusercontent.com/upptime/upptime/HEAD/history/summary.json", timeout=20).json()
    google = next(s for s in summary if s["name"] == "Google")
    with served("intents.sla_compliance") as c:
        month = c.post("/sla-compliance", json={"service": "Google", "period": "last 30 days"}).json()
        target = c.post("/sla-compliance", json={"service": "Google 99.9%", "period": "last year"}).json()
        odd = c.post("/sla-compliance", json={"service": "Google", "period": "last 14 days"}).json()
        unknown = c.post("/sla-compliance", json={"service": "Some Unmonitored Service", "period": "last month"}).json()
    assert month["uptime_pct"] == float(google["uptimeMonth"].rstrip("%")) and target["sla_target_pct"] == 99.9
    assert target["compliant"] is (target["uptime_pct"] >= 99.9)
    assert odd["uptime_pct"] is None and unknown["uptime_pct"] is None


def test_RETURN_POLICY_VERIFY_GENERAL_reads_live_policy_pages_and_never_guesses():
    with served("intents.return_policy") as c:
        adafruit = c.post("/return-policy", json={"retailer": "Adafruit"}).json()
        system76 = c.post("/return-policy", json={"retailer": "System76"}).json()
        nothing = c.post("/return-policy", json={"retailer": "nonexistentshopzzzz"}).json()
        private = c.post("/return-policy", json={"retailer": "http://169.254.169.254/"}).json()
    assert adafruit["return_window_days"] == 30 and adafruit["verified"] is True and adafruit["sources"]
    assert system76["return_window_days"] == 30 and system76["verified"] is True
    assert nothing["return_window_days"] is None and nothing["verified"] is False and nothing["sources"] == []
    assert private["return_window_days"] is None and private["verified"] is False
