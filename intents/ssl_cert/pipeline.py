"""Open a real TLS connection and report whether the certificate verifies.

`certificate_valid` is true exactly when the handshake succeeds under normal verification: the chain leads
to a trusted root (certifi's bundle), the certificate is inside its validity period, and it names the host.
An expired, self-signed, wrong-host or untrusted certificate is false, as is a host that presents no TLS at
all. Only public addresses are contacted (the host is resolved first and every address must be global), so the
endpoint cannot be used to probe a private network.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import ssl
from typing import Optional

import certifi
from pydantic import BaseModel

from intentkit.pipeline import BasePipeline, Req, Result
from intentkit.service import StrictModel

from .config import Settings

_PORTS = {443, 8443}
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)
_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


class SslResponse(StrictModel):
    """Exactly the response field of Telegraph's SSL_VERIFICATION_CERT intent. Nothing else."""

    certificate_valid: bool = False


def parse_host(raw: str) -> Optional[tuple[str, int]]:
    """'https://Example.com:8443/x' -> ('example.com', 8443). None when no hostname can be found."""
    s = (raw or "").strip()
    m = re.search(r"([\w.-]+\.[^\W\d_]{2,}|\[[0-9a-fA-F:]+\]|\d{1,3}(?:\.\d{1,3}){3})(?::(\d{1,5}))?",
                  _SCHEME.sub("", s).split("/")[0] if _SCHEME.match(s) else s)
    if not m:
        return None
    host = m.group(1).strip("[]").strip(".").lower()
    port = int(m.group(2)) if m.group(2) else 443
    port = port if port in _PORTS else 443
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    try:
        ipaddress.ip_address(host)
        return host, port
    except ValueError:
        pass
    if len(host) > 253 or not all(_LABEL.match(p) for p in host.split(".")):
        return None
    return host, port


class SslPipeline(BasePipeline):
    intent = "SSL_VERIFICATION_CERT"

    def __init__(self, settings: Settings, client) -> None:
        super().__init__(settings, client)
        self.ctx = ssl.create_default_context(cafile=certifi.where())

    def key(self, req: Req) -> Optional[str]:
        hp = parse_host(req.get("host", ""))
        return f"ssl:{hp[0]}:{hp[1]}" if hp else None

    def empty(self, req: Req, reason: str) -> BaseModel:
        return SslResponse(certificate_valid=False)

    async def _public_addresses(self, host: str, port: int) -> list[str]:
        """The resolved addresses (IPv4 first), provided every one is public; [] otherwise."""
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addrs = list(dict.fromkeys(i[4][0] for i in infos))
        if not self.settings.allow_private_hosts and not all(ipaddress.ip_address(a).is_global for a in addrs):
            return []
        return sorted(addrs, key=lambda a: ":" in a)[:3]

    async def compute(self, req: Req) -> Result:
        hp = parse_host(req.get("host", ""))
        if hp is None:
            return Result(self.empty(req, "no host"), degraded=True)
        host, port = hp
        s: Settings = self.settings
        try:
            addrs = await asyncio.wait_for(self._public_addresses(host, port), timeout=s.provider_timeout)
        except (OSError, asyncio.TimeoutError):
            return Result(self.empty(req, "host does not resolve"))        # nothing to present a certificate
        if not addrs:
            return Result(self.empty(req, "not a public address"))
        for addr in addrs:
            try:
                # Connect to the vetted address but verify against the host name, so a DNS rebind cannot
                # swap the target between the check above and the connection.
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(addr, port, ssl=self.ctx, server_hostname=host),
                    timeout=s.provider_timeout)
            except ssl.SSLError:                       # untrusted, expired, wrong host, or no usable TLS
                return Result(SslResponse(certificate_valid=False))
            except (OSError, asyncio.TimeoutError):
                continue                               # this address is unreachable: try the next
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
            except Exception:  # noqa: BLE001 - the verdict is already known
                pass
            return Result(SslResponse(certificate_valid=True))
        return Result(self.empty(req, "could not connect"), degraded=True)
