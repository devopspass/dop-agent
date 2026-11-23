"""Resolve a Splunk integration instance (base_url, token, options) from dop-agent's config.yaml.

The CLI and the watcher script run in minimal environments (agent shell, `watch_script` subprocess), so the
instance is looked up here instead of being passed through argv/env. The `${env:X}` / `${file:P}` / `${keychain:S}`
ref of `auth.token` is resolved with dop-agent's own `config.resolve_ref` (`dop-agent/.env` is loaded first).
Nothing here prints a token.

    load_instance(name=None) -> Instance
        name=None: SPLUNK_INSTANCE, else the only enabled splunk instance (several -> error listing them).
Overrides: SPLUNK_INSTANCE (default for --instance), DOP_AGENT_CONFIG (config path).
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[3]


class InstanceError(RuntimeError):
    pass


@dataclass
class Instance:
    name: str
    base_url: str
    token: str = field(default="", repr=False)
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


def load_instance(name: str | None = None) -> Instance:
    import yaml
    path = config_path()
    try:
        doc = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise InstanceError(f"cannot read {path}: {exc}") from None
    insts = {n: c for n, c in (doc.get("integrations") or {}).items()
             if isinstance(c, dict) and c.get("driver") == "splunk" and c.get("enabled", True)}
    name = name or os.environ.get("SPLUNK_INSTANCE") or None
    if name is None:
        if len(insts) != 1:
            raise InstanceError("pass --instance <name>; enabled splunk instances: " + (", ".join(sorted(insts)) or "none"))
        name = next(iter(insts))
    cfg = insts.get(name)
    if cfg is None:
        raise InstanceError(f"splunk instance {name!r} not found/enabled; known: " + (", ".join(sorted(insts)) or "none"))
    base = str(cfg.get("base_url") or "").rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise InstanceError(f"instance {name!r}: base_url (https://host:8089) is required")
    _load_dotenv()
    try:
        token = _resolve_ref(str((cfg.get("auth") or {}).get("token") or ""))
    except Exception as exc:
        raise InstanceError(f"instance {name!r}: cannot resolve auth.token ({type(exc).__name__})") from None
    if not token:
        raise InstanceError(f"instance {name!r}: a non-empty auth.token (env/file/keychain ref) is required")
    return Instance(name, base, token, dict(cfg.get("options") or {}))
