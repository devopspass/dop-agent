"""Resolve a JSM integration instance (base_url, token, options) from dop-agent's config.yaml.

Same convention as the gitlab driver: the CLI and the watcher scripts run in minimal environments (agent shell,
`watch_script` subprocess), so the token is looked up here instead of being passed through argv/env: `${env:X}` /
`${file:P}` / `${keychain:S}` refs of `integrations.<instance>.auth.token` are resolved with dop-agent's own
`config.resolve_ref`; `dop-agent/.env` is loaded first so `${env:X}` works without the bot's environment.
Nothing here ever prints a token.

    load_instance(name=None) -> (name, base_url, token, options)
        name=None: the only enabled jsm instance (several -> error listing them).
Overrides: JSM_INSTANCE (default for --instance), DOP_AGENT_CONFIG (config path).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[3]


class InstanceError(RuntimeError):
    pass


def _config_path() -> Path:
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


def load_instance(name: str | None = None):
    import yaml
    path = _config_path()
    try:
        doc = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise InstanceError(f"cannot read {path}: {exc}") from None
    insts = {n: c for n, c in (doc.get("integrations") or {}).items()
             if isinstance(c, dict) and c.get("driver") == "jsm" and c.get("enabled", True)}
    name = name or os.environ.get("JSM_INSTANCE") or None
    if name is None:
        if len(insts) != 1:
            raise InstanceError("pass --instance <name>; enabled jsm instances: " + (", ".join(sorted(insts)) or "none"))
        name = next(iter(insts))
    cfg = insts.get(name)
    if cfg is None:
        raise InstanceError(f"jsm instance {name!r} not found/enabled; known: " + (", ".join(sorted(insts)) or "none"))
    base = str(cfg.get("base_url") or "").rstrip("/")
    raw = str((cfg.get("auth") or {}).get("token") or "")
    _load_dotenv()
    try:
        token = _resolve_ref(raw)
    except Exception as exc:
        raise InstanceError(f"instance {name!r}: cannot resolve auth.token ({exc})") from None
    opts = dict(cfg.get("options") or {})
    if not base or not token or not opts.get("email"):
        raise InstanceError(f"instance {name!r}: base_url, options.email and a non-empty auth.token (env/file/keychain ref) are required")
    return name, base, token, opts
