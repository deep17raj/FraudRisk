"""IP reputation and geolocation, behind a swappable vendor adapter.

No vendor is hardcoded: `IpAdapter` normalizes whatever the configured provider returns into
one `IpIntel` record, and `IP_PROVIDER` picks the implementation (ipwho | ipinfo |
ip2location | auto). Adding a vendor means adding an adapter, not touching the source.

A note on honesty: the keyless tier of ipwho.is returns geolocation and network ownership but
no proxy/VPN flag (its `security` object is null without a paid key). Where a provider gives
no explicit flag we infer anonymised access from the ASN's network ownership - and the signal
text says so ("ASN 60729 ... is a hosting/anonymisation network"), rather than claiming a
vendor verdict we do not have. Tor membership comes from the Tor Project's own exit list, so
that signal is stated as fact.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import httpx

from ..config import Settings
from ..geo import ELEVATED_RISK_ISO
from ..models import EntityKind, Family, ParsedSubject, Signal
from .base import Source
from .feeds import FeedRegistry

#: Network-operator names that mean "not a residential connection".
_HOSTING_WORDS = re.compile(
    r"\b(hosting|host|datacent|data\s*cent|server|cloud|vps|dedicated|colo|colocation|"
    r"amazon|aws|google\s*cloud|microsoft|azure|digitalocean|linode|ovh|hetzner|vultr|"
    r"contabo|leaseweb|choopa|scaleway|oracle\s*cloud|alibaba|tencent|rackspace|equinix)\b", re.I)

_VPN_WORDS = re.compile(
    r"\b(vpn|proxy|privacy|anonym|tor\b|relay|mullvad|nordvpn|expressvpn|surfshark|"
    r"private\s*internet|cyberghost|protonvpn|ipvanish|torguard|perfect\s*privacy|"
    r"m247|datapacket|packethub|tefincom|cdn77|frantech|buyvm|flokinet)\b", re.I)



@dataclass
class IpIntel:
    ip: str
    provider: str
    country_code: str = ""
    country: str = ""
    city: str = ""
    asn: str = ""
    org: str = ""
    isp: str = ""
    is_proxy: Optional[bool] = None     # explicit vendor flag, when the tier provides one
    is_hosting: Optional[bool] = None
    is_tor: Optional[bool] = None
    explicit_flags: bool = False        # True when the vendor gave real proxy/VPN fields


class IpAdapter(ABC):
    name: str = "ip"
    reliability: float = 0.7

    @abstractmethod
    def configured(self, settings: Settings) -> bool: ...

    @abstractmethod
    async def lookup(self, ip: str, client: httpx.AsyncClient, timeout: float,
                     settings: Settings) -> Optional[IpIntel]: ...


class IpWhoAdapter(IpAdapter):
    """ipwho.is - keyless, no registration, ~10k lookups/month fair use."""

    name = "ipwho"
    reliability = 0.72

    def configured(self, settings: Settings) -> bool:
        return True

    async def lookup(self, ip, client, timeout, settings):
        r = await client.get(f"https://ipwho.is/{ip}", timeout=timeout)
        r.raise_for_status()
        d = r.json()
        if not isinstance(d, dict) or not d.get("success"):
            return None
        conn = d.get("connection") or {}
        sec = d.get("security") or {}
        asn = conn.get("asn")
        intel = IpIntel(
            ip=ip, provider=self.name,
            country_code=(d.get("country_code") or "").upper(),
            country=d.get("country") or "",
            city=d.get("city") or "",
            asn=f"AS{asn}" if asn else "",
            org=conn.get("org") or "",
            isp=conn.get("isp") or "",
        )
        if sec:
            intel.is_proxy = bool(sec.get("proxy") or sec.get("vpn"))
            intel.is_tor = bool(sec.get("tor"))
            intel.is_hosting = bool(sec.get("hosting"))
            intel.explicit_flags = True
        return intel


class IpInfoAdapter(IpAdapter):
    """ipinfo.io - needs IPINFO_TOKEN; the privacy block requires a paid plan."""

    name = "ipinfo"
    reliability = 0.8

    def configured(self, settings: Settings) -> bool:
        return bool(settings.ipinfo_token)

    async def lookup(self, ip, client, timeout, settings):
        r = await client.get(f"https://ipinfo.io/{ip}/json",
                             params={"token": settings.ipinfo_token}, timeout=timeout)
        r.raise_for_status()
        d = r.json()
        if not isinstance(d, dict) or d.get("bogon"):
            return None
        org = d.get("org") or ""
        m = re.match(r"(AS\d+)\s+(.*)", org)
        privacy = d.get("privacy") or {}
        intel = IpIntel(
            ip=ip, provider=self.name,
            country_code=(d.get("country") or "").upper(),
            country=d.get("country") or "",
            city=d.get("city") or "",
            asn=m.group(1) if m else "",
            org=m.group(2) if m else org,
            isp=m.group(2) if m else org,
        )
        if privacy:
            intel.is_proxy = bool(privacy.get("proxy") or privacy.get("vpn"))
            intel.is_tor = bool(privacy.get("tor"))
            intel.is_hosting = bool(privacy.get("hosting"))
            intel.explicit_flags = True
        return intel


class Ip2LocationAdapter(IpAdapter):
    """ip2location.io - needs IP2LOCATION_KEY; proxy fields on the free tier are limited."""

    name = "ip2location"
    reliability = 0.78

    def configured(self, settings: Settings) -> bool:
        return bool(settings.ip2location_key)

    async def lookup(self, ip, client, timeout, settings):
        r = await client.get("https://api.ip2location.io/",
                             params={"key": settings.ip2location_key, "ip": ip}, timeout=timeout)
        r.raise_for_status()
        d = r.json()
        if not isinstance(d, dict) or d.get("error"):
            return None
        proxy = d.get("proxy") or {}
        intel = IpIntel(
            ip=ip, provider=self.name,
            country_code=(d.get("country_code") or "").upper(),
            country=d.get("country_name") or "",
            city=d.get("city_name") or "",
            asn=f"AS{d['asn']}" if d.get("asn") else "",
            org=d.get("as") or "",
            isp=d.get("isp") or d.get("as") or "",
        )
        if d.get("is_proxy") is not None or proxy:
            intel.is_proxy = bool(d.get("is_proxy"))
            intel.is_tor = (proxy.get("proxy_type") or "").upper() == "TOR"
            intel.is_hosting = (proxy.get("usage_type") or "").upper() in {"DCH", "SES"}
            intel.explicit_flags = True
        return intel


ADAPTERS: dict[str, type[IpAdapter]] = {
    "ipwho": IpWhoAdapter,
    "ipinfo": IpInfoAdapter,
    "ip2location": Ip2LocationAdapter,
}


def select_adapter(settings: Settings) -> Optional[IpAdapter]:
    """`auto` prefers a configured paid provider (explicit proxy flags) over the keyless one."""
    choice = (settings.ip_provider or "auto").lower()
    if choice == "none":
        return None
    if choice in ADAPTERS:
        a = ADAPTERS[choice]()
        return a if a.configured(settings) else None
    for name in ("ipinfo", "ip2location", "ipwho"):
        a = ADAPTERS[name]()
        if a.configured(settings):
            return a
    return None


class IpReputationSource(Source):
    """Geolocation + network ownership for every IP in the subject."""

    name = "ip_reputation"
    handles = (EntityKind.IP,)

    def __init__(self, adapter: IpAdapter, feeds: Optional[FeedRegistry] = None) -> None:
        super().__init__()
        self.adapter = adapter
        self.reliability = adapter.reliability
        self.name = f"ip:{adapter.name}"
        #: Consulted only to stay consistent with `TorExitSource`: without it this source
        #: would describe a known Tor exit as an ordinary ISP address, contradicting the
        #: authoritative signal in the same response.
        self.feeds = feeds

    def enabled(self, settings: Settings) -> bool:
        return self.adapter.configured(settings)

    def _known_tor(self, ip: str) -> bool:
        feed = self.feeds.tor if self.feeds else None
        return bool(feed and feed.ready and ip in feed)

    async def collect(self, parsed, client, timeout, settings=None):
        settings = settings or Settings()
        signals: list[Signal] = []
        for ent in parsed.entities_of(EntityKind.IP)[:2]:
            intel = await self.adapter.lookup(ent.value, client, timeout, settings)
            if intel is None:
                continue
            signals.extend(self._to_signals(intel, parsed))
        return signals

    def _to_signals(self, i: IpIntel, parsed: ParsedSubject) -> list[Signal]:
        out: list[Signal] = []
        owner_parts = [x for x in (i.org, i.isp) if x]
        if len(owner_parts) == 2 and owner_parts[0].lower() == owner_parts[1].lower():
            owner_parts = owner_parts[:1]
        owner = " / ".join(owner_parts)
        where = ", ".join(x for x in (i.city, i.country) if x) or "an unknown location"
        rel = self.reliability

        vpn_hit = _VPN_WORDS.search(owner)
        host_hit = _HOSTING_WORDS.search(owner)

        net = f"{i.asn or 'an unlisted ASN'} ({owner.strip() or 'unknown operator'})"
        stated_geo = False
        deferred_to_feed = False

        if i.is_tor:
            out.append(Signal(
                id="anon.tor_vendor",
                label="an IP flagged as a Tor exit node",
                text=f"{i.ip} is flagged as a Tor exit node by {i.provider}, so the real origin "
                     f"of the session is concealed.",
                severity=0.80, reliability=rel, source=self.name, family=Family.ANONYMITY,
                verified=True))
        elif self._known_tor(i.ip):
            # The exit list is authoritative and `TorExitSource` already states it. Saying
            # anything about the anonymity of this address here could only contradict that.
            deferred_to_feed = True
        elif i.is_proxy:
            out.append(Signal(
                id="anon.proxy_vendor",
                label="an IP flagged as a VPN/proxy endpoint",
                text=f"{i.ip} is flagged as a VPN/proxy endpoint by {i.provider} ({net}, "
                     f"{where}), so the true origin is masked.",
                severity=0.55, reliability=rel, source=self.name, family=Family.ANONYMITY,
                verified=True))
            stated_geo = True
        elif vpn_hit:
            out.append(Signal(
                id="anon.vpn_asn",
                label="an IP on a commercial VPN network",
                text=f"{i.ip} belongs to {net}, a commercial VPN/anonymisation operator, so the "
                     f"true origin behind it is masked.",
                severity=0.50, reliability=rel * 0.9, source=self.name, family=Family.ANONYMITY))
        elif i.is_hosting or host_hit:
            out.append(Signal(
                id="anon.hosting_asn",
                label="an IP in a hosting/datacentre range",
                text=f"{i.ip} is in a hosting/datacentre range ({net}, {where}) rather than on a "
                     f"consumer ISP, which is unusual for a genuine customer login.",
                severity=0.42, reliability=rel * 0.9, source=self.name, family=Family.ANONYMITY))
            stated_geo = True

        if i.country_code and i.country_code in ELEVATED_RISK_ISO:
            out.append(Signal(
                id="ip.elevated_risk_geo",
                label="an IP in an elevated-risk jurisdiction",
                text=f"{i.ip} geolocates to {i.country or i.country_code}, a jurisdiction with "
                     f"elevated card-not-present fraud rates.",
                severity=0.22, reliability=rel * 0.65, source=self.name, family=Family.IP_REPUTATION))
        elif not out and not deferred_to_feed:
            # Nothing adverse in the network ownership: state what was checked, factually,
            # without claiming the address is residential (this tier cannot establish that).
            out.append(Signal(
                id="ip.network_ordinary",
                label="an IP with no anonymiser indicators",
                text=f"{i.ip} resolves to {where} on {net}, with no VPN, proxy, Tor or hosting "
                     f"indicator in its network ownership.",
                severity=0.0, reliability=rel, source=self.name,
                family=Family.IP_REPUTATION, benign=True))
        elif deferred_to_feed:
            # Locate the address without re-characterising it; the feed signal carries the verdict.
            out.append(Signal(
                id="ip.network_location",
                label="the connecting IP's location",
                text=f"{i.ip} resolves to {where} on {net}.",
                severity=0.0, reliability=rel, source=self.name,
                family=Family.IP_REPUTATION, benign=True))
        elif i.country_code and not stated_geo:
            out.append(Signal(
                id="ip.geo",
                label="the connecting IP's location",
                text=f"{i.ip} geolocates to {where}.",
                severity=0.0, reliability=rel, source=self.name,
                family=Family.IP_REPUTATION, benign=True))

        # Geography stated in the scenario that disagrees with where the IP actually is.
        if parsed.hints.geo_mismatch and i.country:
            out.append(Signal(
                id="ip.geo_mismatch_context",
                label="an IP location conflicting with the stated geography",
                text=f"The connecting IP is in {i.country}, which conflicts with the location "
                     f"described for the account or payment.",
                severity=0.38, reliability=rel * 0.8, source=self.name,
                family=Family.IP_REPUTATION))
        return out


class TorExitSource(Source):
    """Membership of the Tor Project's published exit list - a fact, not an inference."""

    name = "tor_exits"
    handles = (EntityKind.IP,)
    reliability = 0.95

    def __init__(self, feeds: FeedRegistry) -> None:
        super().__init__()
        self.feeds = feeds

    def enabled(self, settings: Settings) -> bool:
        return self.feeds.tor is not None

    async def collect(self, parsed, client, timeout, settings=None):
        feed = self.feeds.tor
        if feed is None or not feed.ready:
            return []
        out = []
        for ent in parsed.entities_of(EntityKind.IP)[:2]:
            if ent.value in feed:
                out.append(Signal(
                    id="anon.tor_exit",
                label="an IP on the Tor Project's public exit list",
                    text=f"{ent.value} is on the Tor Project's current public exit-node list, so "
                         f"the real origin of the session is concealed.",
                    severity=0.72, reliability=self.reliability, source=self.name,
                    family=Family.ANONYMITY, verified=True))
        return out
