"""Current temperature and a forecast window for a place, from Open-Meteo (met.no as a fallback for 'now')."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

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
        try:
            return min(240, max(1, int(float(req.get("window_hours", "") or 24))))
        except ValueError:
            return 24

    def key(self, req: Req) -> Optional[str]:
        loc = clean_query(req.get("location", ""))
        return f"f:{loc.lower()}:{self._hours(req)}" if loc else None

    def empty(self, req: Req, reason: str) -> BaseModel:
        return ForecastResponse(location=clean_query(req.get("location", ""))[:200])

    async def compute(self, req: Req) -> Result:
        place = await self.place(req.get("location", ""))
        if place is None:
            return Result(self.empty(req, "place not found"), degraded=True)
        hours = self._hours(req)
        d = await self.up.get_json(FORECAST, params={
            "latitude": place.lat, "longitude": place.lon, "timezone": "UTC", "forecast_hours": hours,
            "hourly": "temperature_2m,precipitation_probability,weather_code"})
        h = d.get("hourly") if isinstance(d, dict) else None
        if not isinstance(h, dict):
            return Result(self.empty(req, "no forecast source answered"), degraded=True)
        temps = [t for t in map(_ok, h.get("temperature_2m") or []) if t is not None]
        probs = [p for p in map(_ok, h.get("precipitation_probability") or []) if p is not None]
        codes = [int(c) for c in (h.get("weather_code") or []) if isinstance(c, (int, float)) and int(c) in WMO]
        if not temps:
            return Result(self.empty(req, "forecast had no temperatures"), degraded=True)
        worst = max(codes) if codes else None            # codes rise with severity within each family
        start = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:00 UTC")
        return Result(ForecastResponse(
            location=place.label, when=f"next {hours} hours from {start}",
            temp_max_c=round(max(temps), 1), temp_min_c=round(min(temps), 1),
            precipitation_probability_pct=round(max(probs), 1) if probs else None,
            condition=WMO[worst] if worst is not None else ""))
