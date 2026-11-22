"""Real OpenPhish feed and real Cloudflare DNS. Deselected by default; run with `pytest -m live tests/urlsafe`."""
from __future__ import annotations

import httpx
import pytest

from urlsafe.config import Settings
from urlsafe.pipeline import UrlSafePipeline
from urlsafe.verdict import Verdict

pytestmark = pytest.mark.live


async def test_a_url_from_the_live_feed_is_malicious_and_a_major_site_is_safe():
    async with httpx.AsyncClient(follow_redirects=True) as client:
        feed_text = (await client.get("https://openphish.com/feed.txt", timeout=15)).text
        listed = next(line.strip() for line in feed_text.splitlines() if line.strip().startswith("http"))
        pl = UrlSafePipeline(Settings(), client)
        await pl.start()
        assert pl.feed.ready

        bad = await pl.check(listed)
        assert bad.verdict is Verdict.MALICIOUS and bad.detections >= 1, bad

        good = await pl.check("https://www.wikipedia.org/")
        assert good.verdict is Verdict.SAFE and good.detections == 0, good


async def test_cloudflares_published_test_domain_is_blocked():
    async with httpx.AsyncClient(follow_redirects=True) as client:
        res = await UrlSafePipeline(Settings(enable_feed=False), client).check("http://malware.testcategory.com/")
    assert res.verdict in (Verdict.SUSPICIOUS, Verdict.MALICIOUS) and res.detections >= 1, res
