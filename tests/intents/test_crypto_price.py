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


# ---- a coin named in words instead of by ticker ------------------------------------------------------------
ASSETS = "https://api.coinbase.com/v2/currencies/crypto"
GECKO = "https://api.coingecko.com/api/v3/search"
ASSET_LIST = {"data": [{"code": "GRT", "name": "The Graph"}, {"code": "HYPE", "name": "Hyperliquid"},
                       {"code": "ETC", "name": "Ethereum Classic"}, {"code": "WBTC", "name": "Wrapped Bitcoin"}]}


def named(price_by_symbol, assets=ASSET_LIST, gecko=None) -> FakeHttp:
    """Coinbase answers a spot price for the symbols in `price_by_symbol` and 404s for any other."""
    def spot(req: httpx.Request):
        sym = req.url.path.split("/")[-2].split("-")[0]
        return {"data": {"amount": str(price_by_symbol[sym]), "currency": "USD"}} if sym in price_by_symbol else 404
    return FakeHttp({CB: spot, ASSETS: assets, GECKO: gecko})


async def price_of(fake, symbol):
    return (await CryptoPricePipeline(fast(Settings), fake.client()).answer({"symbol": symbol})).price_usd


@pytest.mark.parametrize("text", ["The Graph", "the graph", "What is the price of The Graph?", "price of The Graph in USD",
                                  "How much is The Graph worth right now?"])
async def test_a_coin_named_in_words_is_resolved_through_coinbases_asset_list(text):
    assert await price_of(named({"GRT": 0.0167}), text) == 0.0167


async def test_a_single_word_name_is_resolved_after_its_ticker_guess_finds_no_price():
    fake = named({"HYPE": 64.1})
    assert await price_of(fake, "Hyperliquid") == 64.1                 # "HYPERLIQUID" is no exchange's ticker


async def test_a_ticker_never_touches_the_name_lists():
    fake = named({"BTC": 60000})
    assert await price_of(fake, "BTC") == 60000
    assert fake.count(ASSETS) == 0 and fake.count(GECKO) == 0


async def test_coingecko_is_asked_only_when_coinbase_does_not_know_the_name_and_must_match_it_exactly():
    gecko = {"coins": [{"name": "Kaspa", "symbol": "kas"}, {"name": "Kaspa Classic", "symbol": "KCL"}]}
    assert await price_of(named({"KAS": 0.027}, gecko=gecko), "Kaspa") == 0.027
    loose = {"coins": [{"name": "DAO Maker", "symbol": "DAO"}, {"name": "Rainmaker", "symbol": "RAIN"}]}
    fake = named({"DAO": 1.0, "RAIN": 2.0}, gecko=loose)
    assert await price_of(fake, "Maker") is None                       # a near name must never stand in for the coin


async def test_a_qualified_name_is_not_taken_for_its_base_coin():
    fake = named({"ETH": 1670.0, "ETC": 5.9, "BTC": 60000, "WBTC": 59990})
    assert await price_of(fake, "Ethereum Classic") == 5.9             # not Ethereum's 1670
    assert await price_of(fake, "Wrapped Bitcoin") == 59990
    assert await price_of(fake, "Staked Ether") is None                # unresolved is honest; ETH's price would be wrong


@pytest.mark.parametrize("text,expected", [("Ethereum Classic", None), ("Wrapped Bitcoin", None), ("Bitcoin Gold", None),
                                           ("Staked Ether", None), ("Bitcoin Cash", "BCH"), ("Bitcoin", "BTC"),
                                           ("What is the price of Solana?", "SOL")])
def test_names_with_a_qualifier_are_left_for_name_resolution(text, expected):
    assert parse_symbol(text) == expected


async def test_when_neither_name_list_answers_a_named_coin_is_empty_not_guessed():
    assert await price_of(named({}, assets=None, gecko=None), "The Graph") is None
    assert await price_of(named({"GRT": 0.0167}, assets={"data": []}), "Nonexistent Coin") is None


@pytest.mark.parametrize("junk", ["", "   ", "??", "!!!"])
async def test_junk_is_empty_without_any_lookup(junk):
    fake = named({"BTC": 1})
    assert await price_of(fake, junk) is None and fake.count(ASSETS) == 0
