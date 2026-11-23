"""Resolve a Wiz integration instance's credentials from dop-agent's config.yaml (same convention as the gitlab driver).

Secrets never travel through argv/env/skills: the CLI and the watcher look the instance up here by name
(`--instance` / WIZ_INSTANCE) and resolve the `${env:X}` / `${file:P}` / `${keychain:S}` refs of
`integrations.<instance>.auth.client_id` / `auth.client_secret` with dop-agent's own `config.resolve_ref`
(`dop-agent/.env` is loaded first so `${env:X}` works without the bot's environment). Only auth type
`service_account` carries secrets; type `wizcli` (interactive login in wiz_dir) has none. Nothing here prints a secret.

    load_credentials(name=None) -> (name, auth_type, client_id|None, client_secret|None)
        name=None: the only enabled wiz instance (several -> error listing them; none -> interactive wizcli login).
Overrides: WIZ_INSTANCE (default for --instance), DOP_AGENT_CONFIG (config path).
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


def load_credentials(name: str | None = None):
    import yaml
    path = _config_path()
    try:
        doc = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise InstanceError(f"cannot read {path}: {exc}") from None
    insts = {n: c for n, c in (doc.get("integrations") or {}).items()
             if isinstance(c, dict) and c.get("driver") == "wiz" and c.get("enabled", True)}
    name = name or os.environ.get("WIZ_INSTANCE") or None
    if name is None and not insts:
        return None, "wizcli", None, None  # standalone use without an instance: interactive wizcli login
    if name is None:
        if len(insts) != 1:
            raise InstanceError("pass --instance <name>; enabled wiz instances: " + (", ".join(sorted(insts)) or "none"))
        name = next(iter(insts))
    cfg = insts.get(name)
    if cfg is None:
        raise InstanceError(f"wiz instance {name!r} not found/enabled; known: " + (", ".join(sorted(insts)) or "none"))
    auth = cfg.get("auth") or {}
    atype = auth.get("type") or "wizcli"
    if atype != "service_account":
        return name, atype, None, None
    _load_dotenv()
    try:
        cid, secret = _resolve_ref(str(auth.get("client_id") or "")), _resolve_ref(str(auth.get("client_secret") or ""))
    except Exception as exc:
        raise InstanceError(f"instance {name!r}: cannot resolve auth.client_id/client_secret ({exc})") from None
    if not cid or not secret:
        raise InstanceError(f"instance {name!r}: auth.client_id and auth.client_secret (env/file/keychain refs) are required")
    return name, atype, cid, secret
