"""Regenerate every file derived from telegraphintent/registry.py.

    python scripts/sync_intents.py           write miner.yaml and the generated blocks
    python scripts/sync_intents.py --check   exit 1 if any generated file is out of date (CI / tests)

Generated: miner.yaml (whole file), the probe block in register.sh, the package block in the Dockerfile.
Never edit those by hand (docs/INTENT_RULES.md, rule F18).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from telegraphintent.registry import IDENTITY, INTENTS, IntentSpec  # noqa: E402

_NUMBERS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine",
            10: "ten", 11: "eleven", 12: "twelve"}


def _plain_ok(text: str) -> bool:
    return (bool(text) and text == text.strip() and ": " not in text and " #" not in text
            and not text.endswith(":") and text[0] not in "!&*-?:,[]{}#|>@`\"'%"
            and "\n" not in text and "\t" not in text)


def _scalar(text: str) -> str:
    return text if _plain_ok(text) else json.dumps(text, ensure_ascii=False)


def miner_description(intents=INTENTS) -> str:
    n = len(intents)
    head = f"One miner serving {_NUMBERS.get(n, str(n))} Telegraph intents."
    return " ".join([head, *(s.summary for s in intents)])


def _param_lines(p, indent: str) -> list[str]:
    return [f"{indent}- name: {p.name}",
            f"{indent}  type: {p.type}",
            f'{indent}  intents: ["*"]',
            f"{indent}  description: {json.dumps(p.description, ensure_ascii=False)}"]


def render_yaml(intents=INTENTS) -> str:
    out = ['version: "1"', "kind: miner", f"id: {IDENTITY['id']}", f"slug: {IDENTITY['slug']}",
           "protocol: generic", f"name: {IDENTITY['name']}",
           f"description: {_scalar(miner_description(intents))}",
           f"base_url: {IDENTITY['base_url']}", "", "auth:", "  type: none", "", "endpoints:"]
    for s in intents:
        out += [f"  - path: {s.path}", f"    external_path: {s.path}", "    method: POST",
                f"    description: {_scalar(s.endpoint_description)}", f"    intents: [{s.intent}]",
                "    params:", "      body:"]
        for key, wanted in (("required", True), ("optional", False)):
            group = [p for p in s.params if p.required is wanted]
            if group:
                out.append(f"        {key}:")
                for p in group:
                    out += _param_lines(p, "          ")
        out.append("")
    out += ["semantics:", "  supported_intents:", *(f"    - {s.intent}" for s in intents), ""]
    return "\n".join(out)


def _top_packages(intents=INTENTS) -> list[str]:
    seen: list[str] = []
    for s in intents:
        top = s.package.split(".")[0]
        if top not in seen:
            seen.append(top)
    return seen


def render_dockerfile_block(intents=INTENTS) -> str:
    pkgs = ["telegraphintent", *_top_packages(intents), "intentkit"]
    return "\n".join(f"COPY {p} ./{p}" for p in pkgs)


def render_register_probes(intents=INTENTS) -> str:
    lines = []
    for s in intents:
        body = s.probe if "'" not in s.probe else s.probe.replace("'", "'\\''")
        lines.append(f"probe {s.path} '{body}' {' '.join(s.fields)}")
    return "\n".join(lines)


def render_register_intents(intents=INTENTS) -> str:
    return "INTENTS='" + json.dumps([s.intent for s in intents], separators=(",", ":")) + "'"


def _swap(text: str, tag: str, body: str) -> str:
    pat = re.compile(rf"(# BEGIN GENERATED: {tag}[^\n]*\n)(.*?)(# END GENERATED: {tag}\n)", re.S)
    if not pat.search(text):
        raise SystemExit(f"marker '# BEGIN GENERATED: {tag}' not found")
    return pat.sub(lambda m: m.group(1) + body + "\n" + m.group(3), text, count=1)


def expected_files(intents=INTENTS) -> dict[Path, str]:
    reg = (ROOT / "register.sh").read_text(encoding="utf-8")
    reg = _swap(reg, "intents", render_register_intents(intents))
    reg = _swap(reg, "probes", render_register_probes(intents))
    dock = _swap((ROOT / "Dockerfile").read_text(encoding="utf-8"), "packages", render_dockerfile_block(intents))
    return {ROOT / "miner.yaml": render_yaml(intents), ROOT / "register.sh": reg, ROOT / "Dockerfile": dock}


def main(argv: list[str]) -> int:
    check = "--check" in argv
    stale = []
    for path, text in expected_files().items():
        current = path.read_bytes().decode("utf-8") if path.exists() else None
        if current == text:
            continue
        if check:
            stale.append(path.name)
        else:
            path.write_bytes(text.encode("utf-8"))   # bytes: keep LF on Windows
            print(f"wrote {path.relative_to(ROOT)}")
    if stale:
        print("out of date, run: python scripts/sync_intents.py  ->", ", ".join(stale))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
