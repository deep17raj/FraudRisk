from __future__ import annotations

from typing import Optional

from fastapi import FastAPI

from intentkit.request import GENERIC_ALIASES
from intentkit.service import create_service_app

from .config import Settings
from .pipeline import DnsPipeline, DnsResponse

VERSION = "0.1.0"
FIELDS = {
    "name": ("domain", "host", "hostname", "email", *GENERIC_ALIASES),
    "record_type": ("type", "rtype", "record", "dns_type"),
}


def create_app(settings: Optional[Settings] = None, pipeline: Optional[DnsPipeline] = None) -> FastAPI:
    return create_service_app(
        title="Telegraph DNS Record Miner", intent="DNS_RECORD_LOOKUP_GENERAL", path="/dns-record",
        version=VERSION, fields=FIELDS, model=DnsResponse, settings=settings or Settings.from_env(),
        pipeline=pipeline, make_pipeline=DnsPipeline, max_len=500)
