from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from intents.currency_convert.app import create_app as create_convert
from intents.fx.config import Settings
from intents.fx.pipelines import ConvertPipeline, ConvertResponse, FxRateResponse, fx_now
from intents.fx.rates import FRANKFURTER, ER_API, parse_currencies, parse_one
from intents.fx_cad.app import create_app as create_cad
from tests.intents.helpers import FakeHttp, StubPipeline, fast

ECB = {"base": "USD", "rates": {"EUR": 0.9, "CAD": 1.4, "SEK": 10.0, "GBP": 0.76, "JPY": 158.0}}
ER = {"result": "success", "rates": {"USD": 1, "EUR": 0.95, "PKR": 280.0, "CAD": 1.41, "AED": 3.6725}}


def http(ecb=ECB, er=ER) -> FakeHttp:
    return FakeHttp({FRANKFURTER: ecb, ER_API: er})


async def rate(fake, code, request_currency=None):
    pl = fx_now(code)(fast(Settings), fake.client())
    return (await pl.answer({"currency": request_currency if request_currency is not None else code})).rate


async def test_the_ecb_rate_is_preferred_where_both_sources_publish_a_currency():
    assert await rate(http(), "SEK") == 10.0
    assert await rate(http(), "GBP") == 0.76


async def test_eur_and_cad_use_open_er_api_when_it_publishes_them():
    assert await rate(http(), "CAD") == 1.41
    assert await rate(http(), "EUR") == 0.95


async def test_eur_and_cad_fall_back_to_the_ecb_when_open_er_api_is_down():
    assert await rate(http(er=None), "CAD") == 1.4
    assert await rate(http(er=None), "EUR") == 0.9


async def test_conversion_keeps_using_the_ecb_table():
    assert await convert(http(), from_currency="USD", to_currency="EUR") == pytest.approx(0.9)


async def test_the_second_source_fills_currencies_the_ecb_does_not_publish():
    assert await rate(http(), "PKR") == 280.0


async def test_either_source_alone_is_enough():
    assert await rate(http(ecb=None), "EUR") == 0.95
    assert await rate(http(er=None), "EUR") == 0.9
    assert await rate(http(er=None), "SEK") == 10.0


async def test_no_source_or_an_unknown_currency_is_null():
    assert await rate(http(ecb=None, er=None), "CAD") is None
    assert await rate(http(), "CAD", request_currency="ZZZ") is None


async def test_a_request_naming_no_currency_defaults_to_the_intents_own():
    assert await rate(http(), "SEK", request_currency="") == 10.0


async def test_the_pair_form_and_currency_names_are_understood():
    assert await rate(http(), "CAD", request_currency="USD/JPY") == 158.0
    assert await rate(http(), "CAD", request_currency="Swedish krona") == 10.0
    assert await rate(http(), "SEK", request_currency="USD/CAD") == 1.41


async def convert(fake, **req):
    pl = ConvertPipeline(fast(Settings), fake.client())
    return (await pl.answer(req)).converted_amount


async def test_conversion_goes_through_the_dollar():
    assert await convert(http(), from_currency="USD", to_currency="EUR", amount="100") == pytest.approx(90.0)
    assert await convert(http(), from_currency="EUR", to_currency="JPY") == pytest.approx(158.0 / 0.9)
    assert await convert(http(), from_currency="GBP", to_currency="PKR", amount="2500") == pytest.approx(2500 * 280 / 0.76)


async def test_amount_defaults_to_one_and_junk_amounts_do_too():
    assert await convert(http(), from_currency="USD", to_currency="EUR") == pytest.approx(0.9)
    assert await convert(http(), from_currency="USD", to_currency="EUR", amount="lots") == pytest.approx(0.9)
    assert await convert(http(), from_currency="USD", to_currency="EUR", amount="-5") == pytest.approx(0.9)


async def test_a_single_sentence_request_carries_its_own_amount():
    assert await convert(http(), query="100 USD to GBP") == pytest.approx(76.0)
    assert await convert(http(), query="1,000 euros in yen") == pytest.approx(1000 * 158.0 / 0.9)


async def test_missing_or_unknown_currencies_are_null():
    assert await convert(http(), from_currency="USD") is None
    assert await convert(http(), from_currency="USD", to_currency="ZZZ") is None
    assert await convert(http(ecb=None, er=None), from_currency="USD", to_currency="EUR") is None


def test_currency_parsing():
    assert parse_currencies("100 USD to GBP") == ["USD", "GBP"]
    assert parse_one("USD/CAD") == "CAD" and parse_one("cad") == "CAD" and parse_one("") is None
    assert parse_one("Canadian dollars") == "CAD"


@pytest.mark.parametrize("module,path,intent", [("intents.fx_cad.app", "/fx-now-cad", "FX_NOW_CAD"),
                                                 ("intents.fx_eur.app", "/fx-now-eur", "FX_NOW_EUR"),
                                                 ("intents.fx_sek.app", "/fx-now-sek", "FX_NOW_SEK")])
def test_each_fx_intent_is_served_at_its_own_path(module, path, intent):
    import importlib
    app = importlib.import_module(module).create_app(fast(Settings), pipeline=StubPipeline(FxRateResponse, rate=1.25))
    assert app.state.intent == intent
    with TestClient(app) as c:
        assert c.post(path, json={"currency": "USD"}).json() == {"rate": 1.25}
        assert c.post(path, content=b"garbage").json() == {"rate": 1.25}


def test_http_contract_for_both_shapes():
    with TestClient(create_cad(fast(Settings), pipeline=StubPipeline(FxRateResponse, rate=1.4307))) as c:
        r = c.post("/fx-now-cad", json={"currency": "CAD"})
        assert r.status_code == 200 and r.json() == {"rate": 1.4307}
    stub = StubPipeline(ConvertResponse, converted_amount=90.0)
    with TestClient(create_convert(fast(Settings), pipeline=stub)) as c:
        assert c.post("/currency-convert", json={"from_currency": "USD", "to_currency": "EUR", "amount": 100}).json() == {"converted_amount": 90.0}
    assert stub.seen[0]["amount"] == "100"
    assert FxRateResponse().rate is None and ConvertResponse().converted_amount is None
