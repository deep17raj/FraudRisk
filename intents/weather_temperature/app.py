from __future__ import annotations

from typing import Optional

from fastapi import FastAPI

from intentkit.request import GENERIC_ALIASES
from intentkit.service import create_service_app
from intents.weather.config import Settings
from intents.weather.pipelines import TemperaturePipeline, TemperatureResponse

VERSION = "0.1.0"
FIELDS = {"location": ("city", "place", "name", "where", *GENERIC_ALIASES)}


def create_app(settings: Optional[Settings] = None, pipeline=None) -> FastAPI:
    return create_service_app(
        title="Telegraph Weather Temperature Miner", intent="WEATHER_CHECK_TEMPERATURE",
        path="/weather-temperature", version=VERSION, fields=FIELDS, model=TemperatureResponse,
        settings=settings or Settings.from_env(), pipeline=pipeline, make_pipeline=TemperaturePipeline)
