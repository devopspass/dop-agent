"""Resolve an Argo CD integration instance (base_url + options) from dop-agent's config.yaml.

Auth is the user's own `argocd` CLI session (`argocd login <host> --sso`), so there is NO token here: nothing in
this package reads the argocd session file or prints credentials.

    load_instance(name=None) -> (name, base_url, options)
        name=None: ARGOCD_INSTANCE env or the only enabled argocd instance (several -> error listing them).
Overrides: ARGOCD_INSTANCE (default for --instance), DOP_AGENT_CONFIG (config path).
"""
from __future__ import annotations

import os
from pathlib import Path


class InstanceError(RuntimeError):
    pass


def _config_path() -> Path:
    env = os.environ.get("DOP_AGENT_CONFIG")
    return Path(env).expanduser() if env else Path.home() / ".dop-agent" / "config.yaml"


def load_instance(name: str | None = None):
    import yaml
    path = _config_path()
    try:
        doc = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise InstanceError(f"cannot read {path}: {exc}") from None
    insts = {n: c for n, c in (doc.get("integrations") or {}).items()
             if isinstance(c, dict) and c.get("driver") == "argocd" and c.get("enabled", True)}
    name = name or os.environ.get("ARGOCD_INSTANCE") or None
    if name is None:
        if len(insts) != 1:
            raise InstanceError("pass --instance <name>; enabled argocd instances: " + (", ".join(sorted(insts)) or "none"))
        name = next(iter(insts))
    cfg = insts.get(name)
    if cfg is None:
        raise InstanceError(f"argocd instance {name!r} not found/enabled; known: " + (", ".join(sorted(insts)) or "none"))
    base = str(cfg.get("base_url") or "").rstrip("/")
    if not base.startswith(("https://", "http://")):
        raise InstanceError(f"instance {name!r}: base_url must start with https:// or http://")
    return name, base, dict(cfg.get("options") or {})
