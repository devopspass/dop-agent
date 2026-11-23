"""Resolve a Bitbucket integration instance (base_url + token + flavor/API endpoint) from dop-agent's config.yaml.

The CLI and the watcher scripts run in minimal environments (agent shell, `watch_script` subprocess), so the
token is looked up here instead of being passed through argv/env: `${env:X}` / `${file:P}` / `${keychain:S}` refs of
`integrations.<instance>.auth.token` are resolved with dop-agent's own `config.resolve_ref`; `dop-agent/.env` is
loaded first. Nothing here ever prints a token.

    load_instance(name=None) -> (name, base_url, token, username, options)
        name=None: BITBUCKET_INSTANCE env or the only enabled bitbucket instance (several -> error listing them).
        username: optional `auth.username`; set -> HTTP Basic (Cloud API token = Atlassian e-mail, app password =
        username, Server user), empty -> Bearer (workspace/repository/project/HTTP access token).
    flavor(base_url, options) -> "cloud" | "server"   (options.flavor, else bitbucket.org = cloud, else server)
    api_url(base_url, options) -> REST root (Cloud https://api.bitbucket.org/2.0, Server <base>/rest/api/1.0;
        options.api_url overrides)
Overrides: BITBUCKET_INSTANCE (default for --instance), DOP_AGENT_CONFIG (config path).
"""
from __future__ import annotations

import os
import sys
import urllib.parse
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
             if isinstance(c, dict) and c.get("driver") == "bitbucket" and c.get("enabled", True)}
    name = name or os.environ.get("BITBUCKET_INSTANCE") or None
    if name is None:
        if len(insts) != 1:
            raise InstanceError("pass --instance <name>; enabled bitbucket instances: " + (", ".join(sorted(insts)) or "none"))
        name = next(iter(insts))
    cfg = insts.get(name)
    if cfg is None:
        raise InstanceError(f"bitbucket instance {name!r} not found/enabled; known: " + (", ".join(sorted(insts)) or "none"))
    base = str(cfg.get("base_url") or "").rstrip("/")
    auth = cfg.get("auth") or {}
    _load_dotenv()
    try:
        token = _resolve_ref(str(auth.get("token") or ""))
        username = _resolve_ref(str(auth.get("username") or "")) if auth.get("username") else ""
    except Exception as exc:
        raise InstanceError(f"instance {name!r}: cannot resolve auth.token/auth.username ({exc})") from None
    if not base or not token:
        raise InstanceError(f"instance {name!r}: base_url and a non-empty auth.token (env/file/keychain ref) are required")
    return name, base, token, username, dict(cfg.get("options") or {})


def flavor(base_url: str, options: dict | None = None) -> str:
    opt = str((options or {}).get("flavor") or "auto").lower()
    if opt in ("cloud", "server"):
        return opt
    host = urllib.parse.urlsplit(base_url).hostname or ""
    return "cloud" if host in ("bitbucket.org", "www.bitbucket.org") else "server"


def api_url(base_url: str, options: dict | None = None) -> str:
    override = str((options or {}).get("api_url") or "").rstrip("/")
    if override:
        return override
    if flavor(base_url, options) == "cloud":
        return "https://api.bitbucket.org/2.0"
    return base_url.rstrip("/") + "/rest/api/1.0"
