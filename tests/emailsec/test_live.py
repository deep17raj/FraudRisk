"""Real resolvers, real network. Deselected by default; run with `pytest -m live tests/emailsec`."""
from __future__ import annotations

import httpx
import pytest

from emailsec.config import Settings
from emailsec.pipeline import EmailSecPipeline

pytestmark = pytest.mark.live


@pytest.mark.parametrize("domain,rtype,expected", [
    ("google.com", "MX", True),
    ("google.com", "TXT", True),
    ("_dmarc.google.com", "TXT", True),
    ("example.com", "MX", True),             # exists, publishes a null MX: still NOERROR
    ("this-does-not-exist-xyz123.com", "MX", False),
    ("not a domain!", "MX", False),
])
async def test_real_lookups(domain, rtype, expected):
    async with httpx.AsyncClient() as client:
        res = await EmailSecPipeline(Settings(), client).lookup(domain, rtype)
    assert res.ok is expected, res
    assert not res.degraded
