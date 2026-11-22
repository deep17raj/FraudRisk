from __future__ import annotations

from typing import Optional

from fastapi import FastAPI

from intentkit.request import GENERIC_ALIASES
from intentkit.service import create_service_app

from .config import Settings
from .pipelines import ConvertPipeline, ConvertResponse, FxRateResponse, fx_now

VERSION = "0.1.0"
FX_FIELDS = {"currency": ("code", "symbol", "ccy", "to", "quote", *GENERIC_ALIASES)}
CONVERT_FIELDS = {
    "from_currency": ("from", "source", "base", "src"),
    "to_currency": ("to", "target", "quote", "dst"),
    "amount": ("value", "qty"),
    # Not a spec param: a caller that sends one sentence ("100 USD to EUR") is still understood.
    "query": ("question", "q", "text", "input"),
}


def create_fx_app(code: str, path: str, settings: Optional[Settings] = None, pipeline=None) -> FastAPI:
    return create_service_app(
        title=f"Telegraph FX Now {code} Miner", intent=f"FX_NOW_{code}", path=path, version=VERSION,
        fields=FX_FIELDS, model=FxRateResponse, settings=settings or Settings.from_env(), pipeline=pipeline,
        make_pipeline=fx_now(code))


def create_convert_app(settings: Optional[Settings] = None, pipeline=None) -> FastAPI:
    return create_service_app(
        title="Telegraph Currency Convert Miner", intent="CURRENCY_EXCHANGE_CONVERT", path="/currency-convert",
        version=VERSION, fields=CONVERT_FIELDS, model=ConvertResponse, settings=settings or Settings.from_env(),
        pipeline=pipeline, make_pipeline=ConvertPipeline)
