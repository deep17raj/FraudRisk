"""CVE data from NVD (preferred) and CIRCL's vulnerability-lookup (fallback). Both are keyless.

`lookup` returns `None` when neither source could be asked, and a `CveInfo` with `score=None` when the CVE
is known but carries no CVSS score yet (NVD's "awaiting analysis"), so the two cases are never confused.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from intentkit.cache import TTLCache
from intentkit.upstream import Upstream

NVD = "https://services.nvd.nist.gov/rest/json/cves/2.0"
CIRCL = "https://vulnerability.circl.lu/api/cve/"
_CVE = re.compile(r"CVE-\d{4}-\d{4,}", re.I)
SEVERITIES = ("NONE", "LOW", "MEDIUM", "HIGH", "CRITICAL")


def parse_cve_id(raw: str) -> Optional[str]:
    m = _CVE.search(raw or "")
    return m.group(0).upper() if m else None


def severity_for(score: float) -> str:
    """The CVSS v3 qualitative rating of a base score."""
    return ("NONE" if score == 0 else "LOW" if score < 4 else "MEDIUM" if score < 7
            else "HIGH" if score < 9 else "CRITICAL")


@dataclass(frozen=True)
class CveInfo:
    cve_id: str
    score: Optional[float] = None
    severity: Optional[str] = None
    description: str = ""
    published: str = ""
    references: tuple[str, ...] = field(default_factory=tuple)
    source: str = ""


#: The source identifier NVD puts on the metrics its own analysts scored.
NVD_SOURCE = "nvd@nist.gov"


def _score(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 10 else None


def _from_nvd(cve_id: str, body: Any) -> Optional[CveInfo]:
    vulns = body.get("vulnerabilities") if isinstance(body, dict) else None
    if not isinstance(vulns, list) or not vulns or not isinstance(vulns[0], dict):
        return None
    cve = vulns[0].get("cve") or {}
    metrics = cve.get("metrics") or {}
    score = None
    for key in ("cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
        entries = [e for e in (metrics.get(key) or []) if isinstance(e, dict)]
        # NVD's own assessment first. Some records carry no Primary metric at all and several
        # Secondary ones instead: CVE-2020-1472 (Zerologon) holds Microsoft's 5.5, scored for local
        # privilege escalation, next to NVD's 10.0, and 10.0 is the score NVD publishes for it. So
        # fall back to the entry NVD itself authored rather than to whichever came first.
        entries.sort(key=lambda e: (e.get("type") != "Primary", e.get("source") != NVD_SOURCE))
        for e in entries:
            score = _score((e.get("cvssData") or {}).get("baseScore"))
            if score is not None:
                break
        if score is not None:
            break
    desc = next((d.get("value", "") for d in cve.get("descriptions") or [] if d.get("lang") == "en"), "")
    refs = tuple(r["url"] for r in cve.get("references") or [] if isinstance(r, dict) and r.get("url"))[:10]
    return CveInfo(cve.get("id") or cve_id, score, severity_for(score) if score is not None else None,
                   desc.strip(), str(cve.get("published", ""))[:10], refs, "NVD")


def _walk_scores(node: Any, out: list[float]) -> None:
    """Collect every CVSS v3.x base score in a CVE record (the CNA's and each ADP container's)."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("cvssV3_1", "cvssV3_0") and isinstance(v, dict):
                s = _score(v.get("baseScore"))
                if s is not None:
                    out.append(s)
            else:
                _walk_scores(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_scores(v, out)


def _from_circl(cve_id: str, body: Any) -> Optional[CveInfo]:
    if not isinstance(body, dict) or "cveMetadata" not in body:
        return None
    containers = body.get("containers") or {}
    cna = containers.get("cna") or {}
    scores: list[float] = []
    _walk_scores(cna.get("metrics"), scores)           # the CNA's own score before any ADP enrichment
    if not scores:
        _walk_scores(containers.get("adp"), scores)
    score = scores[0] if scores else None
    desc = next((d.get("value", "") for d in cna.get("descriptions") or [] if d.get("lang", "en").startswith("en")), "")
    refs = tuple(r["url"] for r in cna.get("references") or [] if isinstance(r, dict) and r.get("url"))[:10]
    published = str((body.get("cveMetadata") or {}).get("datePublished", ""))[:10]
    return CveInfo(cve_id, score, severity_for(score) if score is not None else None, desc.strip(), published, refs,
                   "CIRCL")


#: Shared by CVE_LOOKUP_CVSS and VULNERABILITY_TRIAGE_GENERAL, so a CVE both are asked about costs one NVD
#: request (NVD rate-limits keyless clients to about 5 per 30 s).
SHARED = TTLCache(max_items=2000)
TTL, TTL_DEGRADED = 21600, 60


async def lookup(up: Upstream, cve_id: str, nvd_api_key: str = "") -> Optional[CveInfo]:
    hit = SHARED.get(cve_id)
    if hit is not None:
        return hit

    async def fetch() -> Optional[CveInfo]:
        info = await _lookup(up, cve_id, nvd_api_key)
        if info is not None:
            SHARED.set(cve_id, info, TTL if info.score is not None else TTL_DEGRADED)
        return info

    return await SHARED.single_flight(cve_id, fetch)


async def _lookup(up: Upstream, cve_id: str, nvd_api_key: str = "") -> Optional[CveInfo]:
    headers = {"apiKey": nvd_api_key} if nvd_api_key else None
    res = await up.fetch(NVD, params={"cveId": cve_id}, headers=headers, key="nvd")
    nvd = _from_nvd(cve_id, res[1]) if res is not None and res[0] == 200 else None
    if nvd is not None and nvd.score is not None:
        return nvd
    res2 = await up.fetch(CIRCL + cve_id, key="circl")
    circl = _from_circl(cve_id, res2[1]) if res2 is not None and res2[0] == 200 else None
    if circl is not None and circl.score is not None:
        # NVD knows the CVE but has no score yet: CIRCL's copy of the CNA score fills the gap.
        return CveInfo(cve_id, circl.score, circl.severity, (nvd.description if nvd else "") or circl.description,
                       (nvd.published if nvd else "") or circl.published, (nvd.references if nvd else ()) or circl.references,
                       "CIRCL")
    if nvd is not None or circl is not None:
        return nvd or circl                        # known CVE, no CVSS score anywhere
    return None if (res is None and res2 is None) else CveInfo(cve_id, source="not found")
