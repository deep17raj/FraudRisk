from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_module_level_caches():
    """The CVE source keeps one process-wide cache shared by two intents; each test starts with it empty."""
    from intents.cve import source
    source.SHARED._data.clear()
    source.SHARED._inflight.clear()
    yield
    source.SHARED._data.clear()
