"""Process-wide registry built from config.yaml (``integrations:``), defaults when none configured.

    runtime.get_registry()   -> Registry (cached; rebuilt on config.reload())
    runtime.reset()          drop the cache (tests)
"""
from __future__ import annotations

import threading

import config

from .registry import Registry, build_registry, default_integrations

_lock = threading.Lock()
_registry: Registry | None = None
_hooked = False


def _from_config() -> dict:
    out: dict = {}
    for name, it in config.integrations().items():
        d = it.as_dict()
        d["auth"] = {k: (v.reveal() if hasattr(v, "reveal") else v) for k, v in it.resolved_auth().items()}
        out[name] = d
    return out


def get_registry() -> Registry:
    global _registry, _hooked
    with _lock:
        if not _hooked:
            config.on_change(reset)
            _hooked = True
        if _registry is None:
            _registry = build_registry(_from_config() or default_integrations())
        return _registry


def reset() -> None:
    global _registry
    with _lock:
        _registry = None
