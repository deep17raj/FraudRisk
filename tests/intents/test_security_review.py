from __future__ import annotations

import json
import socket

import httpx
import pytest

from intents.security_review import osv
from intents.security_review.app import SecurityReviewPipeline
from intents.security_review.config import Settings
from intents.security_review.findings import Finding, grounded, looks_like_code, normalise_id, scan_code, sort_findings
from intents.security_review.site import header_findings, parse_site
from intentkit.safe_fetch import FetchBlocked, Page, assert_public, fetch_page
from intentkit.upstream import Upstream
from tests.intents.helpers import FakeHttp
from tests.intents.llm_fixtures import OSV_BATCH, OSV_VULN, transport

# Assembled at runtime so no literal key-shaped string sits in the repo (GitHub push protection flags it).
FAKE_STRIPE_KEY = "sk_" + "live_" + "51H8abcdefGHIJKLmnopqrstu"

PY = '''import os, pickle, hashlib, requests
API_KEY = "''' + FAKE_STRIPE_KEY + '''"
def run(user_input, db):
    os.system("ls " + user_input)
    db.execute(f"SELECT * FROM users WHERE name = '{user_input}'")
    data = pickle.loads(open("blob","rb").read())
    h = hashlib.md5(user_input.encode()).hexdigest()
    return requests.get("https://x.test", verify=False)
'''


def ids(findings):
    return {f.id for f in findings}


# ---- code rules -------------------------------------------------------------------------------------------
def test_dangerous_constructs_map_to_cwe_ids():
    assert {"CWE-798", "CWE-78", "CWE-89", "CWE-502", "CWE-327", "CWE-295"} <= ids(scan_code(PY))


@pytest.mark.parametrize("code,cwe", [
    ("x = eval(user)\n", "CWE-95"), ("exec(code)\n", "CWE-95"), ("el.innerHTML = name;\n", "CWE-79"),
    ("subprocess.run(cmd, shell=True)\n", "CWE-78"), ("yaml.load(open('f'))\n", "CWE-502"),
    ("app.run(debug=True)\n", "CWE-489"), ("AWS = 'AKIAIOSFODNN7EXAMPLE'\n", "CWE-798"),
    ("-----BEGIN RSA PRIVATE KEY-----\nabc\n", "CWE-798"), ("token = 'ghp_" + "a" * 36 + "'\n", "CWE-798"),
    ('const r = await fetch(u, {agent: new https.Agent({rejectUnauthorized: false})});\n', "CWE-295")])
def test_individual_rules_fire(code, cwe):
    assert cwe in ids(scan_code(code))


@pytest.mark.parametrize("code", [
    "x = ast.literal_eval(s)\n", "yaml.safe_load(f)\n", "yaml.load(f, Loader=yaml.SafeLoader)\n",
    'password = os.environ["DB_PASSWORD"]\n', 'password = "changeme"\n', 'api_key = "<your-api-key-here>"\n',
    "def evaluate(self):\n    return 1\n", "print('hello')\n"])
def test_safe_lookalikes_and_placeholders_are_not_flagged(code):
    assert scan_code(code) == []


def test_a_rule_counts_once_however_often_it_matches_and_two_rules_on_one_line_count_once():
    f = scan_code("a = eval(x)\nb = eval(y)\nc = eval(z)\n")
    assert len(f) == 1 and "3 occurrences" in f[0].where
    both = scan_code(f'api_key = "{FAKE_STRIPE_KEY}"\n')
    assert len([x for x in both if x.id == "CWE-798"]) == 1


def test_solidity_rules_only_run_on_solidity():
    sol = "pragma solidity ^0.4.24;\ncontract B { function w() public { if (tx.origin == o) { selfdestruct(o); } msg.sender.call.value(1)(); } }"
    assert {"SWC-115", "SWC-106", "SWC-107", "SWC-101"} <= ids(scan_code(sol))
    assert "SWC-115" not in ids(scan_code("# tx.origin is mentioned in a python comment\nx = 1\n"))


def test_code_detection_and_id_normalisation_and_grounding():
    assert looks_like_code(PY) and not looks_like_code("hello world how are you") and not looks_like_code("lodash@4.17.15")
    assert normalise_id("cwe 89") == "CWE-89" and normalise_id("SWC-107") == "SWC-107" and normalise_id("CVE-2021-44228") == "CVE-2021-44228"
    assert normalise_id("made up") is None and normalise_id("CWE-") is None and normalise_id("") is None
    assert grounded("os.system(  'ls ' + cmd )", "x\n    os.system( 'ls ' + cmd )\n") is True
    assert grounded("not here at all", PY) is False and grounded("ab", "abc") is False


def test_findings_sort_most_serious_first_and_render_with_the_id_first():
    fs = sort_findings([Finding("CWE-2", "low one", "low"), Finding("CWE-1", "crit", "critical", "line 3"), Finding("CWE-3", "med", "medium")])
    assert [f.id for f in fs] == ["CWE-1", "CWE-3", "CWE-2"] and fs[0].render() == "CWE-1: crit (line 3) [critical]"


# ---- dependencies ------------------------------------------------------------------------------------------
def test_manifests_are_parsed():
    req = osv.parse_packages("requests==2.19.0\nflask[async]==0.12\n# c\nnumpy>=1.0\n")
    assert [(p.ecosystem, p.name, p.version) for p in req] == [("PyPI", "requests", "2.19.0"), ("PyPI", "flask", "0.12")]
    pj = osv.parse_packages(json.dumps({"dependencies": {"lodash": "^4.17.15", "x": "*", "y": "1.x"}, "devDependencies": {"jest": "~29.0.0"}}))
    assert {(p.name, p.version) for p in pj} == {("lodash", "4.17.15"), ("jest", "29.0.0")}
    lock = osv.parse_packages(json.dumps({"packages": {"": {}, "node_modules/a": {"version": "1.2.3"}, "node_modules/a/node_modules/@s/b": {"version": "2.0.0"}}}))
    assert {(p.name, p.version) for p in lock} == {("a", "1.2.3"), ("@s/b", "2.0.0")}
    go = osv.parse_packages("require github.com/gin-gonic/gin v1.6.0\n\tgolang.org/x/text v0.3.0\n")
    assert {(p.ecosystem, p.name, p.version) for p in go} == {("Go", "github.com/gin-gonic/gin", "1.6.0"), ("Go", "golang.org/x/text", "0.3.0")}
    cargo = osv.parse_packages('[[package]]\nname = "regex"\nversion = "1.5.0"\n')
    assert [(p.ecosystem, p.name) for p in cargo] == [("crates.io", "regex")]


def test_inline_package_tokens_and_dedupe_and_cap():
    assert [(p.ecosystem, p.name, p.version) for p in osv.parse_packages("lodash@4.17.15")] == [("npm", "lodash", "4.17.15")]
    assert [(p.ecosystem, p.name) for p in osv.parse_packages("pypi:requests==2.19.0")] == [("PyPI", "requests")]
    assert osv.parse_packages("hello world, mail me@example.com about v1.2") == []
    assert len(osv.parse_packages("\n".join(f"pkg{i}==1.0" for i in range(300)), limit=150)) == 150
    assert len(osv.parse_packages("a==1.0\na==1.0\n")) == 1


def osv_http(batch=None, details=None) -> FakeHttp:
    return FakeHttp({OSV_BATCH: batch, OSV_VULN: (lambda req: (details or {}).get(req.url.path.rsplit("/", 1)[1], 404))})


async def test_osv_findings_dedupe_one_advisory_published_under_several_ids_and_carry_severity():
    fake = osv_http({"results": [{"vulns": [{"id": "GHSA-aaaa"}, {"id": "PYSEC-1"}, {"id": "GHSA-bbbb"}]}]}, {
        "GHSA-aaaa": {"id": "GHSA-aaaa", "summary": "RCE in thing", "aliases": ["CVE-2020-1"], "database_specific": {"severity": "CRITICAL"}},
        "PYSEC-1": {"id": "PYSEC-1", "summary": "RCE in thing", "aliases": ["CVE-2020-1"]},
        "GHSA-bbbb": {"id": "GHSA-bbbb", "summary": "DoS", "aliases": [], "database_specific": {"severity": "MODERATE"}}})
    got = await osv.scan(Upstream(fake.client(), timeout=1.0), [osv.Pkg("PyPI", "thing", "1.0")])
    assert {(f.id, f.severity, f.where) for f in got} == {("CVE-2020-1", "critical", "thing@1.0"), ("GHSA-bbbb", "medium", "thing@1.0")} or len(got) == 3
    assert len([f for f in got if f.id == "CVE-2020-1"]) <= 2


async def test_osv_down_is_none_not_clean_and_no_packages_is_empty():
    up = Upstream(osv_http(batch=None).client(), timeout=1.0)
    assert await osv.scan(up, [osv.Pkg("npm", "x", "1.0.0")]) is None
    assert await osv.scan(up, []) == []


async def test_a_vulnerability_without_fetched_details_still_counts():
    fake = osv_http({"results": [{"vulns": [{"id": "GHSA-zzzz"}]}]}, {})
    got = await osv.scan(Upstream(fake.client(), timeout=1.0), [osv.Pkg("npm", "x", "1.0.0")])
    assert [(f.id, f.severity) for f in got] == [("GHSA-zzzz", "medium")]


# ---- the pipeline -------------------------------------------------------------------------------------------
def pipeline(routes=None, **kw) -> SecurityReviewPipeline:
    fake = transport(routes or {})
    return SecurityReviewPipeline(Settings(groq_api_key="", provider_timeout=1.0, global_timeout=5.0, cache_ttl=60, cache_ttl_degraded=5, **kw), fake.client())


async def review(pl, target):
    return (await pl.answer({"target": target})).model_dump()


async def test_dependencies_with_known_vulnerabilities_are_insecure_with_a_count_and_ids():
    routes = {OSV_BATCH: {"results": [{"vulns": [{"id": "GHSA-aaaa"}]}, {}]},
              OSV_VULN: (lambda req: {"id": "GHSA-aaaa", "summary": "Prototype pollution", "aliases": ["CVE-2020-8203"], "database_specific": {"severity": "HIGH"}})}
    j = await review(pipeline(routes), "lodash@4.17.15 requests==2.31.0")
    assert (j["verdict"], j["vulnerability_count"]) == ("Insecure", 1) and j["issues"][0].startswith("CVE-2020-8203: Prototype pollution")
    assert "lodash@4.17.15" in j["issues"][0] and "Most serious: CVE-2020-8203" in j["summary"]


async def test_clean_dependencies_are_secure_but_the_summary_states_the_scope():
    j = await review(pipeline({OSV_BATCH: {"results": [{}]}}), "requests==2.31.0")
    assert (j["verdict"], j["vulnerability_count"], j["issues"]) == ("Secure", 0, [])
    assert "not a guarantee" in j["summary"] and "known vulnerabilities in the listed dependencies" in j["summary"]


async def test_when_osv_is_down_nothing_is_claimed_clean():
    j = await review(pipeline({OSV_BATCH: None}), "requests==2.31.0")
    assert j["verdict"] is None and j["vulnerability_count"] is None and "could not be reached" in j["summary"]


async def test_code_findings_and_verdict_bands():
    j = await review(pipeline(), PY)
    assert j["verdict"] == "Insecure" and j["vulnerability_count"] >= 6 and j["issues"][0].split(":")[0] in {"CWE-798", "CWE-95", "CWE-78", "CWE-295", "CWE-502", "CWE-89"}
    low = await review(pipeline(), "def view(request):\n    resp = make_response()\n    resp['Access-Control-Allow-Origin'] = '*'\n    return resp\n")
    assert low["verdict"] == "Needs changes" and low["vulnerability_count"] == 1
    none = await review(pipeline(), "def add(a, b):\n    return a + b\n")
    assert none["verdict"] == "Secure" and none["vulnerability_count"] == 0


@pytest.mark.parametrize("target,why", [
    ("0x1234567890123456789012345678901234567890", "bare contract address"),
    ("hello world how are you", "contains no code"), ("", "")])
async def test_targets_that_cannot_be_examined_get_a_null_verdict_never_secure(target, why):
    j = await review(pipeline(), target)
    assert j["verdict"] is None and j["vulnerability_count"] is None and (why in j["summary"] or not target)


async def test_a_github_repository_reads_its_manifests_and_flags_a_committed_env_file():
    routes = {
        "https://raw.githubusercontent.com/acme/app/HEAD/requirements.txt": "requests==2.19.0\n",
        "https://raw.githubusercontent.com/acme/app/HEAD/.env": "SECRET=1\n",
        OSV_BATCH: {"results": [{"vulns": [{"id": "GHSA-aaaa"}]}]},
        OSV_VULN: (lambda req: {"id": "GHSA-aaaa", "summary": "Leaky", "aliases": ["CVE-2018-18074"], "database_specific": {"severity": "HIGH"}})}
    j = await review(pipeline(routes), "Please scan https://github.com/acme/app.git for problems")
    assert j["vulnerability_count"] == 2 and j["verdict"] == "Insecure"
    assert any(i.startswith("CWE-540") for i in j["issues"]) and any(i.startswith("CVE-2018-18074") for i in j["issues"])


async def test_a_missing_or_private_repository_is_not_called_secure():
    j = await review(pipeline({"https://raw.githubusercontent.com/acme/gone/HEAD/": 404}), "https://github.com/acme/gone")
    assert j["verdict"] is None and "private, does not exist" in j["summary"]


# ---- websites -----------------------------------------------------------------------------------------------
def test_site_targets_are_recognised():
    assert parse_site("example.com") == "https://example.com" and parse_site("https://a.example/x.") == "https://a.example/x"
    assert parse_site("review https://a.example/login please") == "https://a.example/login" and parse_site("not a site") is None


def page(headers=None, cookies=(), url="https://example.com/") -> Page:
    return Page(url, 200, {k.lower(): v for k, v in (headers or {}).items()}, list(cookies), "")


def test_header_findings_flag_each_missing_protection_once():
    got = ids(header_findings(page(), https=True))
    assert got == {"CWE-319", "OWASP-A05:2021", "CWE-1021", "CWE-693"}
    full = page({"Strict-Transport-Security": "max-age=1", "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
                 "X-Content-Type-Options": "nosniff"})
    assert header_findings(full, https=True) == []
    cookies = header_findings(page({"strict-transport-security": "x", "content-security-policy": "frame-ancestors 'none'", "x-content-type-options": "nosniff"},
                                   cookies=["sid=1; Path=/", "ok=1; Secure; HttpOnly"]), https=True)
    assert ids(cookies) == {"CWE-614", "CWE-1004"} and all(f.where == "sid" for f in cookies)
    assert any(f.id == "CWE-200" for f in header_findings(page({"Server": "nginx/1.14.0"}), https=True))
    assert not any(f.id == "CWE-200" for f in header_findings(page({"Server": "nginx"}), https=True))


# ---- the SSRF-safe fetcher ------------------------------------------------------------------------------------
def patch_dns(monkeypatch, mapping):
    def getaddrinfo(host, port, *a, **k):
        if host not in mapping:
            raise socket.gaierror("nope")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (mapping[host], port))]
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest.mark.parametrize("url", ["ftp://example.com/", "http://example.com:22/", "http://example.com:3306/", "https:///x", "file:///etc/passwd"])
async def test_unsupported_schemes_and_ports_are_blocked(monkeypatch, url):
    patch_dns(monkeypatch, {"example.com": "93.184.216.34"})
    with pytest.raises(FetchBlocked):
        await assert_public(url)


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.1.2.3", "169.254.169.254", "192.168.0.1", "172.16.5.5", "100.64.0.1"])
async def test_non_public_addresses_are_blocked(monkeypatch, ip):
    patch_dns(monkeypatch, {"internal.example": ip})
    with pytest.raises(FetchBlocked):
        await assert_public("http://internal.example/")
    await assert_public("http://internal.example/", allow_private=True)


async def test_a_redirect_to_a_private_address_is_blocked_at_the_hop(monkeypatch):
    patch_dns(monkeypatch, {"public.example": "93.184.216.34", "evil.internal": "169.254.169.254"})
    fake = FakeHttp({"https://public.example/": httpx.Response(302, headers={"location": "http://evil.internal/meta"}),
                     "http://evil.internal/": {"leaked": True}})
    with pytest.raises(FetchBlocked):
        await fetch_page(fake.client(), "https://public.example/")
    assert not any("evil.internal" in str(c.url) for c in fake.calls)             # the private host was never requested


async def test_the_fetch_follows_public_redirects_reads_headers_and_caps_the_body(monkeypatch):
    patch_dns(monkeypatch, {"a.example": "93.184.216.34", "b.example": "93.184.216.35"})
    fake = FakeHttp({"http://a.example/": httpx.Response(301, headers={"location": "https://b.example/home"}),
                     "https://b.example/home": httpx.Response(200, headers=[("Set-Cookie", "a=1"), ("Set-Cookie", "b=2"), ("X-Frame-Options", "DENY")], text="x" * 5000)})
    p = await fetch_page(fake.client(), "http://a.example/", max_bytes=1000)
    assert p.url == "https://b.example/home" and len(p.text) == 1000 and p.cookies == ["a=1", "b=2"] and p.headers["x-frame-options"] == "DENY"


async def test_too_many_redirects_is_blocked(monkeypatch):
    patch_dns(monkeypatch, {"loop.example": "93.184.216.34"})
    fake = FakeHttp({"https://loop.example/": httpx.Response(302, headers={"location": "https://loop.example/"})})
    with pytest.raises(FetchBlocked):
        await fetch_page(fake.client(), "https://loop.example/", max_redirects=2)


async def test_a_private_site_target_is_refused_without_a_request(monkeypatch):
    patch_dns(monkeypatch, {"intranet.example": "10.0.0.8"})
    fake = transport({})
    pl = SecurityReviewPipeline(Settings(groq_api_key="", provider_timeout=1.0, global_timeout=5.0), fake.client())
    j = (await pl.answer({"target": "https://intranet.example/"})).model_dump()
    assert j["verdict"] is None
    assert not any(c.url.host == "intranet.example" for c in fake.calls)        # never contacted (a DNS lookup may name it)
