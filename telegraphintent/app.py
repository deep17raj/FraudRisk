"""Combined ASGI entrypoint: one process, one host, one miner.yaml, every registered intent.

The intents (their POST paths, packages and canonical names) come from `telegraphintent/registry.py`,
the single source of truth; `miner.yaml` is generated from the same list (scripts/sync_intents.py).

    POST <path>      -> the intent's own FastAPI app (package `app.create_app`)
    GET  /healthz    -> aggregate health of every intent
    GET  /miner.yaml -> the single registration file, byte-exact
    GET  /           -> index

Each intent package keeps its own guarantees (always HTTP 200, exactly the intent's response fields,
its own exception handler). This module only routes a request to the right package's own FastAPI app by
path and runs all the lifespans together, so nothing about an intent's behaviour changes by being
hosted alongside the others.

Per-intent configuration
------------------------
The packages read overlapping environment variable names (ANTHROPIC_MODEL, CACHE_TTL_S,
GLOBAL_TIMEOUT_S, LOG_LEVEL, ...). Shared names apply to every intent. To give one intent a
different value, prefix the name with the upper-case package name (FRAUDRISK_, FACTCHECK_, AUDIT_,
IPGEO_, EMAILSEC_, URLSAFE_, MALWARE_, SANCTIONS_, ...; for example MALWARE_GLOBAL_TIMEOUT_S). A
prefixed variable wins over the shared one for that intent only. Only fraudrisk, factcheck and audit use
an API key; every other intent is keyless.
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import logging
import os
from pathlib import Path
from typing import Callable, Iterator

from .registry import INTENTS as REGISTRY, IntentSpec

log = logging.getLogger("telegraphintent.app")
VERSION = "0.2.0"
YAML_PATH = Path(__file__).resolve().parent.parent / "miner.yaml"


@contextlib.contextmanager
def scoped_env(prefix: str) -> Iterator[None]:
    """Temporarily expose `<prefix>_NAME` as `NAME` while one package reads its settings."""
    pre = prefix + "_"
    overlay = {k[len(pre):]: v for k, v in os.environ.items() if k.startswith(pre) and len(k) > len(pre)}
    saved = {k: os.environ.get(k) for k in overlay}
    os.environ.update(overlay)
    try:
        yield
    finally:
        for k, old in saved.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old


def _builder(spec: IntentSpec) -> Callable:
    def build():
        app_module = importlib.import_module(f"{spec.package}.app")
        settings_cls = importlib.import_module(f"{spec.package}.config").Settings
        with scoped_env(spec.prefix):
            return app_module.create_app(settings_cls.from_env())
    return build


#: (name, intent, POST path, builder), in registry order. The POST path matches miner.yaml's endpoint path.
INTENTS: list[tuple[str, str, str, Callable]] = [
    (spec.name, spec.intent, spec.path, _builder(spec)) for spec in REGISTRY
]


async def _json(send, payload: dict, status: int = 200) -> None:
    body = json.dumps(payload).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class TelegraphIntentApp:
    """Tiny ASGI router in front of the per-intent FastAPI apps."""

    def __init__(self, yaml_path: Path = YAML_PATH, apps: dict | None = None) -> None:
        self.yaml_path = yaml_path
        self.apps = apps if apps is not None else {name: build() for name, _, _, build in INTENTS}
        self.routes = {path: self.apps[name] for name, _, path, _ in INTENTS if name in self.apps}
        self.intent_for = {path: intent for _, intent, path, _ in INTENTS}

    # ---- lifespan: run every intent's own startup/shutdown together --------------------------
    async def _lifespan(self, receive, send) -> None:
        stack = contextlib.AsyncExitStack()
        try:
            await receive()  # lifespan.startup
            # Concurrent: several intents download a list or feed while starting, and those waits must overlap.
            await asyncio.gather(*(stack.enter_async_context(app.router.lifespan_context(app))
                                   for app in self.apps.values()))
        except Exception as exc:  # noqa: BLE001
            log.exception("startup failed")
            await send({"type": "lifespan.startup.failed", "message": f"{type(exc).__name__}: {exc}"})
            return
        await send({"type": "lifespan.startup.complete"})
        await receive()  # lifespan.shutdown
        try:
            await stack.aclose()
        finally:
            await send({"type": "lifespan.shutdown.complete"})

    # ---- request routing --------------------------------------------------------------------
    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            return await self._lifespan(receive, send)
        if scope["type"] != "http":
            return
        path = scope["path"].rstrip("/") or "/"
        method = scope["method"]

        if path in self.routes:
            # Delegate everything on an intent path (including a wrong method) to that package.
            return await self.routes[path](scope, receive, send)
        if path == "/healthz" and method in ("GET", "HEAD"):
            return await self._healthz(send)
        if path == "/miner.yaml" and method in ("GET", "HEAD"):
            return await self._miner_yaml(send)
        if path == "/" and method in ("GET", "HEAD"):
            return await _json(send, {
                "service": "telegraph-intent-miner",
                "intents": {intent: f"POST {p}" for _, intent, p, _ in INTENTS},
                "endpoints": [f"POST {p}" for _, _, p, _ in INTENTS] + ["GET /healthz", "GET /miner.yaml"]})
        await _json(send, {"status": "not_found"}, 404)

    async def _healthz(self, send) -> None:
        parts, ok = {}, True
        for name, app in self.apps.items():
            state = getattr(app.state, "pipeline", None)
            if state is None:  # lifespan has not run (or failed) for this intent
                parts[name] = {"status": "not_started"}
                ok = False
            else:
                parts[name] = {"status": "ok", "cache_items": state.cache.size()}
        await _json(send, {"status": "ok" if ok else "degraded", "version": VERSION,
                           "intents": parts})

    async def _miner_yaml(self, send) -> None:
        """Serve miner.yaml verbatim: the on-chain SHA-256 must match these exact bytes."""
        try:
            raw = self.yaml_path.read_bytes()
        except OSError:
            return await _json(send, {"error": "miner.yaml is not bundled with this deployment"}, 404)
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/yaml"),
                                (b"cache-control", b"no-cache"),
                                (b"content-length", str(len(raw)).encode())]})
        await send({"type": "http.response.body", "body": raw})


app = TelegraphIntentApp()
