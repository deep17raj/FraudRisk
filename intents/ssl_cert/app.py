from __future__ import annotations

from typing import Optional

from fastapi import FastAPI

from intentkit.request import GENERIC_ALIASES
from intentkit.service import create_service_app

from .config import Settings
from .pipeline import SslPipeline, SslResponse

VERSION = "0.1.0"
FIELDS = {"host": ("hostname", "domain", "url", "site", "name", *GENERIC_ALIASES)}


def create_app(settings: Optional[Settings] = None, pipeline: Optional[SslPipeline] = None) -> FastAPI:
    return create_service_app(
        title="Telegraph SSL Certificate Miner", intent="SSL_VERIFICATION_CERT", path="/ssl-verification",
        version=VERSION, fields=FIELDS, model=SslResponse, settings=settings or Settings.from_env(),
        pipeline=pipeline, make_pipeline=SslPipeline)
