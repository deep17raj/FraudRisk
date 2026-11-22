from __future__ import annotations

from typing import Optional

from fastapi import FastAPI

from intents.fx.app_factory import create_convert_app
from intents.fx.config import Settings


def create_app(settings: Optional[Settings] = None, pipeline=None) -> FastAPI:
    return create_convert_app(settings, pipeline)
