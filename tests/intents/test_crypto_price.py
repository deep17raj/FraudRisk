from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from intents.crypto_price.app import create_app
from intents.crypto_price.config import Settings
from intents.crypto_price.pipeline import CryptoPricePipeline, CryptoPriceResponse
from intents.crypto_price.symbols import parse_symbol
from tests.intents.helpers import FakeHttp, StubPipeline, fast

CB = "https://api.coinbase.com/v2/prices/"
KRAKEN = "https://api.kraken.com/0/public/Ticker"
BINANCE = "https://data-api.binance.vision/api/v3/ticker/price"
OKX = "https://www.okx.com/api/v5/market/ticker"
BITSTAMP = "https://www.bitstamp.net/api/v2/ticker/"


def exchanges(coinbase=None, kraken=None, binance=None, okx=None, bitstamp=None) -> FakeHttp:
    def px(rule, fn):
        return None if rule is None else (rule if isinstance(rule, (int, Exception)) else fn(rule))
    return FakeHttp({
        CB: px(coinbase, lambda v: {"data": {"amount": str(v), "currency": "USD"}}),
        KRAKEN: px(kraken, lambda v: {"error": [], "result": {"XXBTZUSD": {"c": [str(v), "1.0"]}}}),
        BINANCE: px(binance, lambda v: {"symbol": "BTCUSDT", "price": str(v)}),
        OKX: px(okx, lambda v: {"code": "0", "data": [{"last": str(v)}]}),
        BITSTAMP: px(bitstamp, lambda v: {"last": str(v)}),
    })


async def price(fake: FakeHttp, symbol="BTC", **kw):
    pl = CryptoPricePipeline(fast(Settings, **kw), fake.client())
    return (await pl.answer({"symbol": symbol})).price_usd


async def test_the_answer_is_the_median_of_the_exchanges():
    assert await price(exchanges(100.0, 100.4, 99.8, 100.2, 100.1)) == pytest.approx(100.1)


async def test_one_wild_exchange_cannot_decide_the_scored_number():
    # 180 is dropped as an outlier; the median of the other four is (100.0 + 100.1) / 2
    assert await price(exchanges(100.0, 100.2, 99.9, 100.1, 180.0)) == pytest.approx(100.05)


async def test_exchanges_that_are_down_or_unlisted_are_skipped():
    assert await price(exchanges(coinbase=100.0, kraken=503, binance=httpx.ConnectError("x"), okx=404, bitstamp=None)) == 100.0


async def test_no_exchange_answering_is_null_not_zero():
    assert await price(exchanges(None, None, None, None, None)) is None


async def test_out_of_range_prices_are_ignored():
    assert await price(exchanges(coinbase=0.0, kraken=-5, binance=1e9, okx=100.0, bitstamp="nan")) == 100.0


async def test_each_exchange_is_asked_for_the_right_pair():
    fake = exchanges(100.0, 100.0, 100.0, 100.0, 100.0)
    await price(fake, "DOGE")
    urls = [str(r.url) for r in fake.calls]
    assert any("DOGE-USD" in u for u in urls) and any("pair=XDGUSD" in u for u in urls)
    assert any("symbol=DOGEUSDT" in u for u in urls) and any("DOGE-USDT" in u for u in urls)
    assert any("dogeusd" in u for u in urls)


async def test_repeat_requests_are_served_from_the_cache():
    fake = exchanges(100.0, 100.0, 100.0, 100.0, 100.0)
    pl = CryptoPricePipeline(fast(Settings), fake.client())
    await pl.answer({"symbol": "BTC"})
    n = len(fake.calls)
    await pl.answer({"symbol": "btc"})
    assert len(fake.calls) == n


@pytest.mark.parametrize("raw,sym", [
    ("BTC", "BTC"), ("btc", "BTC"), ("$eth", "ETH"), ("ETH-USD", "ETH"), ("sol/usdt", "SOL"), ("Bitcoin", "BTC"),
    ("What is the current price of Ethereum in US dollars?", "ETH"), ("price of XRP now", "XRP"),
    ("  SOL  ", "SOL"), ("", None), ("???", None),
])
def test_symbols_are_understood(raw, sym):
    assert parse_symbol(raw) == sym


def test_http_contract():
    stub = StubPipeline(CryptoPriceResponse, price_usd=61234.5)
    with TestClient(create_app(fast(Settings), pipeline=stub)) as c:
        r = c.post("/crypto-price", json={"symbol": "BTC"})
        assert r.status_code == 200 and r.json() == {"price_usd": 61234.5}
        assert c.post("/crypto-price", json={"coin": "ETH"}).status_code == 200
    assert [s["symbol"] for s in stub.seen] == ["BTC", "ETH"]
    assert CryptoPriceResponse().model_dump() == {"price_usd": None}
