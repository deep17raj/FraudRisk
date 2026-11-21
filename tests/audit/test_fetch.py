"""Input resolution: is this a link or a contract, is the link safe, and what if it fails.

The SSRF tests use real hostnames that resolve to real non-public addresses, so they check
the guard rather than a mock of it. The transport tests stub `_assert_public_host`, because
their subject is the fetch and extraction logic, not DNS.
"""
from __future__ import annotations

import httpx
import pytest

from audit import fetch as fetch_mod
from audit.fetch import (FetchBlocked, _assert_public_host, fetch_document, html_to_text,
                         looks_like_url, normalize_url, resolve_input)
from audit.models import Verdict
from tests.audit.conftest import extraction, make_pipeline, make_settings

CONTRACT_HTML = """<html><head><title>Agreement</title><style>b{}</style></head><body>
<h2>4.1 Delivery</h2><p>The Supplier <b>shall</b> deliver the Goods by 1&nbsp;March 2026.</p>
<script>tracker()</script><li>4.2 The Buyer shall pay &pound;10,000.</li></body></html>"""


# --- is it a link? ----------------------------------------------------------------------

@pytest.mark.parametrize("value", [
    "https://example.com/contract.pdf",
    "http://example.com/c",
    "example.com/contract.html",
    "www.legislation.gov.uk/ukpga/1979/54",
    "  https://example.com/a?b=c#d  ",
])
def test_links_are_recognised(value):
    assert looks_like_url(value)


@pytest.mark.parametrize("value", [
    "",
    "   ",
    "The Supplier shall deliver the Goods by 1 March 2026.",
    "See https://example.com/terms for the full terms.",   # text that mentions a link
    "Section 4.1 Delivery",
    "4.1.Delivery",                                        # a clause reference, not a host
    "Payment due in 30 days; paid on day 45.",
    "ftp://example.com/contract.txt",                      # not a scheme we fetch
    "mailto:legal@example.com",
])
def test_contract_text_is_not_mistaken_for_a_link(value):
    assert not looks_like_url(value)


def test_a_scheme_is_added_when_missing():
    assert normalize_url("example.com/c") == "https://example.com/c"
    assert normalize_url("http://example.com/c") == "http://example.com/c"


# --- the SSRF guard ---------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://localhost/contract",
    "http://127.0.0.1/contract",
    "http://169.254.169.254/latest/meta-data/",   # cloud metadata
    "http://10.0.0.5/contract",
    "http://192.168.1.1/contract",
    "http://[::1]/contract",
])
def test_non_public_addresses_are_refused(url):
    with pytest.raises(FetchBlocked):
        _assert_public_host(url, allow_private=False)


@pytest.mark.parametrize("url", ["ftp://example.com/x", "file:///etc/passwd",
                                 "gopher://example.com/"])
def test_only_http_and_https_are_fetched(url):
    with pytest.raises(FetchBlocked):
        _assert_public_host(url, allow_private=False)


def test_a_host_that_does_not_resolve_is_refused():
    with pytest.raises(FetchBlocked):
        _assert_public_host("https://nonexistent.invalid/contract", allow_private=False)


def test_the_guard_can_be_opened_deliberately_for_local_testing():
    _assert_public_host("http://127.0.0.1/contract", allow_private=True)


# --- extraction -------------------------------------------------------------------------

def test_html_becomes_readable_contract_text():
    text = html_to_text(CONTRACT_HTML)
    assert "4.1 Delivery" in text and "The Supplier shall deliver the Goods" in text
    assert "tracker()" not in text and "<" not in text
    assert "£10,000" in text, "entities must be decoded"


def test_html_keeps_the_line_structure_clause_numbering_needs():
    text = html_to_text(CONTRACT_HTML)
    assert "\n" in text
    assert text.count("\n\n\n") == 0, "runs of blank lines are collapsed"


def test_a_pdf_without_pypdf_or_a_broken_pdf_returns_nothing_rather_than_guessing():
    assert fetch_mod.pdf_to_text(b"%PDF-1.4 this is not a real pdf") == ""


# --- fetching ---------------------------------------------------------------------------

@pytest.fixture
def no_dns_check(monkeypatch):
    monkeypatch.setattr(fetch_mod, "_assert_public_host", lambda url, **kw: None)


def transport(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


async def test_an_html_contract_is_fetched_and_flattened(no_dns_check):
    async with transport(lambda r: httpx.Response(
            200, text=CONTRACT_HTML, headers={"content-type": "text/html"})) as c:
        doc = await fetch_document("https://example.com/c", c, make_settings())
    assert doc.origin == "url" and not doc.fetch_error
    assert "The Supplier shall deliver the Goods" in doc.text


async def test_plain_text_is_taken_as_is(no_dns_check):
    body = "1.1 The Supplier shall deliver the Goods by 1 March 2026."
    async with transport(lambda r: httpx.Response(
            200, text=body, headers={"content-type": "text/plain"})) as c:
        doc = await fetch_document("https://example.com/c.txt", c, make_settings())
    assert doc.text.strip() == body


async def test_an_http_error_becomes_an_explained_failure(no_dns_check):
    async with transport(lambda r: httpx.Response(404, text="nope")) as c:
        doc = await fetch_document("https://example.com/missing", c, make_settings())
    assert not doc.text and "HTTP 404" in doc.fetch_error


async def test_an_empty_document_becomes_an_explained_failure(no_dns_check):
    async with transport(lambda r: httpx.Response(
            200, text="   ", headers={"content-type": "text/plain"})) as c:
        doc = await fetch_document("https://example.com/empty", c, make_settings())
    assert not doc.text and "empty" in doc.fetch_error


async def test_a_scanned_pdf_with_no_text_layer_says_so(no_dns_check):
    async with transport(lambda r: httpx.Response(
            200, content=b"%PDF-1.4\nnot extractable",
            headers={"content-type": "application/pdf"})) as c:
        doc = await fetch_document("https://example.com/scan.pdf", c, make_settings())
    assert not doc.text and "no extractable text" in doc.fetch_error


async def test_a_connection_failure_becomes_an_explained_failure(no_dns_check):
    def boom(request):
        raise httpx.ConnectError("refused", request=request)

    async with transport(boom) as c:
        doc = await fetch_document("https://example.com/c", c, make_settings())
    assert not doc.text and doc.fetch_error


async def test_the_body_is_capped(no_dns_check):
    big = "The Supplier shall pay. " * 200_000
    async with transport(lambda r: httpx.Response(
            200, text=big, headers={"content-type": "text/plain"})) as c:
        doc = await fetch_document("https://example.com/big", c,
                                   make_settings(max_fetch_bytes=5_000))
    assert 0 < len(doc.text) < 50_000


async def test_a_redirect_is_followed_and_the_new_host_is_rechecked():
    """A public hostname may redirect to 127.0.0.1; the guard must run on every hop."""
    def handler(request):
        if "start" in str(request.url):
            return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})
        return httpx.Response(200, text="should never be reached")

    async with transport(handler) as c:
        doc = await fetch_document("https://example.com/start", c,
                                   make_settings(allow_private_hosts=False))
    assert not doc.text and "non-public" in doc.fetch_error


async def test_a_redirect_loop_is_refused(no_dns_check):
    async with transport(lambda r: httpx.Response(
            302, headers={"location": "https://example.com/again"})) as c:
        doc = await fetch_document("https://example.com/start", c,
                                   make_settings(max_redirects=2))
    assert not doc.text and "redirect" in doc.fetch_error


async def test_a_slow_document_times_out_rather_than_hanging(no_dns_check):
    async def slow(request):
        import asyncio
        await asyncio.sleep(5)
        return httpx.Response(200, text="too late")

    async with transport(slow) as c:
        doc = await fetch_document("https://example.com/slow", c,
                                   make_settings(fetch_timeout=0.2))
    assert not doc.text and "did not download" in doc.fetch_error


# --- through the pipeline ---------------------------------------------------------------

async def test_raw_text_input_is_not_fetched():
    doc = await resolve_input("1.1 The Supplier shall deliver.", None, make_settings())
    assert doc.origin == "text" and doc.text.startswith("1.1")


async def test_a_link_with_fetching_disabled_explains_itself():
    doc = await resolve_input("https://example.com/c", None,
                              make_settings(enable_url_fetch=False))
    assert doc.origin == "url" and "disabled" in doc.fetch_error


async def test_an_unfetchable_link_yields_unclear_explaining_why():
    """The honest answer: we could not read the document, so we assert no verdict."""
    settings = make_settings(enable_url_fetch=True)
    pl = make_pipeline(extraction([]), settings=settings)
    pl.client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(403, text="denied")),
        follow_redirects=False)
    fetch_mod._assert_public_host  # guard is exercised in its own tests
    try:
        res = await pl.audit("https://example.com/private-contract")
    finally:
        await pl.client.aclose()
    assert res.verdict is Verdict.UNCLEAR
    assert res.obligations == [] and res.clauses == []
    assert "could not be read" in res.summary


async def test_a_fetched_contract_is_audited_with_verbatim_citations(no_dns_check):
    settings = make_settings(enable_url_fetch=True)
    payload = extraction([
        {"party": "The Supplier", "duty": "deliver the Goods", "status": "met",
         "deadline": "1 March 2026",
         "quote": "The Supplier shall deliver the Goods by 1 March 2026"},
    ])
    pl = make_pipeline(payload, settings=settings)
    pl.client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(
            200, text=CONTRACT_HTML, headers={"content-type": "text/html"})),
        follow_redirects=False)
    try:
        res = await pl.audit("https://example.com/contract")
    finally:
        await pl.client.aclose()
    assert res.clauses, "a fetched contract must still produce citations"
    flattened = html_to_text(CONTRACT_HTML)
    for clause in res.clauses:
        assert clause in flattened, "citations must be verbatim against the fetched document"
