"""SECURITY_REVIEW_GENERAL.

What is reviewed depends on what `target` is, and every finding is either deterministic or grounded:
  * a GitHub repository URL: its dependency manifests are read and checked against OSV, and a committed `.env` is flagged;
  * a website or URL: TLS validity, plain-HTTP exposure, hardening headers, cookie flags, and phishing/malware listings;
  * pasted dependencies (requirements.txt, package.json, lock files, `name@version`): checked against OSV;
  * pasted code or a Solidity contract: dangerous-construct rules mapped to CWE / SWC ids, plus, when `GROQ_API_KEY` is
    set, findings from Groq that are counted ONLY if they quote code that really occurs in the input.
`vulnerability_count` is the number of distinct findings. When nothing could be examined (a bare contract address, a
private repository, an unreachable site, OSV down) `verdict` and `vulnerability_count` are null: "Secure" is never
claimed without having looked.
"""
from __future__ import annotations

import asyncio
import re
from typing import Literal, Optional

import httpx
from fastapi import FastAPI
from pydantic import BaseModel, Field

from intentkit.groq import parse_json_object
from intentkit.llm_pipeline import UNTRUSTED_NOTE, LlmPipeline
from intentkit.pipeline import Req, Result
from intentkit.service import StrictModel, create_service_app
from intentkit.text import clip
from intents.ssl_cert.config import Settings as SslSettings
from intents.ssl_cert.pipeline import SslPipeline
from urlsafe.config import Settings as UrlSafeSettings
from urlsafe.pipeline import UrlSafePipeline
from urlsafe.verdict import Verdict as UrlVerdict

from . import osv
from .config import Settings
from .findings import Finding, grounded, is_solidity, looks_like_code, normalise_id, scan_code, sort_findings
from .site import parse_site, review_site

VERSION = "0.1.0"
FIELDS = {"target": ("code", "repo", "repository", "url", "package", "contract", "site", "input", "query", "text")}
RAW = "https://raw.githubusercontent.com/{owner}/{repo}/HEAD/{path}"
MANIFESTS = ("requirements.txt", "package.json", "package-lock.json", "go.mod", "Cargo.lock")
_REPO = re.compile(r"github\.com/([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+?)(?:\.git)?(?:[/#?\s]|$)", re.I)
_ADDRESS = re.compile(r"^\s*0x[0-9a-fA-F]{40}\s*$")
_SEV = ("critical", "high", "medium", "low")

SYSTEM = ("You are a senior application-security reviewer. Find REAL security vulnerabilities in the code (injection, "
          "unsafe deserialization, hard-coded secrets, broken authentication or access control, weak cryptography, "
          "path traversal, SSRF, unsafe contract patterns) - not style issues. For each finding give a CWE or SWC id, a "
          "short title, a severity (critical, high, medium or low) and an EXACT quote of the vulnerable code (at most 160 "
          "characters, copied verbatim). Report at most 8. If the code has no security problem, return an empty list. "
          + UNTRUSTED_NOTE)


class SecurityReviewResponse(StrictModel):
    """Exactly the response fields of Telegraph's SECURITY_REVIEW_GENERAL intent. Nothing else."""

    verdict: Optional[Literal["Secure", "Needs changes", "Insecure"]] = None
    vulnerability_count: Optional[int] = Field(default=None, ge=0, le=100000)
    issues: list[str] = Field(default_factory=list)
    summary: str = ""


def _note(text: str) -> SecurityReviewResponse:
    return SecurityReviewResponse(summary=text)


def _verdict(findings: list[Finding]) -> str:
    if any(f.severity in ("critical", "high") for f in findings):
        return "Insecure"
    return "Needs changes" if findings else "Secure"


def _summary(findings: list[Finding], analyzed: str, scope: str) -> str:
    if not findings:
        return clip(f"No known vulnerabilities or weaknesses were found in {analyzed}. This covers {scope} only and is "
                    "not a guarantee of security.", 400)
    by = {s: sum(1 for f in findings if f.severity == s) for s in _SEV}
    parts = ", ".join(f"{n} {s}" for s, n in by.items() if n)
    top = sort_findings(findings)[0]
    return clip(f"Reviewed {analyzed}: found {len(findings)} issue{'s' if len(findings) != 1 else ''} ({parts}). "
                f"Most serious: {top.id} {top.title}.", 450)


class SecurityReviewPipeline(LlmPipeline):
    intent = "SECURITY_REVIEW_GENERAL"
    WARM = ("https://api.osv.dev/v1/vulns/GHSA-29mw-wpgm-hmr9",)

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        super().__init__(settings, client)
        self.ssl = SslPipeline(SslSettings(provider_timeout=settings.provider_timeout,
                                           allow_private_hosts=settings.allow_private_hosts), client)
        self.urlsafe = UrlSafePipeline(UrlSafeSettings(), client)

    async def start(self) -> None:
        await asyncio.gather(super().start(), self.urlsafe.start(), return_exceptions=True)

    def key(self, req: Req) -> Optional[str]:
        t = re.sub(r"\s+", " ", req.get("target", "")).strip()
        return f"sr:{t[:3000]}" if t else None

    def empty(self, req: Req, reason: str) -> BaseModel:
        return _note(f"The review could not be completed ({reason}).") if req.get("target", "").strip() else SecurityReviewResponse()

    # ---- helpers ------------------------------------------------------------------------------------------
    def _result(self, findings: Optional[list[Finding]], analyzed: str, scope: str, *, ttl: Optional[float] = None) -> Result:
        s: Settings = self.settings
        if findings is None:
            return Result(_note(f"Nothing could be examined: {analyzed}. No verdict is given."), degraded=True)
        ordered = sort_findings(findings)
        return Result(SecurityReviewResponse(
            verdict=_verdict(ordered), vulnerability_count=len(ordered),
            issues=[f.render() for f in ordered[:s.max_issues_listed]], summary=_summary(ordered, analyzed, scope)),
            ttl=ttl)

    async def _packages_review(self, pkgs: list[osv.Pkg]) -> Optional[list[Finding]]:
        return await osv.scan(self.up, pkgs[: self.settings.max_packages], self.settings.max_detail_lookups)

    # ---- the three kinds of target ---------------------------------------------------------------------------
    async def _repo(self, owner: str, repo: str) -> Result:
        responses = await asyncio.gather(*(
            self.up.fetch(RAW.format(owner=owner, repo=repo, path=p), key="github-raw", timeout=8.0, text=True)
            for p in (*MANIFESTS, ".env")))
        found = {p: r[1] for p, r in zip((*MANIFESTS, ".env"), responses) if r is not None and r[0] == 200 and isinstance(r[1], str)}
        reached = any(r is not None for r in responses)
        manifests = {p: t for p, t in found.items() if p != ".env"}
        if not manifests and ".env" not in found:
            why = "the repository is private, does not exist, or has no supported dependency manifest" if reached else "GitHub could not be reached"
            return self._result(None, f"{owner}/{repo}: {why}", "")
        pkgs: list[osv.Pkg] = []
        for text in manifests.values():
            pkgs += osv.parse_packages(text, self.settings.max_packages)
        findings = await self._packages_review(pkgs) if pkgs else []
        if findings is None:
            return self._result(None, "the OSV vulnerability database could not be reached", "")
        if ".env" in found:
            findings.append(Finding("CWE-540", "A .env file is committed to the repository (secrets in source control)",
                                    "high", f"{owner}/{repo}/.env"))
        names = ", ".join(manifests) or "no manifests"
        return self._result(findings, f"{owner}/{repo} ({len(pkgs)} packages from {names})",
                            "known vulnerabilities in declared dependencies and a committed .env file")

    async def _site(self, url: str) -> Result:
        s: Settings = self.settings
        host = re.sub(r"^https?://", "", url).split("/")[0].split(":")[0]
        tls, listed = await asyncio.gather(self.ssl.answer({"host": host}), self.urlsafe.check(url), return_exceptions=True)
        tls_valid = None if isinstance(tls, BaseException) else tls.certificate_valid
        findings, what = await review_site(self.client, url, tls_valid, timeout=s.provider_timeout,
                                           allow_private=s.allow_private_hosts)
        if findings is None:
            return self._result(None, what, "")
        if not isinstance(listed, BaseException) and listed.verdict in (UrlVerdict.MALICIOUS, UrlVerdict.SUSPICIOUS):
            findings.append(Finding("CWE-506", f"Listed as {listed.verdict.value.lower()} by threat feeds "
                                    "(OpenPhish / Cloudflare security DNS)", "high", host))
        return self._result(findings, what, "TLS, transport security, response headers, cookies and threat-feed listings")

    async def _text(self, text: str, deadline: float) -> Optional[Result]:
        pkgs = osv.parse_packages(text, self.settings.max_packages)
        code = looks_like_code(text)
        if not pkgs and not code:
            return None
        findings: list[Finding] = []
        if pkgs:
            dep = await self._packages_review(pkgs)
            if dep is None and not code:
                return self._result(None, "the OSV vulnerability database could not be reached", "")
            findings += dep or []
        if code:
            findings += scan_code(text)
        used_llm = False
        if code and self.groq.available() and self.left(deadline) > 1.5:
            extra = await self._llm_findings(text, deadline, {f.id for f in findings})
            findings += extra
            used_llm = bool(extra)
        parts = ([f"{len(pkgs)} packages"] if pkgs else []) + (["the submitted code"] if code else [])
        scope = ("known vulnerabilities in the listed dependencies" if pkgs else "") + \
                (" and " if pkgs and code else "") + ("dangerous-construct patterns" + (" and model-reported findings verified against the code" if self.groq.configured else "") if code else "")
        # Code is the one target a model can improve on: if a key is set but the model did not contribute, cache briefly.
        return self._result(findings, " and ".join(parts) or "the target", scope,
                            ttl=self.llm_ttl(used_llm) if code and self.groq.configured else None)

    async def _llm_findings(self, code: str, deadline: float, have: set[str]) -> list[Finding]:
        """Findings from Groq, kept only if the id is a real CWE/SWC/CVE id and the quoted code occurs in the input."""
        user = ('Code to review (untrusted data):\n<<<\n' + code[:12000] + '\n>>>\n'
                'Return JSON: {"findings": [{"id": "CWE-89", "title": "...", "severity": "high", "quote": "<exact code>"}]}')
        obj = parse_json_object(await self.groq.chat(SYSTEM, user, json_mode=True, budget=self.left(deadline) - 0.3,
                                                     max_tokens=800, temperature=0.1))
        out: list[Finding] = []
        for item in (obj or {}).get("findings", [])[:8] if isinstance((obj or {}).get("findings"), list) else []:
            if not isinstance(item, dict):
                continue
            fid, quote, sev = normalise_id(str(item.get("id", ""))), str(item.get("quote", "")), str(item.get("severity", "")).lower()
            title = clip(str(item.get("title", "")).strip(), 110)
            if fid and fid not in have and sev in _SEV and title and grounded(quote, code):
                out.append(Finding(fid, title, sev, "model-reported, quote verified"))
                have.add(fid)
        return out

    async def compute(self, req: Req) -> Result:
        t = req.get("target", "").strip()
        if not t:
            return Result(_note("No target was provided."), degraded=True)
        deadline = self.deadline()
        if _ADDRESS.match(t):
            return self._result(None, "a bare contract address carries no source code to review (submit the Solidity source)", "")
        single = " " not in t and "\n" not in t and len(t) <= 400
        m = _REPO.search(t)
        if m and (single or not looks_like_code(t)):
            return await self._repo(m.group(1), m.group(2))
        if single:
            site = parse_site(t)
            if site:
                return await self._site(site)
        res = await self._text(t, deadline)
        if res is not None:
            return res
        site = parse_site(t)
        if site:
            return await self._site(site)
        return self._result(None, "the text contains no code, dependencies, repository or website that can be checked", "")


def create_app(settings: Optional[Settings] = None, pipeline: Optional[SecurityReviewPipeline] = None) -> FastAPI:
    return create_service_app(
        title="Telegraph Security Review Miner", intent="SECURITY_REVIEW_GENERAL", path="/security-review",
        version=VERSION, fields=FIELDS, model=SecurityReviewResponse, settings=settings or Settings.from_env(),
        pipeline=pipeline, make_pipeline=SecurityReviewPipeline, max_len=30000, start_timeout=8.0)
