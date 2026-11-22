"""Hermetic helpers: an injected feed and fake DoH resolvers. Nothing here touches the network."""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from intentkit.threat import UrlFeed  # noqa: E402
from tests.emailsec.conftest import FakeDns, noerror  # noqa: E402,F401
from urlsafe.config import Settings  # noqa: E402
from urlsafe.pipeline import UrlSafePipeline  # noqa: E402

FEED_TEXT = "\n".join([
    "https://secure-login-update.evil.example/paypal/verify.php?id=1",
    "http://www.galeri-pemenang99.blogspot.com/",
    "https://docs.google.com/forms/d/e/1FAIpQLSabc/viewform",
])


def make_settings(**kw) -> Settings:
    base = dict(enable_feed=True, enable_dns=True, global_timeout=3.0, provider_timeout=1.0,
                cache_ttl=60, cache_ttl_degraded=0)
    base.update(kw)
    return Settings(**base)


def loaded_feed(text: str = FEED_TEXT) -> UrlFeed:
    f = UrlFeed("t", "http://feed.invalid", 3600, 1.0)
    f._items = f.parse(text)
    f._loaded_at = time.monotonic()
    return f


def empty_feed() -> UrlFeed:
    return UrlFeed("t", "http://feed.invalid", 3600, 1.0)      # never loaded: not ready


def blocked(name: str) -> dict:
    return noerror(name, (1, "0.0.0.0"))


def clean(name: str) -> dict:
    return noerror(name, (1, "93.184.216.34"))


def build(feed=None, security=None, **settings) -> tuple[UrlSafePipeline, FakeDns]:
    dns = FakeDns(security=security)
    s = make_settings(**settings)
    return UrlSafePipeline(s, dns.client(), feed=feed if feed is not None else loaded_feed()), dns
