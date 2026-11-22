from __future__ import annotations

from typing import Optional

from fastapi import FastAPI

from intentkit.request import GENERIC_ALIASES
from intentkit.service import create_service_app

from .config import Settings
from .pipeline import CvePipeline, CveResponse

VERSION = "0.1.0"
FIELDS = {"cve_id": ("cve", "id", "vulnerability", *GENERIC_ALIASES)}


def create_app(settings: Optional[Settings] = None, pipeline: Optional[CvePipeline] = None) -> FastAPI:
    return create_service_app(
        title="Telegraph CVE Lookup Miner", intent="CVE_LOOKUP_CVSS", path="/cve-lookup", version=VERSION,
        fields=FIELDS, model=CveResponse, settings=settings or Settings.from_env(), pipeline=pipeline,
        make_pipeline=CvePipeline)
