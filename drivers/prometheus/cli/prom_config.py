"""Resolve a Prometheus integration instance (URLs, auth, options) from dop-agent's config.yaml.

The CLI and the watcher scripts run in minimal environments (agent shell, `watch_script` subprocess), so the
instance is looked up here instead of being passed through argv/env. Secrets (`auth.token` / `auth.password`) are
`${env:X}` / `${file:P}` / `${keychain:S}` refs resolved with dop-agent's own `config.resolve_ref`
(`dop-agent/.env` is loaded first). Nothing here prints a secret.

    load_instance(name=None) -> Instance
        name=None: PROMETHEUS_INSTANCE, else the only enabled prometheus instance (several -> error listing them).
Overrides: PROMETHEUS_INSTANCE (default for --instance), DOP_AGENT_CONFIG (config path).
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[3]
AUTH_TYPES = ("none", "basic", "bearer")


class InstanceError(RuntimeError):
    pass


@dataclass
class Instance:
    name: str
    prometheus_url: str
    alertmanager_url: str = ""
    auth_type: str = "none"
    username: str = ""
    secret: str = field(default="", repr=False)  # bearer token or basic password
    options: dict = field(default_factory=dict)


def config_path() -> Path:
    env = os.environ.get("DOP_AGENT_CONFIG")
    return Path(env).expanduser() if env else Path.home() / ".dop-agent" / "config.yaml"


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(AGENT_DIR / ".env", override=False)
    except ImportError:
        pass


def _resolve_ref(value: str) -> str:
    if str(AGENT_DIR) not in sys.path:
        sys.path.insert(0, str(AGENT_DIR))
    import config as agent_config
    return agent_config.resolve_ref(value)


def _url(value, what: str, name: str, required: bool) -> str:
    url = str(value or "").strip().rstrip("/")
    if not url:
        if required:
            raise InstanceError(f"instance {name!r}: option {what} (http[s]://host[:port]) is required")
        return ""
    if not url.startswith(("http://", "https://")):
        raise InstanceError(f"instance {name!r}: option {what} must start with http:// or https://")
    return url


def load_instance(name: str | None = None) -> Instance:
    import yaml
    path = config_path()
    try:
        doc = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise InstanceError(f"cannot read {path}: {exc}") from None
    insts = {n: c for n, c in (doc.get("integrations") or {}).items()
             if isinstance(c, dict) and c.get("driver") == "prometheus" and c.get("enabled", True)}
    name = name or os.environ.get("PROMETHEUS_INSTANCE") or None
    if name is None:
        if len(insts) != 1:
            raise InstanceError("pass --instance <name>; enabled prometheus instances: " + (", ".join(sorted(insts)) or "none"))
        name = next(iter(insts))
    cfg = insts.get(name)
    if cfg is None:
        raise InstanceError(f"prometheus instance {name!r} not found/enabled; known: " + (", ".join(sorted(insts)) or "none"))
    options = dict(cfg.get("options") or {})
    prom = _url(options.get("prometheus_url"), "prometheus_url", name, True)
    am = _url(options.get("alertmanager_url"), "alertmanager_url", name, False)
    auth = cfg.get("auth") or {}
    atype = str(auth.get("type") or "none")
    if atype not in AUTH_TYPES:
        raise InstanceError(f"instance {name!r}: unsupported auth.type {atype!r} (use {', '.join(AUTH_TYPES)})")
    user = secret = ""
    if atype != "none":
        _load_dotenv()
        key = "token" if atype == "bearer" else "password"
        try:
            secret = _resolve_ref(str(auth.get(key) or ""))
        except Exception as exc:
            raise InstanceError(f"instance {name!r}: cannot resolve auth.{key} ({type(exc).__name__})") from None
        if not secret:
            raise InstanceError(f"instance {name!r}: a non-empty auth.{key} (env/file/keychain ref) is required")
        if atype == "basic":
            user = str(auth.get("username") or "")
            if not user:
                raise InstanceError(f"instance {name!r}: auth.username is required for basic auth")
    return Instance(name, prom, am, atype, user, secret, options)
