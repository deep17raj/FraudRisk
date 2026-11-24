"""REGULATORY_FILING_MONITOR_GENERAL from SEC EDGAR (keyless).

If `entity` names an SEC registrant (a company name or ticker found in SEC's company list), the answer is that
registrant's own filings in the period, from its EDGAR submissions record, newest first. Otherwise `entity` is
treated as a concern (a product, a topic) and the answer is EDGAR full-text search for filings that mention it
in the period. The count is complete; the title and URL lists are capped at `MAX_LISTED`.
"""
from __future__ import annotations

import time
from datetime import date
from typing import Optional

from fastapi import FastAPI
from pydantic import BaseModel, Field

from intentkit.ask import filings_parts
from intentkit.names import normalize
from intentkit.pipeline import BasePipeline, Req, Result
from intentkit.request import GENERIC_ALIASES
from intentkit.service import StrictModel, create_service_app

from .config import Settings
from .periods import parse_period

VERSION = "0.1.0"
TICKERS = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
FTS = "https://efts.sec.gov/LATEST/search-index"
FIELDS = {"entity": ("company", "concern", "name", "product", "subject", *GENERIC_ALIASES),
          "period": ("timeframe", "range", "window", "dates")}


class FilingsResponse(StrictModel):
    """Exactly the response fields of Telegraph's REGULATORY_FILING_MONITOR_GENERAL intent. Nothing else."""

    entity: str = ""
    filing_count: Optional[int] = Field(default=None, ge=0, le=100_000_000)
    filings: list[str] = Field(default_factory=list)
    urls: list[str] = Field(default_factory=list)


class FilingsPipeline(BasePipeline):
    intent = "REGULATORY_FILING_MONITOR_GENERAL"

    def __init__(self, settings: Settings, client) -> None:
        super().__init__(settings, client)
        self._tickers: list[tuple[int, str, tuple[str, ...]]] = []
        self._tickers_at = 0.0
        self.headers = {"User-Agent": settings.sec_user_agent}

    # ---- registrant resolution --------------------------------------------------------------------
    async def _registrants(self) -> list[tuple[int, str, tuple[str, ...]]]:
        s: Settings = self.settings
        if self._tickers and time.monotonic() - self._tickers_at < s.tickers_ttl:
            return self._tickers
        d = await self.up.get_json(TICKERS, headers=self.headers, timeout=15.0)
        if isinstance(d, dict):
            rows = [(int(v["cik_str"]), f"{v.get('ticker', '')}|{v.get('title', '')}", normalize(v.get("title", "")))
                    for v in d.values() if isinstance(v, dict) and "cik_str" in v]
            if rows:
                self._tickers, self._tickers_at = rows, time.monotonic()
        return self._tickers

    async def _resolve(self, entity: str) -> Optional[tuple[int, str]]:
        rows = await self._registrants()
        words, upper = normalize(entity), entity.strip().upper()
        best: Optional[tuple[int, str]] = None
        best_score = 0.0
        for cik, label, cw in rows:
            ticker, title = label.split("|", 1)
            if ticker and ticker.upper() == upper and len(upper) <= 5:
                return cik, title
            if words and cw == words:
                score = 1.0
            elif len(words) >= 2 and set(words) < set(cw) and len(cw) <= len(words) + 2:
                score = 0.9
            else:
                continue
            if score > best_score:
                best, best_score = (cik, title), score
        return best

    # ---- answers -----------------------------------------------------------------------------------
    def key(self, req: Req) -> Optional[str]:
        entity, period = filings_parts(req.get("entity", "").strip(), req.get("period", ""))
        start, end = parse_period(period)
        return f"{entity.casefold()}|{start}|{end}" if entity else None

    def empty(self, req: Req, reason: str) -> BaseModel:
        return FilingsResponse(entity=req.get("entity", "")[:200])

    async def compute(self, req: Req) -> Result:
        s: Settings = self.settings
        entity, period = filings_parts(req.get("entity", "").strip(), req.get("period", ""))
        if not entity:
            return Result(self.empty(req, "no entity"), degraded=True)
        start, end = parse_period(period)
        reg = await self._resolve(entity)
        if reg is not None:
            res = await self._registrant_filings(entity, reg, start, end)
        else:
            res = await self._full_text(entity, start, end)
        return Result(res, degraded=res.filing_count is None)

    async def _registrant_filings(self, entity: str, reg: tuple[int, str], start: date, end: date) -> FilingsResponse:
        cik, _ = reg
        d = await self.up.get_json(SUBMISSIONS.format(cik=cik), headers=self.headers, timeout=10.0)
        rec = ((d or {}).get("filings") or {}).get("recent") if isinstance(d, dict) else None
        if not isinstance(rec, dict):
            return FilingsResponse(entity=entity[:200])
        acc, dates, forms = rec.get("accessionNumber") or [], rec.get("filingDate") or [], rec.get("form") or []
        docs, descs = rec.get("primaryDocument") or [], rec.get("primaryDocDescription") or []
        keep: list[tuple[str, str]] = []
        for i, fd in enumerate(dates):
            try:
                day = date.fromisoformat(fd)
            except (TypeError, ValueError):
                continue
            if start <= day <= end:
                form = forms[i] if i < len(forms) else ""
                desc = (descs[i] if i < len(descs) else "") or ""
                title = f"{form}: {desc} ({fd})" if desc and desc != form else f"{form} ({fd})"
                url = (f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc[i].replace('-', '')}/{docs[i]}"
                       if i < len(acc) and i < len(docs) and docs[i] else
                       f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik:010d}")
                keep.append((title, url))
        shown = keep[: self.settings.max_listed]
        return FilingsResponse(entity=entity[:200], filing_count=len(keep),
                               filings=[t for t, _ in shown], urls=[u for _, u in shown])

    async def _full_text(self, entity: str, start: date, end: date) -> FilingsResponse:
        d = await self.up.get_json(FTS, params={"q": f'"{entity}"', "dateRange": "custom",
                                                "startdt": start.isoformat(), "enddt": end.isoformat()},
                                   headers=self.headers, timeout=10.0)
        hits = (d or {}).get("hits") if isinstance(d, dict) else None
        if not isinstance(hits, dict):
            return FilingsResponse(entity=entity[:200])
        total = (hits.get("total") or {}).get("value")
        titles, urls = [], []
        for h in (hits.get("hits") or [])[: self.settings.max_listed]:
            src, _id = h.get("_source") or {}, str(h.get("_id", ""))
            adsh, _, fname = _id.partition(":")
            ciks = src.get("ciks") or []
            who = (src.get("display_names") or [""])[0].split("  (CIK")[0].strip()
            titles.append(f"{src.get('form', '')} {who} ({src.get('file_date', '')})".strip())
            urls.append(f"https://www.sec.gov/Archives/edgar/data/{int(ciks[0])}/{adsh.replace('-', '')}/{fname}"
                        if ciks and adsh and fname else "")
        return FilingsResponse(entity=entity[:200], filing_count=int(total) if isinstance(total, int) else None,
                               filings=titles, urls=urls)


def create_app(settings: Optional[Settings] = None, pipeline: Optional[FilingsPipeline] = None) -> FastAPI:
    return create_service_app(
        title="Telegraph Regulatory Filing Miner", intent="REGULATORY_FILING_MONITOR_GENERAL",
        path="/regulatory-filings", version=VERSION, fields=FIELDS, model=FilingsResponse,
        settings=settings or Settings.from_env(), pipeline=pipeline, make_pipeline=FilingsPipeline, max_len=300)
