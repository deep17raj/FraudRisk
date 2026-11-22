"""Reading an IP out of the request, and reducing each provider's JSON to a GeoFix."""
from __future__ import annotations

import pytest

from ipgeo import providers as P
from ipgeo.config import Settings
from ipgeo.parse import extract_ip
from tests.ipgeo import payloads as pl


class TestExtractIp:
    @pytest.mark.parametrize("raw,expected", [
        ("8.8.8.8", "8.8.8.8"),
        ("  8.8.8.8  ", "8.8.8.8"),
        ("Where is the IP address 8.8.8.8 located?", "8.8.8.8"),
        ("8.8.8.8.", "8.8.8.8"),
        ("(1.1.1.1)", "1.1.1.1"),
        ("[2001:4860:4860::8888]", "2001:4860:4860::8888"),
        ("2001:4860:4860:0:0:0:0:8888", "2001:4860:4860::8888"),
        ("::ffff:8.8.8.8", "8.8.8.8"),
        ("geolocate 203.0.113.9 please", "203.0.113.9"),
    ])
    def test_finds_the_address(self, raw, expected):
        assert extract_ip(raw) == expected

    @pytest.mark.parametrize("raw", ["", "   ", "hello world", "999.1.1.1", "1.2.3", "no address here, just dead beef"])
    def test_none_when_there_is_no_valid_address(self, raw):
        assert extract_ip(raw) is None


class TestParsers:
    def test_ipinfo(self):
        f = P.parse_ipinfo(pl.IPINFO)
        assert (f.latitude, f.longitude) == (38.0088, -122.1175)
        assert f.country == "" and f.country_code == "US"          # ipinfo gives only the code
        assert f.city == "Mountain View" and f.isp == "Google LLC"  # "AS15169 " prefix stripped

    def test_ipwho(self):
        f = P.parse_ipwho(pl.IPWHO)
        assert (f.latitude, f.longitude) == (37.3393939, -121.8949553)
        assert f.country == "United States" and f.country_code == "US" and f.city == "San Jose"
        assert f.isp == "Google LLC"

    def test_ipapi_co(self):
        f = P.parse_ipapi_co(pl.IPAPI_CO)
        assert (f.latitude, f.longitude) == (37.42301, -122.083352)
        assert f.country == "United States" and f.isp == "Google LLC"

    def test_freeipapi(self):
        f = P.parse_freeipapi(pl.FREEIPAPI)
        assert (f.latitude, f.longitude) == (37.422, -122.085)
        assert f.city == "Mountain View" and f.isp == "Google LLC"

    def test_geojs_accepts_string_coordinates(self):
        f = P.parse_geojs(pl.GEOJS)
        assert (f.latitude, f.longitude) == (37.751, -97.822)
        assert f.city == "" and f.isp == "Google LLC"

    @pytest.mark.parametrize("parse,bad", [
        (P.parse_ipwho, {"ip": "10.0.0.1", "success": False, "message": "Reserved range"}),
        (P.parse_ipinfo, {"ip": "10.0.0.1", "bogon": True}),
        (P.parse_ipinfo, {"error": {"title": "Rate limit"}}),
        (P.parse_ipapi_co, {"error": True, "reason": "RateLimited"}),
        (P.parse_geojs, {"ip": "8.8.8.8"}),
        (P.parse_freeipapi, {"latitude": None, "longitude": None}),
    ])
    def test_error_payloads_give_no_fix(self, parse, bad):
        assert parse(bad) is None

    @pytest.mark.parametrize("parse", [P.parse_ipwho, P.parse_ipinfo, P.parse_ipapi_co, P.parse_geojs, P.parse_freeipapi])
    @pytest.mark.parametrize("junk", [None, [], "text", 42, {}])
    def test_junk_never_raises(self, parse, junk):
        assert parse(junk) is None

    def test_null_island_is_not_a_location(self):
        assert P.parse_ipwho({"success": True, "latitude": 0, "longitude": 0}) is None

    def test_out_of_range_coordinates_are_rejected(self):
        assert P.parse_ipwho({"success": True, "latitude": 95.0, "longitude": 10.0}) is None
        assert P.parse_ipwho({"success": True, "latitude": 10.0, "longitude": 190.0}) is None

    def test_non_finite_and_boolean_coordinates_are_rejected(self):
        assert P.parse_ipwho({"success": True, "latitude": float("nan"), "longitude": 1.0}) is None
        assert P.parse_ipwho({"success": True, "latitude": True, "longitude": 1.0}) is None


class TestProviderSelection:
    def test_all_by_default(self):
        assert [p.name for p in P.select_providers(Settings())] == ["ipinfo", "ipwho", "ipapi_co", "freeipapi", "geojs"]

    def test_subset(self):
        assert [p.name for p in P.select_providers(Settings(providers="ipwho, geojs"))] == ["ipwho", "geojs"]

    def test_an_unknown_subset_falls_back_to_all(self):
        assert len(P.select_providers(Settings(providers="nonsense"))) == 5

    def test_ipinfo_token_is_sent_only_when_configured(self):
        assert P.PROVIDERS[0].url("8.8.8.8", Settings())[1] == {}
        assert P.PROVIDERS[0].url("8.8.8.8", Settings(ipinfo_token="t0k"))[1] == {"token": "t0k"}
