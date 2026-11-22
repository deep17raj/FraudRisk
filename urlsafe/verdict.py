"""Evidence -> (verdict, detections, summary). Pure logic, so every branch is unit-tested.

  Malicious   the exact URL is on a live feed, or two or more sources list the address
  Suspicious  exactly one source lists it, or its structure carries enough red flags (no list names it)
  Safe        at least one source was consulted and none lists it, and the address has no strong red flag
  Unknown     no source could be consulted and the address shows no strong red flag

`detections` counts sources that LIST the address and nothing else.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .heuristics import Flag


class Verdict(str, Enum):
    SAFE = "Safe"
    SUSPICIOUS = "Suspicious"
    MALICIOUS = "Malicious"
    UNKNOWN = "Unknown"


@dataclass(frozen=True)
class Listing:
    source: str      # "OpenPhish", "Cloudflare Security DNS"
    level: str       # "url": this exact URL is listed; "host": the site/host is listed
    detail: str      # e.g. "this exact URL is on its live phishing feed"


@dataclass
class Evidence:
    listings: list[Listing] = field(default_factory=list)
    flags: list[Flag] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)     # sources that were actually consulted


@dataclass(frozen=True)
class Outcome:
    verdict: Verdict
    detections: int
    summary: str


def _names(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _flag_text(flags: list[Flag]) -> str:
    return "; ".join(f.text for f in sorted(flags, key=lambda f: -f.weight)[:3])


def decide(ev: Evidence, suspicious_weight: float = 0.6) -> Outcome:
    detections = len(ev.listings)
    weight = min(1.0, sum(f.weight for f in ev.flags))
    listed = _names([f"{l.source} ({l.detail})" for l in ev.listings]) if ev.listings else ""

    if any(l.level == "url" for l in ev.listings) or detections >= 2:
        n = f"{detections} sources list" if detections > 1 else "a live threat feed lists"
        return Outcome(Verdict.MALICIOUS, detections, f"{n.capitalize()} this address: {listed}.")

    if detections == 1:
        return Outcome(Verdict.SUSPICIOUS, detections,
                       f"Flagged by {listed}. That is a single source, so it is treated as suspicious rather "
                       f"than confirmed malicious.")

    if weight >= suspicious_weight:
        return Outcome(Verdict.SUSPICIOUS, 0,
                       f"No threat list checked names this address, but the address itself looks like a lure: "
                       f"{_flag_text(ev.flags)}.")

    if not ev.checked:
        return Outcome(Verdict.UNKNOWN, 0,
                       "No threat source could be reached, and the address shows no strong structural red flag, "
                       "so no verdict can be given.")

    note = f" Minor structural note: {_flag_text(ev.flags)}." if ev.flags else ""
    return Outcome(Verdict.SAFE, 0,
                   f"Not listed by {_names(ev.checked)}, and no strong red flag in the address. This consults "
                   f"only keyless sources, so it is not a guarantee.{note}")
