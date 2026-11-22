from __future__ import annotations

from typing import Optional

from fastapi import FastAPI
from pydantic import BaseModel, Field

from intentkit.pipeline import BasePipeline, Req, Result
from intentkit.request import GENERIC_ALIASES
from intentkit.service import StrictModel, create_service_app
from intents.cve.config import Settings
from intents.cve.source import lookup, parse_cve_id

VERSION = "0.1.0"
FIELDS = {"cve_id": ("cve", "id", "vulnerability", *GENERIC_ALIASES)}


class VulnTriageResponse(StrictModel):
    """Exactly the response field of Telegraph's VULNERABILITY_TRIAGE_GENERAL intent. Nothing else."""

    cvss_base_score: Optional[float] = Field(default=None, ge=0, le=10, allow_inf_nan=False)


class VulnTriagePipeline(BasePipeline):
    intent = "VULNERABILITY_TRIAGE_GENERAL"

    def key(self, req: Req) -> Optional[str]:
        cve = parse_cve_id(req.get("cve_id", ""))
        return f"cve:{cve}" if cve else None

    def empty(self, req: Req, reason: str) -> BaseModel:
        return VulnTriageResponse(cvss_base_score=None)

    async def compute(self, req: Req) -> Result:
        cve = parse_cve_id(req.get("cve_id", ""))
        if cve is None:
            return Result(self.empty(req, "no CVE id"), degraded=True)
        info = await lookup(self.up, cve, self.settings.nvd_api_key)
        if info is None or info.score is None:
            return Result(self.empty(req, "no CVSS score available"), degraded=True)
        return Result(VulnTriageResponse(cvss_base_score=info.score))


def create_app(settings: Optional[Settings] = None, pipeline: Optional[VulnTriagePipeline] = None) -> FastAPI:
    return create_service_app(
        title="Telegraph Vulnerability Triage Miner", intent="VULNERABILITY_TRIAGE_GENERAL",
        path="/vulnerability-triage", version=VERSION, fields=FIELDS, model=VulnTriageResponse,
        settings=settings or Settings.from_env(), pipeline=pipeline, make_pipeline=VulnTriagePipeline)
