"""Known vulnerabilities in dependencies, from OSV.dev (keyless, aggregates GHSA, PyPI, npm, Go, RustSec and more).

`parse_packages` reads what a caller pasted (a manifest, a lock file, or "name@version" tokens). `scan` asks OSV about
every package in one batch request and then fetches advisory details (aliases and severity) for the first few, so the
same advisory published under several ids (GHSA, PYSEC, CVE) is counted once.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Optional

from intentkit.upstream import Upstream

from .findings import Finding

BATCH = "https://api.osv.dev/v1/querybatch"
VULN = "https://api.osv.dev/v1/vulns/"
_SEVERITY = {"CRITICAL": "critical", "HIGH": "high", "MODERATE": "medium", "MEDIUM": "medium", "LOW": "low"}
_ECOSYSTEM = {"npm": "npm", "pypi": "PyPI", "pip": "PyPI", "go": "Go", "golang": "Go", "cargo": "crates.io",
              "crates": "crates.io", "crates.io": "crates.io", "maven": "Maven", "gem": "RubyGems", "rubygems": "RubyGems",
              "nuget": "NuGet", "composer": "Packagist", "packagist": "Packagist"}


@dataclass(frozen=True)
class Pkg:
    ecosystem: str
    name: str
    version: str

    def label(self) -> str:
        return f"{self.name}@{self.version}"


def _clean_version(raw: str) -> Optional[str]:
    """The lowest version a range allows ('^1.2.3' -> '1.2.3'); None for ranges that name no version."""
    m = re.search(r"\d+(?:\.\d+){0,3}(?:[-+.][0-9A-Za-z.\-]+)?", raw or "")
    return m.group(0) if m and not re.search(r"\|\||\bx\b|\*", raw) else None


def _from_json(text: str) -> list[Pkg]:
    try:
        d = json.loads(text)
    except ValueError:
        return []
    out: list[Pkg] = []
    if not isinstance(d, dict):
        return out
    for section in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        deps = d.get(section)
        if isinstance(deps, dict):
            for name, spec in deps.items():
                v = _clean_version(spec if isinstance(spec, str) else (spec.get("version", "") if isinstance(spec, dict) else ""))
                if v and isinstance(name, str):
                    out.append(Pkg("npm", name, v))
    pk = d.get("packages")                                       # package-lock.json v2/v3: exact versions
    if isinstance(pk, dict):
        for path, info in pk.items():
            if path.startswith("node_modules/") and isinstance(info, dict) and isinstance(info.get("version"), str):
                out.append(Pkg("npm", path.rsplit("node_modules/", 1)[1], info["version"]))
    return out


def parse_packages(text: str, limit: int = 150) -> list[Pkg]:
    pkgs: list[Pkg] = []
    stripped = text.strip()
    if stripped.startswith("{"):
        pkgs += _from_json(stripped)
    for m in re.finditer(r"(?m)^\s*([A-Za-z0-9][A-Za-z0-9_.\-]*)\s*(?:\[[^\]]*\])?\s*===?\s*([0-9][A-Za-z0-9_.!+\-]*)", text):
        pkgs.append(Pkg("PyPI", m.group(1), m.group(2)))                                   # requirements.txt
    for m in re.finditer(r"(?m)^\s*(?:require\s+)?([A-Za-z0-9_.\-]+\.[A-Za-z]{2,}/[\w.\-/~]+)\s+v(\d[\w.\-+]*)", text):
        pkgs.append(Pkg("Go", m.group(1), m.group(2)))                                       # go.mod
    for m in re.finditer(r'(?m)^name\s*=\s*"([^"]+)"\s*\nversion\s*=\s*"([^"]+)"', text):
        pkgs.append(Pkg("crates.io", m.group(1), m.group(2)))                                # Cargo.lock
    if len(stripped) <= 400:                                                                 # "npm:lodash@4.17.15"
        for m in re.finditer(r"(?:\b(npm|pypi|pip|go|cargo|crates|maven|gem|nuget|composer):)?(@?[A-Za-z0-9_.\-/]+)(@|==)(\d[\w.\-+]*)",
                             stripped, re.I):
            eco = _ECOSYSTEM.get((m.group(1) or ("pypi" if m.group(3) == "==" else "npm")).lower())
            if eco and "." not in m.group(2).split("/")[0][-1:] and "@" not in m.group(2)[1:]:
                pkgs.append(Pkg(eco, m.group(2), m.group(4)))
    seen, out = set(), []
    for p in pkgs:
        k = (p.ecosystem, p.name.lower(), p.version)
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out[:limit]


@dataclass(frozen=True)
class Advisory:
    key: str                    # the canonical id used to count one advisory once
    id: str
    title: str
    severity: str
    pkg: Pkg


async def _detail(up: Upstream, vid: str) -> Optional[dict]:
    d = await up.get_json(VULN + vid, key="osv", timeout=4.5)
    return d if isinstance(d, dict) else None


def _severity_of(d: dict) -> str:
    sev = ((d.get("database_specific") or {}).get("severity") or "").upper()
    return _SEVERITY.get(sev, "medium")


async def scan(up: Upstream, pkgs: list[Pkg], max_details: int = 40) -> Optional[list[Finding]]:
    """Findings for the packages, or None when OSV could not be reached (so nothing is reported as clean)."""
    if not pkgs:
        return []
    body = {"queries": [{"package": {"name": p.name, "ecosystem": p.ecosystem}, "version": p.version} for p in pkgs]}
    res = await up.fetch(BATCH, method="POST", json_body=body, key="osv", timeout=7.0)
    results = res[1].get("results") if res is not None and res[0] == 200 and isinstance(res[1], dict) else None
    if not isinstance(results, list):
        return None
    pairs: list[tuple[Pkg, str]] = []
    for pkg, r in zip(pkgs, results):
        for v in (r or {}).get("vulns", []) or []:
            if isinstance(v, dict) and v.get("id"):
                pairs.append((pkg, v["id"]))
    details = await asyncio.gather(*(_detail(up, vid) for _, vid in pairs[:max_details]))
    found: dict[tuple[str, str], Finding] = {}
    for i, (pkg, vid) in enumerate(pairs):
        d = details[i] if i < len(details) else None
        aliases = [a for a in (d or {}).get("aliases", []) if isinstance(a, str)]
        cve = next((a for a in aliases if a.startswith("CVE-")), "")
        ghsa = next((a for a in [vid, *aliases] if a.startswith("GHSA-")), "")
        shown = cve or ghsa or vid
        title = ((d or {}).get("summary") or "known vulnerability")[:110]
        key = (pkg.label(), cve or ghsa or vid)
        found.setdefault(key, Finding(shown, title, _severity_of(d) if d else "medium", pkg.label()))
    return list(found.values())
