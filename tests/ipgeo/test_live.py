"""Real providers, real network. Deselected by default; run with `pytest -m live tests/ipgeo`."""
from __future__ import annotations

import httpx
import pytest

from ipgeo.config import Settings
from ipgeo.pipeline import IpGeoPipeline

pytestmark = pytest.mark.live


async def test_public_dns_server_is_located_in_the_united_states():
    async with httpx.AsyncClient(follow_redirects=True) as client:
        res = await IpGeoPipeline(Settings(), client).locate("8.8.8.8")
    assert not res.degraded, res
    assert res.country == "United States"
    assert 36 < res.latitude < 40 and -125 < res.longitude < -95
    assert len(res.providers) >= 1


async def test_ipv6_is_located():
    async with httpx.AsyncClient(follow_redirects=True) as client:
        res = await IpGeoPipeline(Settings(), client).locate("2001:4860:4860::8888")
    assert not res.degraded and res.latitude is not None
