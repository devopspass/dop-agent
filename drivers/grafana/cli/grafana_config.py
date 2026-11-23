"""Resolve a Grafana integration instance (base_url, auth, options) from dop-agent's config.yaml.

The CLI and the watcher scripts run in minimal environments (agent shell, `watch_script` subprocess), so the
instance is looked up here instead of being passed through argv/env. For `auth.type: service_account_token` the
`${env:X}` / `${file:P}` / `${keychain:S}` ref of `auth.token` is resolved with dop-agent's own
`config.resolve_ref` (`dop-agent/.env` is loaded first). For `auth.type: oauth_cdp` there is no stored secret:
`grafana_oauth.py` keeps a refreshable token in `~/.dop-agent/grafana-state/<instance>.json`. Nothing here prints a token.

    load_instance(name=None) -> Instance
        name=None: GRAFANA_INSTANCE, else the only enabled grafana instance (several -> error listing them).
Overrides: GRAFANA_INSTANCE (default for --instance), DOP_AGENT_CONFIG (config path).
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[3]
TOKEN_AUTH = "service_account_token"
OAUTH_AUTH = "oauth_cdp"


class InstanceError(RuntimeError):
    pass


@dataclass
class Instance:
    name: str
    base_url: str
    auth_type: str
    token: str = field(default="", repr=False)
    options: dict = field(default_factory=dict)


def config_path() -> Path:
    env = os.environ.get("DOP_AGENT_CONFIG")
    return Path(env).expanduser() if env else Path.home() / ".dop-agent" / "config.yaml"


def state_dir() -> Path:
    return Path(os.environ.get("DOP_AGENT_HOME") or Path.home() / ".dop-agent") / "grafana-state"


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
             if isinstance(c, dict) and c.get("driver") == "grafana" and c.get("enabled", True)}
    name = name or os.environ.get("GRAFANA_INSTANCE") or None
    if name is None:
        if len(insts) != 1:
            raise InstanceError("pass --instance <name>; enabled grafana instances: " + (", ".join(sorted(insts)) or "none"))
        name = next(iter(insts))
    cfg = insts.get(name)
    if cfg is None:
        raise InstanceError(f"grafana instance {name!r} not found/enabled; known: " + (", ".join(sorted(insts)) or "none"))
    base = str(cfg.get("base_url") or "").rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise InstanceError(f"instance {name!r}: base_url (http[s]://host) is required")
    auth = cfg.get("auth") or {}
    atype = str(auth.get("type") or TOKEN_AUTH)
    options = dict(cfg.get("options") or {})
    token = ""
    if atype == TOKEN_AUTH:
        _load_dotenv()
        try:
            token = _resolve_ref(str(auth.get("token") or ""))
        except Exception as exc:
            raise InstanceError(f"instance {name!r}: cannot resolve auth.token ({exc})") from None
        if not token:
            raise InstanceError(f"instance {name!r}: a non-empty auth.token (env/file/keychain ref) is required")
    elif atype == OAUTH_AUTH:
        if not options.get("idp_url"):
            raise InstanceError(f"instance {name!r}: option idp_url is required for auth.type oauth_cdp")
    else:
        raise InstanceError(f"instance {name!r}: unsupported auth.type {atype!r}")
    return Instance(name, base, atype, token, options)
