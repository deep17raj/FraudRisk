"""Stage 1 - input resolution: `contract` is either the text itself or a link to it.

Telegraph encodes the `contract` request field on-chain in at most 256 bytes, so a routed
request very often carries a URL rather than a document. Resolving one is therefore part of
the core path, not a nicety.

Fetching a URL that a stranger supplies is an SSRF sink, so every hop is checked: http/https
only, every resolved address must be public, the check is repeated on each redirect (a public
hostname can redirect to 127.0.0.1 or to a cloud metadata endpoint), and the body is streamed
under a hard byte cap. A fetch that fails for any reason - blocked, slow, empty, unreadable -
returns a `SourceDoc` carrying the reason, and the pipeline answers `Unclear` and says why.
"""
from __future__ import annotations

import asyncio
import html
import io
import ipaddress
import logging
import re
import socket
from urllib.parse import urljoin, urlparse

import httpx

from .config import Settings
from .models import SourceDoc

log = logging.getLogger("audit.fetch")

#: The whole input must be one URL-ish token; a contract that merely *mentions* a URL is text.
_URL_RE = re.compile(
    r"^(?:(?P<scheme>https?)://)?"
    r"(?P<host>[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+)"
    r"(?::\d{1,5})?"
    r"(?P<rest>[/?#][^\s]*)?$"
)
#: A bare "foo.bar" is only a link if the suffix looks like a real TLD, not "Section 4.2".
_TLD_RE = re.compile(r"\.[A-Za-z]{2,24}$")

_SCRIPTISH = re.compile(r"(?is)<(script|style|head|noscript|svg|template)\b.*?</\1\s*>")
_COMMENT = re.compile(r"(?s)<!--.*?-->")
_BLOCK_END = re.compile(
    r"(?i)</(?:p|div|li|tr|h[1-6]|section|article|blockquote|td|th|table|ul|ol|pre)\s*>"
    r"|<br\s*/?>|<hr\s*/?>"
)
_TAG = re.compile(r"(?s)<[^>]+>")


def looks_like_url(value: str) -> bool:
    """True when the input as a whole is a link, rather than contract text."""
    s = (value or "").strip().strip("<>\"'")
    if not s or len(s.split()) != 1 or len(s) > 2048:
        return False
    m = _URL_RE.match(s)
    if not m:
        return False
    if m.group("scheme"):
        return True
    # No scheme, so this has to be judged on shape alone. Insist on a plausible TLD and on a
    # first label that is not purely numeric, or a bare clause reference such as
    # "4.1.Delivery" would be fetched as a host instead of audited as text.
    host = m.group("host")
    if host.split(".")[0].isdigit() and not host.replace(".", "").isdigit():
        return False
    return bool(_TLD_RE.search(host))


def normalize_url(value: str) -> str:
    s = (value or "").strip().strip("<>\"'")
    return s if "://" in s else f"https://{s}"


class FetchBlocked(Exception):
    """The URL is not one this service is willing to fetch."""


def _assert_public_host(url: str, *, allow_private: bool) -> None:
    """Resolve the host and refuse anything that is not a public address."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise FetchBlocked(f"unsupported scheme {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise FetchBlocked("no host in URL")
    if allow_private:
        return
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise FetchBlocked(f"host does not resolve ({exc.strerror or exc})") from exc
    if not infos:
        raise FetchBlocked("host does not resolve")
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast
                or addr.is_reserved or addr.is_unspecified):
            raise FetchBlocked(f"{host} resolves to the non-public address {addr}")


def html_to_text(raw: str) -> str:
    """Flatten HTML to text, keeping the line structure clause numbering depends on."""
    s = _COMMENT.sub(" ", _SCRIPTISH.sub(" ", raw))
    s = _BLOCK_END.sub("\n", s)
    s = _TAG.sub(" ", s)
    s = html.unescape(s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"[ \t ]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def pdf_to_text(data: bytes, max_pages: int = 60) -> str:
    """Extract text from a PDF. Returns "" when pypdf is absent or the file is unreadable."""
    try:
        from pypdf import PdfReader
    except ImportError:
        log.warning("pypdf not installed; cannot read PDF contracts",
                    extra={"fields": {"event": "pdf_no_pypdf"}})
        return ""
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = reader.pages[:max_pages]
        return re.sub(r"\n{3,}", "\n\n", "\n\n".join(p.extract_text() or "" for p in pages)).strip()
    except Exception as exc:
        log.warning("PDF extraction failed", extra={"fields": {
            "event": "pdf_failed", "error": f"{type(exc).__name__}: {exc}"}})
        return ""


async def _get(client: httpx.AsyncClient, url: str, settings: Settings) -> tuple[bytes, str, str]:
    """Fetch one URL, following redirects ourselves so each hop is re-checked.

    Returns (body, content_type, final_url). Raises FetchBlocked or an httpx error.
    """
    current = url
    for _ in range(max(1, settings.max_redirects) + 1):
        _assert_public_host(current, allow_private=settings.allow_private_hosts)
        async with client.stream(
            "GET", current, follow_redirects=False,
            headers={"Accept": "text/html,application/pdf,text/plain,*/*"},
        ) as resp:
            if resp.is_redirect:
                location = resp.headers.get("location")
                if not location:
                    raise FetchBlocked(f"HTTP {resp.status_code} redirect without a location")
                current = urljoin(current, location)
                continue
            resp.raise_for_status()
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes():
                # Trim the chunk to what is left of the budget rather than appending it
                # whole and checking afterwards: a server that sends the body in one large
                # chunk would otherwise slip past the cap entirely. Cutting mid-character
                # is safe because the decode below replaces malformed bytes.
                room = settings.max_fetch_bytes - total
                if room <= 0:
                    break
                chunk = chunk[:room]
                chunks.append(chunk)
                total += len(chunk)
            return b"".join(chunks), resp.headers.get("content-type", ""), str(resp.url)
    raise FetchBlocked(f"more than {settings.max_redirects} redirects")


async def fetch_document(url: str, client: httpx.AsyncClient, settings: Settings) -> SourceDoc:
    """Resolve a link to contract text, or to a SourceDoc explaining why it could not be."""
    try:
        body, ctype, final_url = await asyncio.wait_for(
            _get(client, url, settings), timeout=settings.fetch_timeout)
    except FetchBlocked as exc:
        return SourceDoc(text="", origin="url", url=url, fetch_error=str(exc))
    except asyncio.TimeoutError:
        return SourceDoc(text="", origin="url", url=url,
                         fetch_error=f"the document did not download within {settings.fetch_timeout:.0f}s")
    except httpx.HTTPStatusError as exc:
        return SourceDoc(text="", origin="url", url=url,
                         fetch_error=f"the server answered HTTP {exc.response.status_code}")
    except Exception as exc:
        return SourceDoc(text="", origin="url", url=url,
                         fetch_error=f"the document could not be retrieved ({type(exc).__name__})")

    ctype = (ctype or "").lower()
    looks_pdf = "pdf" in ctype or body[:5] == b"%PDF-"
    if looks_pdf:
        text = pdf_to_text(body)
        if not text.strip():
            return SourceDoc(text="", origin="url", url=final_url,
                             fetch_error="the linked PDF contains no extractable text "
                                         "(it may be a scan)")
    else:
        try:
            decoded = body.decode("utf-8", errors="replace")
        except Exception:
            decoded = body.decode("latin-1", errors="replace")
        text = html_to_text(decoded) if ("html" in ctype or "<" in decoded[:2000]) else decoded.strip()

    if not text.strip():
        return SourceDoc(text="", origin="url", url=final_url,
                         fetch_error="the linked document was empty")
    return SourceDoc(text=text, origin="url", url=final_url)


async def resolve_input(contract: str, client: httpx.AsyncClient | None,
                        settings: Settings) -> SourceDoc:
    """Turn the `contract` request field into the document to audit."""
    raw = (contract or "").strip()
    if not raw:
        return SourceDoc(text="", origin="empty")

    if looks_like_url(raw):
        if not settings.enable_url_fetch or client is None:
            return SourceDoc(text="", origin="url", url=raw,
                             fetch_error="this deployment has link fetching disabled")
        doc = await fetch_document(normalize_url(raw), client, settings)
    else:
        doc = SourceDoc(text=raw, origin="text")

    if len(doc.text) > settings.max_contract_chars:
        doc.text = doc.text[: settings.max_contract_chars]
        doc.truncated = True
    return doc
