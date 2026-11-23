"""SLA_COMPLIANCE_GENERAL from Upptime's published uptime figures (keyless).

Upptime is an open-source uptime monitor whose public repositories publish `history/summary.json` with, per monitored
service, the availability percentage for the last day, week, month (30 days), year and all time. This intent returns
those published numbers verbatim, and nothing it computes itself:
  * the period must be one Upptime publishes (24 hours / 7 days / 30 days / 365 days / all time, in the usual wordings);
    any other window gives `uptime_pct` null. (Recomputing other windows from the daily downtime minutes was tried and
    did not reproduce Upptime's own day and month figures, so it is not offered.)
  * the service must be one a configured Upptime repository monitors (matched by name, slug or site host); otherwise null.
  * `sla_target_pct` is only a percentage written in the request text ("99.9%"); otherwise null. `compliant` is true only
    when the uptime and the target are both known and the uptime is at least the target, so `false` also means
    "cannot confirm" when either is unknown.
Status pages that publish incidents rather than uptime (Atlassian Statuspage) are deliberately not used: summing incident
durations would be an invented number.
"""
from __future__ import annotations

import re
import time
from typing import Optional
from urllib.parse import urlparse

from fastapi import FastAPI
from pydantic import BaseModel, Field

from intentkit.pipeline import BasePipeline, Req, Result
from intentkit.service import StrictModel, create_service_app
from intentkit.text import clip

from .config import Settings

VERSION = "0.1.0"
FIELDS = {"service": ("name", "site", "endpoint", "monitor", "query", "text", "input"),
          "period": ("window", "timeframe", "range", "duration")}
SUMMARY = "https://raw.githubusercontent.com/{repo}/HEAD/history/summary.json"
FIELD_FOR = {"day": "uptimeDay", "week": "uptimeWeek", "month": "uptimeMonth", "year": "uptimeYear", "all": "uptime"}
_PCT = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")


def published_period(text: str) -> Optional[str]:
    """Which of Upptime's published windows a phrase means: day | week | month | year | all, else None."""
    s = (text or "").strip().lower()
    if not s:
        return None
    if re.search(r"\b(all[\s-]*time|overall|lifetime|ever|since (?:the )?(?:start|beginning))\b", s):
        return "all"
    n = re.search(r"\b(?:last|past|previous)?\s*(\d+)\s*(hours?|hrs?|h|days?|d|weeks?|w|months?|years?|y)\b", s)
    if n:
        v, unit = int(n.group(1)), n.group(2)
        if unit.startswith("mo"):                        # "12 months" is Upptime's year, "1 month" its 30-day month
            return {1: "month", 12: "year"}.get(v)
        if unit.startswith("y"):
            return "year" if v == 1 else None
        if unit.startswith("w"):
            return "week" if v == 1 else None
        days = v / 24 if unit.startswith("h") else v
        return {1: "day", 7: "week", 30: "month", 365: "year"}.get(int(days)) if days == int(days) else None
    for words, name in ((r"\b(day|24 ?h|yesterday|today)\b", "day"), (r"\b(week)\b", "week"), (r"\b(month)\b", "month"), (r"\b(year|12 months)\b", "year")):
        if re.search(words, s):
            return name
    return None


def _norm(text: str) -> str:
    t = (text or "").strip().lower()
    if "://" in t or re.match(r"^[\w.-]+\.[a-z]{2,}(/|$)", t):
        host = urlparse(t if "://" in t else "//" + t).hostname or t
        t = re.sub(r"^www\.", "", host)
        t = t.rsplit(".", 1)[0] if t.count(".") >= 1 else t
    return re.sub(r"[^a-z0-9]+", "", t)


def target_in(*texts: str) -> Optional[float]:
    for t in texts:
        m = _PCT.search(t or "")
        if m and 0 < float(m.group(1)) <= 100:
            return float(m.group(1))
    return None


class SlaResponse(StrictModel):
    """Exactly the response fields of Telegraph's SLA_COMPLIANCE_GENERAL intent. Nothing else."""

    service: str = ""
    uptime_pct: Optional[float] = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    sla_target_pct: Optional[float] = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    compliant: bool = False
    period: str = ""


class SlaPipeline(BasePipeline):
    intent = "SLA_COMPLIANCE_GENERAL"

    def __init__(self, settings: Settings, client) -> None:
        super().__init__(settings, client)
        self._tables: dict[str, tuple[float, list[dict]]] = {}

    def key(self, req: Req) -> Optional[str]:
        s = re.sub(r"\s+", " ", req.get("service", "")).strip().lower()
        return f"sla:{s}|{req.get('period', '').strip().lower()}" if s else None

    def empty(self, req: Req, reason: str) -> BaseModel:
        return SlaResponse(service=req.get("service", "").strip()[:200], period=req.get("period", "").strip()[:100])

    async def _services(self, repo: str) -> Optional[list[dict]]:
        hit = self._tables.get(repo)
        if hit and time.monotonic() - hit[0] < 600:
            return hit[1]
        d = await self.up.get_json(SUMMARY.format(repo=repo), key="upptime")
        if isinstance(d, list):
            rows = [x for x in d if isinstance(x, dict)]
            self._tables[repo] = (time.monotonic(), rows)
            return rows
        return hit[1] if hit else None

    async def compute(self, req: Req) -> Result:
        service_raw, period_raw = req.get("service", "").strip(), req.get("period", "").strip()
        if not service_raw:
            return Result(self.empty(req, "no service"), degraded=True)
        base = SlaResponse(service=clip(service_raw, 200), period=clip(period_raw, 100))
        window = published_period(period_raw) or published_period(service_raw)
        target = target_in(service_raw, period_raw)
        want = _norm(re.sub(_PCT, " ", service_raw))
        reached, match = False, None
        for repo in [r.strip() for r in self.settings.upptime_repos.split(",") if r.strip()]:
            rows = await self._services(repo)
            if rows is None:
                continue
            reached = True
            for row in rows:
                if want and want in {_norm(str(row.get("name", ""))), _norm(str(row.get("slug", ""))), _norm(str(row.get("url", "")))}:
                    match = row
                    break
            if match:
                break
        if match is None or window is None:
            return Result(base.model_copy(update={"sla_target_pct": target}), degraded=not reached)
        m = _PCT.search(str(match.get(FIELD_FOR[window], "")))
        uptime = float(m.group(1)) if m and 0 <= float(m.group(1)) <= 100 else None
        return Result(SlaResponse(service=base.service, uptime_pct=uptime, sla_target_pct=target,
                                  compliant=uptime is not None and target is not None and uptime >= target, period=base.period))


def create_app(settings: Optional[Settings] = None, pipeline: Optional[SlaPipeline] = None) -> FastAPI:
    return create_service_app(
        title="Telegraph SLA Compliance Miner", intent="SLA_COMPLIANCE_GENERAL", path="/sla-compliance", version=VERSION,
        fields=FIELDS, model=SlaResponse, settings=settings or Settings.from_env(), pipeline=pipeline,
        make_pipeline=SlaPipeline, max_len=300)
