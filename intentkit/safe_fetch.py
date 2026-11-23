"""Fetch a page a caller pointed us at, without letting them point us at our own network.

Every hop (the first request and each redirect) is checked: http(s) only, a short list of ports, and the host must
resolve to public addresses only. Redirects are followed by us, never by the HTTP client, so a public URL cannot
bounce the request to 169.254.169.254 or localhost. The body is read up to a byte cap, so a huge download cannot be
used to exhaust memory.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import httpx

ALLOWED_PORTS = {80, 443, 8080, 8443}


class FetchBlocked(Exception):
    """The URL is not one this service is willing to fetch."""


@dataclass
class Page:
    url: str                       # the final URL after redirects
    status: int
    headers: dict[str, str]        # lower-cased names; repeated headers are joined with ", "
    cookies: list[str] = field(default_factory=list)   # each Set-Cookie header, separately
    text: str = ""
    redirects: list[str] = field(default_factory=list)


async def assert_public(url: str, allow_private: bool = False) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise FetchBlocked(f"unsupported scheme {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise FetchBlocked("no host in URL")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise FetchBlocked(f"port {port} is not allowed")
    if allow_private:
        return
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise FetchBlocked(f"host does not resolve ({exc})") from exc
    addrs = {i[4][0] for i in infos}
    if not addrs:
        raise FetchBlocked("host does not resolve")
    for a in addrs:
        if not ipaddress.ip_address(a).is_global:
            raise FetchBlocked(f"{host} resolves to the non-public address {a}")


async def fetch_page(client: httpx.AsyncClient, url: str, *, timeout: float = 5.0, max_bytes: int = 200_000,
                     max_redirects: int = 3, allow_private: bool = False) -> Page:
    """GET `url`, re-checking every hop. Raises FetchBlocked, or an httpx error for a network failure."""
    current, hops = url, []
    for _ in range(max_redirects + 1):
        await assert_public(current, allow_private)
        async with client.stream("GET", current, follow_redirects=False, timeout=timeout,
                                 headers={"Accept": "text/html,*/*;q=0.8"}) as resp:
            if resp.is_redirect and resp.headers.get("location"):
                nxt = urljoin(current, resp.headers["location"])
                hops.append(nxt)
                current = nxt
                continue
            chunks, total = [], 0
            async for chunk in resp.aiter_bytes():
                room = max_bytes - total
                if room <= 0:
                    break
                chunks.append(chunk[:room])
                total += len(chunk[:room])
            headers: dict[str, str] = {}
            for k in {k.lower() for k in resp.headers.keys()}:
                headers[k] = ", ".join(resp.headers.get_list(k))
            return Page(str(resp.url), resp.status_code, headers, resp.headers.get_list("set-cookie"),
                        b"".join(chunks).decode("utf-8", "replace"), hops)
    raise FetchBlocked(f"more than {max_redirects} redirects")
