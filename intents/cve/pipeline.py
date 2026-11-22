from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from intentkit.pipeline import BasePipeline, Req, Result
from intentkit.service import StrictModel

from .config import Settings
from .source import lookup, parse_cve_id


class CveResponse(StrictModel):
    """Exactly the response fields of Telegraph's CVE_LOOKUP_CVSS intent. Nothing else.

    `severity` is one of the spec's enum values, or null when the CVE has no CVSS score to rate.
    """

    cve_id: str = ""
    cvss_score: Optional[float] = Field(default=None, ge=0, le=10, allow_inf_nan=False)
    severity: Optional[Literal["NONE", "LOW", "MEDIUM", "HIGH", "CRITICAL"]] = None
    description: str = ""
    published: str = ""
    references: list[str] = Field(default_factory=list)


class CvePipeline(BasePipeline):
    intent = "CVE_LOOKUP_CVSS"

    def key(self, req: Req) -> Optional[str]:
        cve = parse_cve_id(req.get("cve_id", ""))
        return f"cve:{cve}" if cve else None

    def empty(self, req: Req, reason: str) -> BaseModel:
        return CveResponse(cve_id=(parse_cve_id(req.get("cve_id", "")) or req.get("cve_id", "").strip())[:50])

    async def compute(self, req: Req) -> Result:
        cve = parse_cve_id(req.get("cve_id", ""))
        if cve is None:
            return Result(self.empty(req, "no CVE id"), degraded=True)
        s: Settings = self.settings
        info = await lookup(self.up, cve, s.nvd_api_key)
        if info is None:
            return Result(self.empty(req, "no source answered"), degraded=True)
        return Result(CveResponse(cve_id=info.cve_id, cvss_score=info.score, severity=info.severity,
                                  description=info.description[:1000], published=info.published,
                                  references=list(info.references)),
                      degraded=info.score is None)
