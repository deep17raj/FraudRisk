"""Security review of a website: TLS validity, transport security and the common hardening headers.

Only what one safe GET can show is checked. Each missing protection is a weakness (CWE / OWASP A05 Security
Misconfiguration), not an exploit; severities are deliberately modest except for broken or absent TLS.
"""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse

import httpx

from intentkit.safe_fetch import FetchBlocked, Page, fetch_page

from .findings import Finding

_VERSIONED = re.compile(r"\d+\.\d+")


def parse_site(target: str) -> Optional[str]:
    """A bare host or URL -> an https URL to test; None when the text names no site."""
    t = target.strip().strip("<>\"' ")
    m = re.search(r"https?://[^\s<>\"']+", t)
    if m:
        return m.group(0).rstrip(".,;)")
    if re.fullmatch(r"(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?::\d+)?(?:/\S*)?", t):
        return "https://" + t
    return None


def header_findings(page: Page, https: bool) -> list[Finding]:
    h, out = page.headers, []
    host = urlparse(page.url).hostname or ""
    if https and "strict-transport-security" not in h:
        out.append(Finding("CWE-319", "No HSTS header: connections can be downgraded to plain HTTP", "medium", host))
    csp = h.get("content-security-policy", "")
    if not csp:
        out.append(Finding("OWASP-A05:2021", "No Content-Security-Policy header (weaker XSS containment)", "low", host))
    if "x-frame-options" not in h and "frame-ancestors" not in csp:
        out.append(Finding("CWE-1021", "Clickjacking: no X-Frame-Options or CSP frame-ancestors", "low", host))
    if h.get("x-content-type-options", "").lower() != "nosniff":
        out.append(Finding("CWE-693", "Missing X-Content-Type-Options: nosniff", "low", host))
    for c in page.cookies:
        name = c.split("=", 1)[0].strip()
        low = c.lower()
        if https and "secure" not in low:
            out.append(Finding("CWE-614", "Cookie set without the Secure attribute", "medium", name))
        if "httponly" not in low:
            out.append(Finding("CWE-1004", "Cookie set without the HttpOnly attribute", "low", name))
    server = h.get("server", "") + " " + h.get("x-powered-by", "")
    if _VERSIONED.search(server):
        out.append(Finding("CWE-200", f"Server software and version disclosed ({server.strip()[:60]})", "low", host))
    return out


async def review_site(client: httpx.AsyncClient, url: str, tls_valid: Optional[bool], *, timeout: float,
                      allow_private: bool) -> tuple[Optional[list[Finding]], str]:
    """(findings, what was examined). findings is None when the site could not be examined at all."""
    parsed = urlparse(url)
    host = parsed.hostname or url
    findings: list[Finding] = []
    if tls_valid is False:
        findings.append(Finding("CWE-295", "TLS certificate is invalid, expired or untrusted", "high", host))
    https_url = url if parsed.scheme == "https" else f"https://{host}{parsed.path or '/'}"
    page: Optional[Page] = None
    try:
        page = await fetch_page(client, https_url, timeout=timeout, allow_private=allow_private)
    except FetchBlocked:
        return None, "a site that may not be fetched"
    except httpx.HTTPError:
        page = None
    if page is None:
        if tls_valid is False:
            return findings, f"the TLS certificate of {host} (the page itself could not be fetched over HTTPS)"
        try:                                                  # HTTPS failed outright: is there plain HTTP at all?
            plain = await fetch_page(client, f"http://{host}{parsed.path or '/'}", timeout=timeout, allow_private=allow_private)
        except (FetchBlocked, httpx.HTTPError):
            return None, f"{host} (unreachable)"
        findings.append(Finding("CWE-319", "Site is served over plain HTTP only (no working HTTPS)", "high", host))
        return findings + header_findings(plain, https=False), f"{host} over HTTP"
    final_https = page.url.startswith("https://")
    if not final_https:
        findings.append(Finding("CWE-319", "HTTPS request ends on a plain-HTTP URL", "high", host))
    return findings + header_findings(page, https=final_https), f"{host} (TLS, HTTP headers, cookies)"
