from __future__ import annotations

from typing import Optional

from fastapi import FastAPI

from intentkit.request import GENERIC_ALIASES
from intentkit.service import create_service_app
from intents.weather.config import Settings
from intents.weather.pipelines import ForecastPipeline, ForecastResponse

VERSION = "0.1.0"
FIELDS = {
    "location": ("city", "place", "name", "where", *GENERIC_ALIASES),
    "window_hours": ("hours", "window", "horizon"),
}


def create_app(settings: Optional[Settings] = None, pipeline=None) -> FastAPI:
    return create_service_app(
        title="Telegraph Weather Forecast Miner", intent="WEATHER_FORECAST_GENERAL",
        path="/weather-forecast", version=VERSION, fields=FIELDS, model=ForecastResponse,
        settings=settings or Settings.from_env(), pipeline=pipeline, make_pipeline=ForecastPipeline)
