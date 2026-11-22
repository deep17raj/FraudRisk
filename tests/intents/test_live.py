"""Real sources, real network, no stubs. Deselected by default; run with `python -m pytest -m live tests/intents`.

Each intent is asked the questions Telegraph itself asks (the `question_keys` in docs/intent_specs/) plus a
few negatives. Assertions are on facts that are stable (a CVE's score, whether google.com has a valid
certificate), or on cross-checks against an independent source, never on a value that moves.
"""
from __future__ import annotations

import importlib

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
