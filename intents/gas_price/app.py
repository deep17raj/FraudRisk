from __future__ import annotations

from typing import Optional

from fastapi import FastAPI

from intentkit.request import GENERIC_ALIASES
from intentkit.service import create_service_app

from .config import Settings
from .pipeline import GasPricePipeline, GasPriceResponse

VERSION = "0.1.0"
FIELDS = {"chain": ("network", "blockchain", "chain_name", *GENERIC_ALIASES)}


def create_app(settings: Optional[Settings] = None, pipeline: Optional[GasPricePipeline] = None) -> FastAPI:
    return create_service_app(
        title="Telegraph Gas Price Miner", intent="GAS_PRICE_GENERAL", path="/gas-price", version=VERSION,
        fields=FIELDS, model=GasPriceResponse, settings=settings or Settings.from_env(), pipeline=pipeline,
        make_pipeline=GasPricePipeline)
