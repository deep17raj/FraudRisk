from __future__ import annotations

from typing import Optional

from fastapi import FastAPI

from intents.fx.app_factory import create_fx_app
from intents.fx.config import Settings


def create_app(settings: Optional[Settings] = None, pipeline=None) -> FastAPI:
    return create_fx_app("EUR", "/fx-now-eur", settings, pipeline)
