"""The consensus step is what decides the latitude that gets scored, so it is tested directly."""
from __future__ import annotations

import pytest

from ipgeo import providers as P
from ipgeo.consensus import haversine_km, merge
from tests.ipgeo import payloads as pl
from tests.ipgeo.conftest import fix

RANK = {"ipinfo": 0, "ipwho": 1, "ipapi_co": 2, "freeipapi": 3, "geojs": 4}


def test_haversine_matches_a_known_distance():
    # San Francisco -> Los Angeles is about 559 km.
    assert haversine_km(37.7749, -122.4194, 34.0522, -118.2437) == pytest.approx(559, abs=5)
    assert haversine_km(10.0, 20.0, 10.0, 20.0) == 0.0


def test_the_recorded_8_8_8_8_answers_form_one_bay_area_cluster_and_drop_geojs():
    fixes = [P.parse_ipinfo(pl.IPINFO), P.parse_ipwho(pl.IPWHO), P.parse_ipapi_co(pl.IPAPI_CO),
             P.parse_freeipapi(pl.FREEIPAPI), P.parse_geojs(pl.GEOJS)]
    m = merge(fixes, 100.0, RANK)
    assert set(m.providers) == {"ipinfo", "ipwho", "ipapi_co", "freeipapi"}
    assert (m.support, m.total) == (4, 5)
    assert m.latitude == pytest.approx(37.422505, abs=1e-6)       # median of the four latitudes
    assert m.longitude == pytest.approx(-122.084176, abs=1e-6)
    assert m.country == "United States"
    assert m.city == "Mountain View"                               # 3 of 4 say Mountain View, not San Jose
    assert m.isp == "Google LLC"


def test_a_single_outlier_cannot_move_the_answer():
    fixes = [fix("a", 48.85, 2.35), fix("b", 48.86, 2.34), fix("c", -33.9, 151.2, city="Sydney", country="Australia")]
    m = merge(fixes, 100.0, {"a": 0, "b": 1, "c": 2})
    assert m.country == "United States" and abs(m.latitude - 48.855) < 0.01 and m.support == 2


def test_a_tie_goes_to_the_cluster_holding_the_best_ranked_provider():
    fixes = [fix("late", 10.0, 10.0, city="X"), fix("early", 50.0, 50.0, city="Y")]
    m = merge(fixes, 100.0, {"early": 0, "late": 1})
    assert m.providers == ("early",) and m.city == "Y"


def test_one_fix_is_returned_as_is():
    m = merge([fix("only", 12.345678, 98.765432)], 100.0, {"only": 0})
    assert (m.latitude, m.longitude, m.support, m.total) == (12.345678, 98.765432, 1, 1)


def test_coordinates_are_rounded_to_six_decimals():
    m = merge([fix("a", 1.123456789, 2.987654321)], 100.0, {"a": 0})
    assert (m.latitude, m.longitude) == (1.123457, 2.987654)


def test_country_name_is_preferred_over_the_iso_code():
    fixes = [fix("a", country="", code="US"), fix("b", country="United States", code="US")]
    assert merge(fixes, 100.0, {"a": 0, "b": 1}).country == "United States"


def test_the_iso_code_is_used_when_no_provider_gives_a_name():
    fixes = [fix("a", country="", code="DE", city="Berlin")]
    assert merge(fixes, 100.0, {"a": 0}).country == "DE"


def test_nothing_in_nothing_out():
    assert merge([], 100.0, {}) is None


def test_the_radius_decides_whether_two_fixes_agree():
    near, far = fix("a", 10.0, 10.0), fix("b", 11.5, 10.0)   # ~167 km apart
    assert merge([near, far], 200.0, {"a": 0, "b": 1}).support == 2
    assert merge([near, far], 100.0, {"a": 0, "b": 1}).support == 1


def test_country_centre_and_null_island_coordinates_are_placeholders_not_locations():
    from ipgeo.consensus import is_placeholder
    assert is_placeholder(fix("geojs", 37.751, -97.822)) and is_placeholder(fix("x", 0.0, 0.0))
    assert not is_placeholder(fix("ipinfo", 37.422, -122.085)) and not is_placeholder(fix("x", 37.9, -97.8))
