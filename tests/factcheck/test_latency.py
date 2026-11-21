"""Latency tests.

OFFLINE (default): sources are mocked with realistic 80-600 ms delays, so this measures the end-to-end
pipeline + HTTP overhead on top of provider time (it is NOT a measure of real provider latency).
LIVE (`pytest -m live`): real Wikipedia (+ any keyed providers / LLM configured in the environment).
"""
import asyncio
import random
import statistics
import time

import httpx
import pytest
from httpx import ASGITransport

from factcheck.app import create_app
from factcheck.config import Settings
from factcheck.pipeline import FactCheckPipeline
from tests.factcheck.conftest import FakeSource, ev, make_settings

P95_CEILING_S = 3.0           # offline: overhead + mocked provider delay
P95_CEILING_LIVE_S = 9.0      # live: well under the 12 s hard cap so routing never sees us as unreliable


class JitterSource(FakeSource):
    async def search(self, claim, client, timeout):
        await asyncio.sleep(random.uniform(0.08, 0.6))
        return await super().search(claim, client, timeout)


def p95(xs):
    xs = sorted(xs)
    return xs[max(0, int(round(0.95 * len(xs))) - 1)]


async def test_offline_p95_latency_under_ceiling_with_concurrency():
    random.seed(7)
    snippet = "The Eiffel Tower is a wrought-iron lattice tower located in Paris, France."
    evid = [ev(f"https://en.wikipedia.org/wiki/E{i}", "Eiffel Tower", snippet, provider="wikipedia") for i in range(4)]
    settings = make_settings(global_timeout=8.0, retrieval_timeout=4.0)
    pipeline = FactCheckPipeline(settings, httpx.AsyncClient(), sources=[JitterSource("a", evid), JitterSource("b", evid)])
    app = create_app(settings, pipeline)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            async def one(i):
                t0 = time.perf_counter()
                r = await c.post("/fact-check", json={"claim": f"The Eiffel Tower number {i} is located in Paris."})
                assert r.status_code == 200 and r.json()["verdict"]
                return time.perf_counter() - t0

            lat = await asyncio.gather(*[one(i) for i in range(120)])     # 120 unique claims, all concurrent
    print(f"\n offline latency: p50={statistics.median(lat):.2f}s p95={p95(lat):.2f}s max={max(lat):.2f}s")
    assert p95(lat) < P95_CEILING_S


@pytest.mark.live
async def test_live_p95_latency_under_ceiling():
    claims = ["The Eiffel Tower is located in Paris.", "Humans only use 10% of their brains.",
              "Mount Everest is the tallest mountain above sea level.", "Sharks are mammals.",
              "Albert Einstein won the Nobel Prize in Physics in 1921.", "Mars is the fourth planet from the Sun.",
              "Lightning never strikes the same place twice.", "The Pacific Ocean is the largest ocean on Earth."]
    s = Settings.from_env()
    async with httpx.AsyncClient(http2=True, follow_redirects=True, headers={"User-Agent": s.user_agent}) as client:
        pipeline = FactCheckPipeline(s, client)
        lat = []
        for c in claims * 2:
            t0 = time.perf_counter()
            r = await asyncio.wait_for(pipeline.check(c + " "), timeout=s.global_timeout + 1)
            lat.append(time.perf_counter() - t0)
            assert r.verdict
    print(f"\n LIVE latency: p50={statistics.median(lat):.2f}s p95={p95(lat):.2f}s max={max(lat):.2f}s")
    assert p95(lat) < P95_CEILING_LIVE_S
