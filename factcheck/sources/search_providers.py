"""Vendor adapters (Tavily / Serper / Brave). Each is a thin mapping to the common Evidence type."""
from __future__ import annotations

import httpx

from ..models import Evidence
from ..text_utils import strip_html
from .base import SearchProvider


class TavilyProvider(SearchProvider):
    name = "tavily"
    URL = "https://api.tavily.com/search"

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def configured(self) -> bool:
        return bool(self.api_key)

    async def _run(self, query: str, client: httpx.AsyncClient, timeout: float, topic: str) -> list[Evidence]:
        body: dict = {"query": query[:380], "max_results": 6, "search_depth": "basic", "topic": topic,
                      "include_answer": False}
        if topic == "news":
            body["days"] = 14
        r = await client.post(self.URL, json=body, timeout=timeout,
                              headers={"Authorization": f"Bearer {self.api_key}"})
        r.raise_for_status()
        out = []
        for it in r.json().get("results", []):
            if it.get("url") and it.get("content"):
                out.append(Evidence(url=it["url"], title=it.get("title", ""), snippet=it["content"][:900],
                                    provider=f"tavily-{topic}", published=it.get("published_date")))
        return out

    async def web(self, query, client, timeout):
        return await self._run(query, client, timeout, "general")

    async def news(self, query, client, timeout):
        return await self._run(query, client, timeout, "news")


class SerperProvider(SearchProvider):
    name = "serper"

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def configured(self) -> bool:
        return bool(self.api_key)

    async def _run(self, path: str, key: str, query: str, client: httpx.AsyncClient, timeout: float, tag: str):
        r = await client.post(f"https://google.serper.dev/{path}", json={"q": query[:300], "num": 8},
                              headers={"X-API-KEY": self.api_key}, timeout=timeout)
        r.raise_for_status()
        out = []
        for it in r.json().get(key, []):
            link, snip = it.get("link"), it.get("snippet")
            if link and snip:
                out.append(Evidence(url=link, title=it.get("title", ""), snippet=snip[:900],
                                    provider=f"serper-{tag}", published=it.get("date")))
        return out

    async def web(self, query, client, timeout):
        return await self._run("search", "organic", query, client, timeout, "web")

    async def news(self, query, client, timeout):
        return await self._run("news", "news", query, client, timeout, "news")


class BraveProvider(SearchProvider):
    name = "brave"

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def configured(self) -> bool:
        return bool(self.api_key)

    async def _run(self, path: str, query: str, client: httpx.AsyncClient, timeout: float, tag: str):
        r = await client.get(f"https://api.search.brave.com/res/v1/{path}",
                             params={"q": query[:380], "count": 8},
                             headers={"X-Subscription-Token": self.api_key, "Accept": "application/json"},
                             timeout=timeout)
        r.raise_for_status()
        data = r.json()
        items = data.get("web", {}).get("results", []) if path.startswith("web") else data.get("results", [])
        out = []
        for it in items:
            url, desc = it.get("url"), it.get("description")
            if url and desc:
                out.append(Evidence(url=url, title=strip_html(it.get("title", "")), snippet=strip_html(desc)[:900],
                                    provider=f"brave-{tag}", published=it.get("page_age") or it.get("age")))
        return out

    async def web(self, query, client, timeout):
        return await self._run("web/search", query, client, timeout, "web")

    async def news(self, query, client, timeout):
        return await self._run("news/search", query, client, timeout, "news")
