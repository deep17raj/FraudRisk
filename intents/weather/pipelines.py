"""Current temperature and a forecast window for a place, from Open-Meteo (met.no as a fallback for 'now')."""
from __future__ import annotations

import math
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

from pydantic import BaseModel, Field

from intentkit.ask import hours_of, is_sentence
from intentkit.cache import TTLCache
from intentkit.pipeline import BasePipeline, Req, Result
from intentkit.service import StrictModel

from .config import Settings
from .geo import Place, clean_query, resolve

FORECAST = "https://api.open-meteo.com/v1/forecast"
MET_NO = "https://api.met.no/weatherapi/locationforecast/2.0/compact"

#: WMO weather interpretation codes -> plain words, ordered by how bad the weather is.
WMO = {0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
       51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "heavy freezing drizzle",
       61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "heavy freezing rain",
       71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light rain showers",
       81: "rain showers", 82: "violent rain showers", 85: "snow showers", 86: "heavy snow showers",
       95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with heavy hail"}
TEMP = dict(ge=-90, le=60, allow_inf_nan=False)
THUNDER = frozenset({95, 96, 99})

#: MET Norway symbol names (without the _day/_night suffix) as the WMO codes the rest of this module speaks.
_MET_SYMBOLS = {
    "clearsky": 0, "fair": 1, "partlycloudy": 2, "cloudy": 3, "fog": 45,
    "lightrain": 61, "rain": 63, "heavyrain": 65, "lightrainshowers": 80, "rainshowers": 81, "heavyrainshowers": 82,
    "lightsleet": 66, "sleet": 67, "heavysleet": 67, "lightsleetshowers": 66, "sleetshowers": 67, "heavysleetshowers": 67,
    "lightsnow": 71, "snow": 73, "heavysnow": 75, "lightsnowshowers": 85, "snowshowers": 85, "heavysnowshowers": 86,
}


def met_symbol_code(symbol) -> Optional[int]:
    """A MET Norway `symbol_code` ("lightrainandthunder", "partlycloudy_night") as a WMO code, or None."""
    if not isinstance(symbol, str) or not symbol:
        return None
    base = symbol.split("_", 1)[0]
    if "thunder" in base:
        return 95
    return _MET_SYMBOLS.get(base)


def window_condition(codes: list[int]) -> str:
    """The condition that describes most of the window, in plain words.

    A week of clear weather with one foggy hour is "clear sky", not "fog": the single worst hour says little about
    the window, and the peak chance of rain already has its own field. A tie goes to the more severe code (codes
    rise with severity within each family). The one exception is a thunderstorm, which is worth knowing about even
    when it is brief, so it is added as a risk to a different dominant condition.
    """
    if not codes:
        return ""
    counts = Counter(codes)
    top = max(counts.values())
    dominant = max(c for c, n in counts.items() if n == top)
    label = WMO[dominant]
    if dominant not in THUNDER and any(c in THUNDER for c in codes):
        label += ", with a risk of thunderstorm"
    return label


class TemperatureResponse(StrictModel):
    """Exactly the response field of Telegraph's WEATHER_CHECK_TEMPERATURE intent. Nothing else."""

    temperature_c: Optional[float] = Field(default=None, **TEMP)


class ForecastResponse(StrictModel):
    """Exactly the response fields of Telegraph's WEATHER_FORECAST_GENERAL intent. Nothing else."""

    location: str = ""
    when: str = ""
    temp_max_c: Optional[float] = Field(default=None, **TEMP)
    temp_min_c: Optional[float] = Field(default=None, **TEMP)
    precipitation_probability_pct: Optional[float] = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    condition: str = ""


def _ok(v) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


class _WeatherBase(BasePipeline):
    def __init__(self, settings: Settings, client) -> None:
        super().__init__(settings, client)
        self.geo_cache = TTLCache(max_items=settings.cache_max_items)

    async def place(self, raw: str) -> Optional[Place]:
        return await resolve(self.up, self.geo_cache, self.settings.geocode_ttl, raw)


class TemperaturePipeline(_WeatherBase):
    intent = "WEATHER_CHECK_TEMPERATURE"

    def key(self, req: Req) -> Optional[str]:
        loc = clean_query(req.get("location", ""))
        return f"t:{loc.lower()}" if loc else None

    def empty(self, req: Req, reason: str) -> BaseModel:
        return TemperatureResponse(temperature_c=None)

    async def compute(self, req: Req) -> Result:
        place = await self.place(req.get("location", ""))
        if place is None:
            return Result(self.empty(req, "place not found"), degraded=True)
        temp = await self._open_meteo(place)
        if temp is None:
            temp = await self._met_no(place)
        if temp is None:
            return Result(self.empty(req, "no weather source answered"), degraded=True)
        return Result(TemperatureResponse(temperature_c=round(temp, 1)))

    async def _open_meteo(self, p: Place) -> Optional[float]:
        d = await self.up.get_json(FORECAST, params={"latitude": p.lat, "longitude": p.lon,
                                                     "current": "temperature_2m", "timezone": "UTC"})
        return _ok(((d or {}).get("current") or {}).get("temperature_2m")) if isinstance(d, dict) else None

    async def _met_no(self, p: Place) -> Optional[float]:
        d = await self.up.get_json(MET_NO, params={"lat": round(p.lat, 4), "lon": round(p.lon, 4)})
        try:
            return _ok(d["properties"]["timeseries"][0]["data"]["instant"]["details"]["air_temperature"])
        except (KeyError, IndexError, TypeError):
            return None


class ForecastPipeline(_WeatherBase):
    intent = "WEATHER_FORECAST_GENERAL"

    @staticmethod
    def _hours(req: Req) -> int:
        raw = req.get("window_hours", "")
        if not raw and is_sentence(req.get("location", "")):      # "...over the next 48 hours?"
            return hours_of(req.get("location", "")) or 24
        try:
            return min(240, max(1, int(float(raw or 24))))
        except ValueError:
            return 24

    def key(self, req: Req) -> Optional[str]:
        loc = clean_query(req.get("location", ""))
        return f"f:{loc.lower()}:{self._hours(req)}" if loc else None

    def empty(self, req: Req, reason: str) -> BaseModel:
        return ForecastResponse(location=clean_query(req.get("location", ""))[:200])

    async def _open_meteo_window(self, place: Place, hours: int) -> Optional[tuple[list[float], list[float], list[int]]]:
        d = await self.up.get_json(FORECAST, params={
            "latitude": place.lat, "longitude": place.lon, "timezone": "UTC", "forecast_hours": hours,
            "hourly": "temperature_2m,precipitation_probability,weather_code"})
        h = d.get("hourly") if isinstance(d, dict) else None
        if not isinstance(h, dict):
            return None
        temps = [t for t in map(_ok, h.get("temperature_2m") or []) if t is not None]
        probs = [p for p in map(_ok, h.get("precipitation_probability") or []) if p is not None]
        codes = [int(c) for c in (h.get("weather_code") or []) if isinstance(c, (int, float)) and int(c) in WMO]
        return (temps, probs, codes) if temps else None

    async def _met_no_window(self, place: Place, hours: int) -> Optional[tuple[list[float], list[float], list[int]]]:
        """The same window from MET Norway when Open-Meteo cannot be reached (it rate-limits shared hosting IPs).

        MET's compact forecast has no precipitation probability, so that stays empty rather than being estimated.
        """
        d = await self.up.get_json(MET_NO, params={"lat": round(place.lat, 4), "lon": round(place.lon, 4)})
        series = ((d or {}).get("properties") or {}).get("timeseries") if isinstance(d, dict) else None
        if not isinstance(series, list):
            return None
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        end = now + timedelta(hours=hours)
        temps: list[float] = []
        codes: list[int] = []
        for point in series:
            try:
                at = datetime.fromisoformat(str(point["time"]).replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
            if not (now <= at < end):
                continue
            data = point.get("data") or {}
            t = _ok(((data.get("instant") or {}).get("details") or {}).get("air_temperature"))
            if t is None:
                continue
            temps.append(t)
            for span in ("next_1_hours", "next_6_hours", "next_12_hours"):
                code = met_symbol_code(((data.get(span) or {}).get("summary") or {}).get("symbol_code"))
                if code is not None:
                    codes.append(code)
                    break
        return (temps, [], codes) if temps else None

    async def compute(self, req: Req) -> Result:
        place = await self.place(req.get("location", ""))
        if place is None:
            return Result(self.empty(req, "place not found"), degraded=True)
        hours = self._hours(req)
        window = await self._open_meteo_window(place, hours) or await self._met_no_window(place, hours)
        if window is None:
            return Result(self.empty(req, "no forecast source answered"), degraded=True)
        temps, probs, codes = window
        start = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:00 UTC")
        return Result(ForecastResponse(
            location=place.label, when=f"next {hours} hours from {start}",
            temp_max_c=round(max(temps), 1), temp_min_c=round(min(temps), 1),
            precipitation_probability_pct=round(max(probs), 1) if probs else None,
            condition=window_condition(codes)))
