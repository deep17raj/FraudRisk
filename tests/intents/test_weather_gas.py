from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from intents.gas_price.app import create_app as create_gas
from intents.gas_price.config import Settings as GasSettings
from intents.gas_price.pipeline import CHAINS, GasPricePipeline, GasPriceResponse, parse_chain
from intents.weather.config import Settings
from intents.weather.geo import clean_query
from intents.weather.pipelines import (FORECAST, MET_NO, ForecastPipeline, ForecastResponse, TemperaturePipeline,
                                       TemperatureResponse)
from intents.weather_forecast.app import create_app as create_forecast
from intents.weather_temperature.app import create_app as create_temp
from tests.intents.helpers import FakeHttp, StubPipeline, fast

GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
KARACHI = {"results": [{"name": "Karachi", "latitude": 24.86, "longitude": 67.01, "country": "Pakistan",
                        "country_code": "PK", "admin1": "Sindh"}]}
PARIS = {"results": [{"name": "Paris", "latitude": 33.6, "longitude": -95.5, "country": "United States",
                      "country_code": "US", "admin1": "Texas"},
                     {"name": "Paris", "latitude": 48.85, "longitude": 2.35, "country": "France",
                      "country_code": "FR", "admin1": "Ile-de-France"}]}


def weather_http(current=27.0, hourly=None, geo=KARACHI, met=None) -> FakeHttp:
    def forecast(req: httpx.Request):
        if "current" in req.url.params:
            return None if current is None else {"current": {"temperature_2m": current}}
        return hourly
    return FakeHttp({GEOCODE: geo, FORECAST: forecast, MET_NO: met})


async def temperature(fake, location):
    return (await TemperaturePipeline(fast(Settings), fake.client()).answer({"location": location})).temperature_c


async def test_current_temperature_for_a_named_place():
    fake = weather_http()
    assert await temperature(fake, "Karachi") == 27.0
    assert "name=Karachi" in str(fake.calls[0].url)


async def test_latitude_longitude_needs_no_geocoding():
    fake = weather_http()
    assert await temperature(fake, "24.86, 67.01") == 27.0
    assert fake.count(GEOCODE) == 0


async def test_a_country_hint_picks_the_right_homonym():
    fake = weather_http(geo=PARIS)
    pl = TemperaturePipeline(fast(Settings), fake.client())
    place = await pl.place("Paris, France")
    assert place is not None and place.lat == pytest.approx(48.85)
    assert (await pl.place("Paris")).lat == pytest.approx(33.6)       # no hint: the geocoder's first result


async def test_met_no_answers_when_open_meteo_has_no_current_temperature():
    met = {"properties": {"timeseries": [{"data": {"instant": {"details": {"air_temperature": 12.5}}}}]}}
    assert await temperature(weather_http(current=None, met=met), "Karachi") == 12.5


async def test_unknown_place_or_no_source_is_null():
    assert await temperature(weather_http(geo={"results": []}), "Qzxqzx") is None
    assert await temperature(weather_http(current=None, met=None), "Karachi") is None
    assert await temperature(weather_http(), "") is None


async def test_a_geocoder_outage_is_not_remembered_as_place_not_found():
    fake = weather_http(geo=None)
    pl = TemperaturePipeline(fast(Settings), fake.client())
    assert await pl.place("Karachi") is None
    fake.add(GEOCODE, KARACHI)
    assert await pl.place("Karachi") is not None


def test_questions_are_reduced_to_the_place():
    assert clean_query("What is the current temperature in Karachi right now?") == "Karachi"
    assert clean_query("Tokyo") == "Tokyo"


HOURLY = {"hourly": {"temperature_2m": [10.0, 12.5, 8.0], "precipitation_probability": [10, 70, 20],
                     "weather_code": [1, 63, 3]}}


async def test_forecast_summarises_the_window():
    pl = ForecastPipeline(fast(Settings), weather_http(hourly=HOURLY).client())
    r = await pl.answer({"location": "Karachi", "window_hours": "48"})
    assert (r.temp_max_c, r.temp_min_c, r.precipitation_probability_pct) == (12.5, 8.0, 70.0)
    assert r.condition == "rain" and r.location == "Karachi, Sindh, Pakistan" and r.when.startswith("next 48 hours")


async def test_a_forecast_question_in_the_location_field_gives_the_place_and_the_window():
    fake = weather_http(hourly=HOURLY)
    r = await ForecastPipeline(fast(Settings), fake.client()).answer(
        {"location": "What is the weather forecast for Karachi over the next 48 hours?"})
    assert r.location == "Karachi, Sindh, Pakistan" and r.temp_max_c == 12.5 and r.when.startswith("next 48 hours")
    assert "name=Karachi" in str(fake.calls[0].url)
    given = await ForecastPipeline(fast(Settings), weather_http(hourly=HOURLY).client()).answer(
        {"location": "Will Karachi be warm over the next 48 hours?", "window_hours": "12"})
    assert given.when.startswith("next 12 hours")


async def test_a_temperature_question_in_the_location_field_finds_the_place():
    assert await temperature(weather_http(), "Will Karachi exceed 40 degrees today?") == 27.0


def hourly(codes):
    return {"hourly": {"temperature_2m": [10.0] * len(codes), "precipitation_probability": [0] * len(codes),
                       "weather_code": codes}}


async def condition_for(codes):
    pl = ForecastPipeline(fast(Settings), weather_http(hourly=hourly(codes)).client())
    return (await pl.answer({"location": "Karachi"})).condition


async def test_the_condition_is_what_most_of_the_window_looks_like_not_its_worst_hour():
    assert await condition_for([0] * 20 + [45] * 4) == "clear sky"           # one foggy morning in a clear day
    assert await condition_for([3] * 15 + [51] * 5 + [0] * 4) == "overcast"
    assert await condition_for([71] * 10 + [3] * 14) == "overcast"


async def test_a_tie_between_conditions_goes_to_the_more_severe():
    assert await condition_for([0, 0, 61, 61]) == "light rain"
    assert await condition_for([3, 3, 3, 63, 63, 63]) == "rain"


async def test_a_thunderstorm_is_never_hidden_by_a_calmer_dominant_condition():
    assert await condition_for([3] * 20 + [95] * 2) == "overcast, with a risk of thunderstorm"
    assert await condition_for([95] * 10 + [3] * 2) == "thunderstorm"            # already the dominant condition
    assert await condition_for([99] * 3 + [96] * 3 + [3]) == "thunderstorm with heavy hail"


async def test_codes_outside_the_table_are_ignored_and_an_empty_window_has_no_condition():
    assert await condition_for([0, 0, 0, 1234]) == "clear sky"
    r = await ForecastPipeline(fast(Settings), weather_http(hourly=hourly([1234])).client()).answer({"location": "Karachi"})
    assert r.condition == "" and r.temp_max_c == 10.0


def met_series(hours=60, base=10.0, symbol="cloudy_day", thunder_at=None):
    from datetime import datetime, timedelta, timezone
    start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    points = []
    for i in range(hours):
        sym = "lightrainandthunder" if thunder_at == i else symbol
        points.append({"time": (start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "data": {"instant": {"details": {"air_temperature": base + (i % 5)}},
                                "next_1_hours": {"summary": {"symbol_code": sym}, "details": {"precipitation_amount": 0.0}}}})
    return {"properties": {"timeseries": points}}


async def test_met_norway_answers_the_forecast_when_open_meteo_cannot():
    fake = weather_http(hourly=None, met=met_series())
    r = await ForecastPipeline(fast(Settings), fake.client()).answer({"location": "Karachi", "window_hours": "24"})
    assert (r.temp_max_c, r.temp_min_c) == (14.0, 10.0) and r.condition == "overcast"
    assert r.location == "Karachi, Sindh, Pakistan" and r.when.startswith("next 24 hours")
    assert r.precipitation_probability_pct is None            # MET's compact forecast has none, and none is invented


async def test_met_forecast_covers_only_the_asked_window_and_flags_a_thunderstorm():
    short = await ForecastPipeline(fast(Settings), weather_http(hourly=None, met=met_series(60, base=0.0)).client()).answer(
        {"location": "Karachi", "window_hours": "3"})
    assert (short.temp_max_c, short.temp_min_c) == (2.0, 0.0)           # three hours: 0, 1, 2
    stormy = await ForecastPipeline(fast(Settings), weather_http(hourly=None, met=met_series(thunder_at=2)).client()).answer(
        {"location": "Karachi", "window_hours": "24"})
    assert stormy.condition == "overcast, with a risk of thunderstorm"


async def test_open_meteo_is_still_preferred_when_it_answers():
    fake = weather_http(hourly=HOURLY, met=met_series())
    r = await ForecastPipeline(fast(Settings), fake.client()).answer({"location": "Karachi", "window_hours": "48"})
    assert r.temp_max_c == 12.5 and fake.count(MET_NO) == 0


async def test_both_sources_down_is_an_honest_empty_forecast():
    r = await ForecastPipeline(fast(Settings), weather_http(hourly=None, met=None).client()).answer({"location": "Karachi"})
    assert r.temp_max_c is None and r.condition == "" and r.location == "Karachi"


def test_met_symbols_map_to_conditions():
    from intents.weather.pipelines import met_symbol_code
    assert [met_symbol_code(x) for x in ("clearsky_day", "partlycloudy_night", "heavyrain", "lightsnowshowers_day")] == [0, 2, 65, 85]
    assert met_symbol_code("heavyrainandthunder") == 95 and met_symbol_code("lightssleetshowersandthunder_day") == 95
    assert met_symbol_code("nonsense") is None and met_symbol_code(None) is None and met_symbol_code("") is None


async def test_forecast_window_is_clamped_and_defaults_to_a_day():
    fake = weather_http(hourly=HOURLY)
    pl = ForecastPipeline(fast(Settings), fake.client())
    for raw in ("", "junk", "0", "99999"):          # "" and "junk" both mean 24, so the second is cached
        await pl.answer({"location": "Karachi", "window_hours": raw})
    hours = [r.url.params["forecast_hours"] for r in fake.calls if "forecast_hours" in r.url.params]
    assert hours == ["24", "1", "240"]


async def test_forecast_without_data_is_honestly_empty():
    r = await ForecastPipeline(fast(Settings), weather_http(hourly=None).client()).answer({"location": "Karachi"})
    assert r.temp_max_c is None and r.condition == "" and r.location == "Karachi"
    r2 = await ForecastPipeline(fast(Settings), weather_http(hourly={"hourly": {"temperature_2m": []}}).client()).answer({"location": "Karachi"})
    assert r2.temp_max_c is None


def test_weather_http_contracts():
    with TestClient(create_temp(fast(Settings), pipeline=StubPipeline(TemperatureResponse, temperature_c=8.9))) as c:
        assert c.post("/weather-temperature", json={"location": "London"}).json() == {"temperature_c": 8.9}
    stub = StubPipeline(ForecastResponse, location="London", temp_max_c=9.0)
    with TestClient(create_forecast(fast(Settings), pipeline=stub)) as c:
        r = c.post("/weather-forecast", json={"location": "London", "window_hours": 12})
    assert set(r.json()) == {"location", "when", "temp_max_c", "temp_min_c", "precipitation_probability_pct", "condition"}
    assert stub.seen[0]["window_hours"] == "12"


# ---- gas price ------------------------------------------------------------------------------------------------
def gas_http(wei="0x3b9aca00", eth_usd="2000", rpc_down=()) -> FakeHttp:
    routes = {"https://api.coinbase.com/v2/prices/": {"data": {"amount": eth_usd}} if eth_usd else None}
    for urls, _ in CHAINS.values():
        for u in urls:
            routes[u] = None if u in rpc_down else {"jsonrpc": "2.0", "id": 1, "result": wei}
    return FakeHttp(routes)


async def gas(fake, chain):
    return await GasPricePipeline(fast(GasSettings), fake.client()).answer({"chain": chain})


async def test_gas_price_and_the_cost_of_a_transfer():
    r = await gas(gas_http(), "ethereum")        # 0x3b9aca00 = 1 gwei
    assert r.gas_price_gwei == 1.0 and r.chain == "ethereum"
    assert r.transfer_fee_usd == pytest.approx(1e-9 * 21000 * 2000)
    assert r.as_of.endswith("Z")


async def test_the_second_rpc_node_answers_when_the_first_is_down():
    first = CHAINS["base"][0][0]
    r = await gas(gas_http(rpc_down=(first,)), "base")
    assert r.gas_price_gwei == 1.0


async def test_without_a_dollar_price_the_gas_price_is_still_returned():
    r = await gas(gas_http(eth_usd=None), "ethereum")
    assert r.gas_price_gwei == 1.0 and r.transfer_fee_usd is None


async def test_unknown_chain_or_all_nodes_down_is_null_but_echoes_the_chain():
    r = await gas(gas_http(), "dogechain")
    assert r.chain == "dogechain" and r.gas_price_gwei is None and r.as_of == ""
    down = tuple(u for urls, _ in CHAINS.values() for u in urls)
    r2 = await gas(gas_http(rpc_down=down), "ethereum")
    assert r2.gas_price_gwei is None and r2.chain == "ethereum"


@pytest.mark.parametrize("raw,chain", [("ETH", "ethereum"), ("Ethereum Mainnet", "ethereum"), ("bnb chain", "bsc"),
                                       ("Arbitrum One", "arbitrum"), ("what is the gas on polygon?", "polygon"),
                                       ("OP", "optimism"), ("nope", None), ("", None)])
def test_chain_names(raw, chain):
    assert parse_chain(raw) == chain


def test_gas_http_contract():
    with TestClient(create_gas(fast(GasSettings), pipeline=StubPipeline(GasPriceResponse, chain="ethereum", gas_price_gwei=1.0))) as c:
        r = c.post("/gas-price", json={"chain": "ethereum"})
    assert set(r.json()) == {"chain", "gas_price_gwei", "transfer_fee_usd", "as_of"}
