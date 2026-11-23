"""Resolve a Datadog integration instance (site, API key, application key, options) from dop-agent's config.yaml.

The CLI and watcher scripts run in minimal environments (agent shell, `watch_script` subprocess), so the instance is
looked up here by name instead of passing credentials through argv/env. `integrations.<instance>.auth` is
`{type: api_keys, api_key: '${env:DD_API_KEY}', app_key: '${env:DD_APP_KEY}'}`; refs are `${env:X}` / `${file:P}` /
`${keychain:S}`, resolved with dop-agent's `config.resolve_ref` (`dop-agent/.env` is loaded first). Nothing here prints a key.

    load_instance(name=None) -> Instance
        name=None: DATADOG_INSTANCE, else the only enabled datadog instance (several -> error listing them).
        A missing/disabled instance or an empty key raises InstanceError (never falls back to another instance).
Override: DOP_AGENT_CONFIG (config path).
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[3]
SITE_RE = re.compile(r"(?i)([a-z0-9]{2,12}\.)?datadoghq\.(com|eu)|ddog-gov\.com|ap[0-9]\.datadoghq\.com")


class InstanceError(RuntimeError):
    pass


@dataclass
class Instance:
    name: str
    site: str
    api_key: str = field(repr=False, default="")
    app_key: str = field(repr=False, default="")
    options: dict = field(default_factory=dict)

    @property
    def api(self) -> str:
        return f"https://api.{self.site}"


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
             if isinstance(c, dict) and c.get("driver") == "datadog" and c.get("enabled", True)}
    name = name or os.environ.get("DATADOG_INSTANCE") or None
    if name is None:
        if len(insts) != 1:
            raise InstanceError("pass --instance <name>; enabled datadog instances: " + (", ".join(sorted(insts)) or "none"))
        name = next(iter(insts))
    cfg = insts.get(name)
    if cfg is None:
        raise InstanceError(f"datadog instance {name!r} not found/enabled; known: " + (", ".join(sorted(insts)) or "none"))
    options = dict(cfg.get("options") or {})
    site = str(options.get("site") or "datadoghq.com").strip().lower()
    if not SITE_RE.fullmatch(site):
        raise InstanceError(f"instance {name!r}: invalid options.site {site!r} (e.g. datadoghq.eu, datadoghq.com)")
    auth = cfg.get("auth") or {}
    if auth.get("type") not in (None, "api_keys"):
        raise InstanceError(f"instance {name!r}: auth.type must be api_keys")
    _load_dotenv()
    keys = {}
    for k in ("api_key", "app_key"):
        try:
            keys[k] = _resolve_ref(str(auth.get(k) or ""))
        except Exception as exc:
            raise InstanceError(f"instance {name!r}: cannot resolve auth.{k} ({type(exc).__name__})") from None
        if not keys[k]:
            raise InstanceError(f"instance {name!r}: auth.{k} is empty (env/file/keychain ref not set?)")
    return Instance(name, site, keys["api_key"], keys["app_key"], options)
