"""Resolve a Jenkins integration instance (base_url + user + token) from dop-agent's config.yaml.

The CLI and the watcher scripts run in minimal environments (agent shell, `watch_script` subprocess), so the
token is looked up here instead of being passed through argv/env: `${env:X}` / `${file:P}` / `${keychain:S}` refs of
`integrations.<instance>.auth.token` are resolved with dop-agent's own `config.resolve_ref`; `dop-agent/.env` is
loaded first. Nothing here ever prints a token.

    load_instance(name=None) -> (name, base_url, user, token, options)
        name=None: JENKINS_INSTANCE env, else the only enabled jenkins instance (several -> error listing them).
        A missing/disabled instance or an empty token/user raises InstanceError (never falls back to another one).
Overrides: JENKINS_INSTANCE (default for --instance), DOP_AGENT_CONFIG (config path).
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
             if isinstance(c, dict) and c.get("driver") == "jenkins" and c.get("enabled", True)}
    name = name or os.environ.get("JENKINS_INSTANCE") or None
    if name is None:
        if len(insts) != 1:
            raise InstanceError("pass --instance <name>; enabled jenkins instances: " + (", ".join(sorted(insts)) or "none"))
        name = next(iter(insts))
    cfg = insts.get(name)
    if cfg is None:
        raise InstanceError(f"jenkins instance {name!r} not found/enabled; known: " + (", ".join(sorted(insts)) or "none"))
    base = str(cfg.get("base_url") or "").rstrip("/")
    opts = dict(cfg.get("options") or {})
    user = str(opts.get("user") or "")
    raw = str((cfg.get("auth") or {}).get("token") or "")
    _load_dotenv()
    try:
        token = _resolve_ref(raw)
    except Exception as exc:
        raise InstanceError(f"instance {name!r}: cannot resolve auth.token ({exc})") from None
    if not base or not token or not user:
        raise InstanceError(f"instance {name!r}: base_url, options.user and a non-empty auth.token (env/file/keychain ref) are required")
    return name, base, user, token, opts
