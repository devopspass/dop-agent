"""Resolve a HashiCorp Vault integration instance (addr, namespace, token) from dop-agent's config.yaml.

The CLI and the watcher scripts run in minimal environments, so the instance is looked up here by name (argv
`--instance` / env VAULT_INSTANCE) and the token is resolved from its secret ref (`${env:X}` / `${file:P}` /
`${keychain:S}`, via dop-agent's `config.resolve_ref`; `dop-agent/.env` is loaded first). The token never
travels through argv/env/skills and is never printed.

    load_instance(name=None) -> Instance
        name=None: VAULT_INSTANCE, else the only enabled vault instance (several -> error listing them).
        A missing/disabled instance or an empty token raises InstanceError (never falls back to another instance).
Override: DOP_AGENT_CONFIG (config path).
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[3]
_NS_RE = re.compile(r"^[A-Za-z0-9_.\-]+(/[A-Za-z0-9_.\-]+)*/?$")


class InstanceError(RuntimeError):
    pass


@dataclass
class Instance:
    name: str
    base_url: str
    token: str = ""
    namespace: str = ""
    tls_verify: bool = True
    options: dict = field(default_factory=dict)


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


def load_instance(name: str | None = None) -> Instance:
    import yaml
    path = _config_path()
    try:
        doc = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise InstanceError(f"cannot read {path}: {exc}") from None
    insts = {n: c for n, c in (doc.get("integrations") or {}).items()
             if isinstance(c, dict) and c.get("driver") == "vault" and c.get("enabled", True)}
    name = name or os.environ.get("VAULT_INSTANCE") or None
    if name is None:
        if len(insts) != 1:
            raise InstanceError("pass --instance <name>; enabled vault instances: " + (", ".join(sorted(insts)) or "none"))
        name = next(iter(insts))
    cfg = insts.get(name)
    if cfg is None:
        raise InstanceError(f"vault instance {name!r} not found/enabled; known: " + (", ".join(sorted(insts)) or "none"))
    base = str(cfg.get("base_url") or "").strip().rstrip("/")
    if not re.match(r"^https?://[A-Za-z0-9.\-]+(:[0-9]{1,5})?$", base):
        raise InstanceError(f"instance {name!r}: base_url must be http(s)://host[:port] (the Vault addr)")
    opts = dict(cfg.get("options") or {})
    ns = str(opts.get("namespace") or "").strip()
    if ns and not _NS_RE.match(ns):
        raise InstanceError(f"instance {name!r}: invalid namespace")
    auth = cfg.get("auth") or {}
    _load_dotenv()
    try:
        token = _resolve_ref(str(auth.get("token") or ""))
    except Exception as exc:
        raise InstanceError(f"instance {name!r}: cannot resolve auth.token ({type(exc).__name__})") from None
    if not token:
        raise InstanceError(f"instance {name!r}: auth.token is empty (env/file/keychain ref not set?)")
    if re.search(r"\s", token.strip()):
        raise InstanceError(f"instance {name!r}: auth.token contains whitespace/newlines (bad secret ref content)")
    tls = str(opts.get("tls_verify", "true")).lower() != "false"
    return Instance(name, base, token.strip(), ns, tls, opts)
