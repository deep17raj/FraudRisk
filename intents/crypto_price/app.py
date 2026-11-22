from __future__ import annotations

from typing import Optional

from fastapi import FastAPI

from intentkit.request import GENERIC_ALIASES
from intentkit.service import create_service_app

from .config import Settings
from .pipeline import CryptoPricePipeline, CryptoPriceResponse

VERSION = "0.1.0"
FIELDS = {"symbol": ("ticker", "coin", "asset", "token", "currency", "base", *GENERIC_ALIASES)}


def create_app(settings: Optional[Settings] = None, pipeline: Optional[CryptoPricePipeline] = None) -> FastAPI:
    return create_service_app(
        title="Telegraph Crypto Price Miner", intent="CRYPTO_PRICE_USD", path="/crypto-price", version=VERSION,
        fields=FIELDS, model=CryptoPriceResponse, settings=settings or Settings.from_env(), pipeline=pipeline,
        make_pipeline=CryptoPricePipeline)
