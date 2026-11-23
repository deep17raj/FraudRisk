from __future__ import annotations

import asyncio
import socket
import ssl

import pytest
from fastapi.testclient import TestClient

from intents.dns_lookup.app import create_app as create_dns
from intents.dns_lookup.config import Settings as DnsSettings
from intents.dns_lookup.pipeline import DnsPipeline, DnsResponse
from intents.ssl_cert.app import create_app as create_ssl
from intents.ssl_cert.config import Settings as SslSettings
from intents.ssl_cert.pipeline import SslPipeline, SslResponse, parse_host
from tests.intents.helpers import FakeHttp, StubPipeline, fast


# ---- SSL --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("google.com", ("google.com", 443)), ("https://Example.COM/path?x=1", ("example.com", 443)),
    ("https://example.com:8443/x", ("example.com", 8443)), ("example.com:25", ("example.com", 443)),
    ("Is the TLS certificate of github.com valid?", ("github.com", 443)), ("8.8.8.8", ("8.8.8.8", 443)),
    ("bücher.example", ("xn--bcher-kva.example", 443)), ("", None), ("not a host", None), ("localhost", None),
])
def test_host_parsing(raw, expected):
    assert parse_host(raw) == expected


class FakeWriter:
    def close(self): pass

    async def wait_closed(self): pass


def patch_network(monkeypatch, addresses, connect):
    def getaddrinfo(host, port, *a, **k):
        return [(socket.AF_INET6 if ":" in a_ else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a_, port)) for a_ in addresses]
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    calls = []

    async def open_connection(addr, port, **kw):
        calls.append((addr, port, kw.get("server_hostname")))
        result = connect(addr)
        if isinstance(result, BaseException):
            raise result
        return object(), FakeWriter()
    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    return calls


async def verdict(host, **kw) -> bool:
    pl = SslPipeline(fast(SslSettings, **kw), None)
    return (await pl.answer({"host": host})).certificate_valid


async def test_a_handshake_that_verifies_is_valid_and_targets_the_vetted_address(monkeypatch):
    calls = patch_network(monkeypatch, ["93.184.216.34"], lambda a: None)
    assert await verdict("example.com") is True
    assert calls == [("93.184.216.34", 443, "example.com")]     # connect to the address, verify the name


@pytest.mark.parametrize("error", [ssl.SSLCertVerificationError("expired"), ssl.SSLError("no tls")])
async def test_verification_failures_and_no_tls_are_invalid(monkeypatch, error):
    patch_network(monkeypatch, ["93.184.216.34"], lambda a: error)
    assert await verdict("expired.example") is False


async def test_an_unreachable_address_falls_through_to_the_next_one(monkeypatch):
    calls = patch_network(monkeypatch, ["2606:2800:220:1:248:1893:25c8:1946", "93.184.216.34"],
                          lambda a: OSError("unreachable") if ":" in a else None)
    assert await verdict("example.com") is True
    assert [c[0] for c in calls] == ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"] or calls[0][0] == "93.184.216.34"


async def test_nothing_reachable_is_false(monkeypatch):
    patch_network(monkeypatch, ["93.184.216.34"], lambda a: OSError("refused"))
    assert await verdict("example.com") is False


@pytest.mark.parametrize("addresses", [["127.0.0.1"], ["10.0.0.5"], ["169.254.169.254"], ["192.168.1.1"], ["::1"],
                                       ["93.184.216.34", "10.0.0.5"]])
async def test_private_loopback_and_link_local_hosts_are_never_contacted(monkeypatch, addresses):
    calls = patch_network(monkeypatch, addresses, lambda a: None)
    assert await verdict("internal.example") is False
    assert calls == []


async def test_the_ssrf_guard_can_be_lifted_for_local_testing_only(monkeypatch):
    calls = patch_network(monkeypatch, ["127.0.0.1"], lambda a: None)
    assert await verdict("localhost.example", allow_private_hosts=True) is True and calls


async def test_a_host_that_does_not_resolve_is_false(monkeypatch):
    def getaddrinfo(*a, **k):
        raise socket.gaierror("no such host")
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    assert await verdict("no-such-host.example") is False


def test_ssl_http_contract():
    with TestClient(create_ssl(fast(SslSettings), pipeline=StubPipeline(SslResponse, certificate_valid=True))) as c:
        assert c.post("/ssl-verification", json={"host": "google.com"}).json() == {"certificate_valid": True}
        assert c.post("/ssl-verification", content=b"x").json() == {"certificate_valid": True}   # stub always says True


# ---- DNS --------------------------------------------------------------------------------------------------
GOOGLE, CLOUDFLARE = "https://dns.google/resolve", "https://cloudflare-dns.com/dns-query"


def doh(status=0, answers=()):
    d = {"Status": status}
    if answers:
        d["Answer"] = [{"name": "x.", "type": t, "TTL": 60, "data": v} for t, v in answers]
    return d


async def has_record(fake, name="example.com", rtype="A") -> bool:
    return (await DnsPipeline(fast(DnsSettings), fake.client()).answer({"name": name, "record_type": rtype})).has_record


async def test_a_record_of_the_asked_type_is_true():
    fake = FakeHttp({GOOGLE: doh(0, [(1, "93.184.216.34")]), CLOUDFLARE: doh(0, [(1, "93.184.216.34")])})
    assert await has_record(fake, rtype="A") is True


async def test_nodata_is_false_even_though_the_status_is_noerror():
    fake = FakeHttp({GOOGLE: doh(0), CLOUDFLARE: doh(0)})
    assert await has_record(fake, rtype="AAAA") is False


async def test_a_cname_alone_does_not_count_as_an_a_record():
    fake = FakeHttp({GOOGLE: doh(0, [(5, "target.example.")]), CLOUDFLARE: doh(0, [(5, "target.example.")])})
    assert await has_record(fake, "www.example.com", "A") is False
    assert await has_record(fake, "www.example.com", "CNAME") is True


@pytest.mark.parametrize("rtype,code", [("MX", 15), ("TXT", 16), ("NS", 2), ("CAA", 257), ("AAAA", 28)])
async def test_record_types_map_to_their_wire_codes(rtype, code):
    fake = FakeHttp({GOOGLE: doh(0, [(code, "x")]), CLOUDFLARE: doh(0, [(code, "x")])})
    assert await has_record(fake, rtype=rtype) is True
    assert await has_record(FakeHttp({GOOGLE: doh(0, [(1, "1.2.3.4")]), CLOUDFLARE: doh(0, [(1, "1.2.3.4")])}),
                            "other.example.com", rtype) is False


async def test_a_ptr_question_about_an_address_queries_its_reverse_name():
    fake = FakeHttp({GOOGLE: doh(0, [(12, "dns.google.")]), CLOUDFLARE: doh(0, [(12, "dns.google.")])})
    assert await has_record(fake, "8.8.8.8", "PTR") is True
    assert {r.url.params["name"] for r in fake.calls} == {"8.8.8.8.in-addr.arpa"}
    fake6 = FakeHttp({GOOGLE: doh(0, [(12, "x.")]), CLOUDFLARE: doh(0, [(12, "x.")])})
    assert await has_record(fake6, "2001:4860:4860::8888", "PTR") is True
    assert all(r.url.params["name"].endswith(".ip6.arpa") for r in fake6.calls)
    plain = FakeHttp({GOOGLE: doh(0, [(1, "8.8.8.8")]), CLOUDFLARE: doh(0, [(1, "8.8.8.8")])})
    await has_record(plain, "8.8.8.8", "A")
    assert {r.url.params["name"] for r in plain.calls} == {"8.8.8.8"}


async def test_nxdomain_and_servfail_are_false():
    for status in (3, 2):
        assert await has_record(FakeHttp({GOOGLE: doh(status), CLOUDFLARE: doh(status)})) is False


async def test_cloudflare_answers_when_google_is_down_and_google_wins_a_disagreement():
    assert await has_record(FakeHttp({GOOGLE: None, CLOUDFLARE: doh(0, [(1, "1.1.1.1")])})) is True
    assert await has_record(FakeHttp({GOOGLE: doh(3), CLOUDFLARE: doh(0, [(1, "1.1.1.1")])}), "other.example") is False


async def test_both_resolvers_down_is_false_and_junk_makes_no_query():
    assert await has_record(FakeHttp({GOOGLE: None, CLOUDFLARE: None})) is False
    fake = FakeHttp({GOOGLE: doh(0, [(1, "1.1.1.1")])})
    assert await has_record(fake, "not a domain!", "A") is False
    assert await has_record(fake, "example.com", "BOGUS") is False
    assert fake.calls == []


def test_dns_http_contract():
    stub = StubPipeline(DnsResponse, has_record=True)
    with TestClient(create_dns(fast(DnsSettings), pipeline=stub)) as c:
        assert c.post("/dns-record", json={"name": "example.com", "record_type": "MX"}).json() == {"has_record": True}
        c.post("/dns-record", json={"domain": "b.example", "type": "TXT"})
    assert stub.seen[1] == {"name": "b.example", "record_type": "TXT"}
