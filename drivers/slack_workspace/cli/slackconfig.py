"""Resolve a Slack-workspace integration instance (base_url, token, options) from dop-agent's config.yaml.

The CLI and the watcher run in minimal environments (agent shell, `watch_script` subprocess), so the instance is
looked up here instead of being passed through argv/env. Auth: `integrations.<instance>.auth.token` is a
`${env:X}` / `${file:P}` / `${keychain:S}` ref resolved with dop-agent's `config.resolve_ref` (`dop-agent/.env` is
loaded first). Nothing here ever prints a token.

    load_instance(name=None) -> Instance
        name=None: SLACK_WS_INSTANCE, else the only enabled slack_workspace instance (several -> error listing them).
        A missing/disabled/renamed instance or an empty token raises InstanceError (never falls back to another one).
Overrides: DOP_AGENT_CONFIG (config path).
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[3]
DRIVER_ID = "slack_workspace"
DEFAULT_API = "https://slack.com/api"
_SECRET_RE = re.compile(r"(?i)(token|password|secret|authorization)[=:]\s*\S+|xox[a-z]-[A-Za-z0-9-]+")


class InstanceError(RuntimeError):
    pass


@dataclass
class Instance:
    name: str
    base_url: str            # workspace URL (https://team.slack.com), used for permalinks
    token: str = ""
    options: dict = field(default_factory=dict)

    @property
    def api(self) -> str:
        return str(self.options.get("api_url") or DEFAULT_API).rstrip("/")

    @property
    def can_write(self) -> bool:
        return (self.options.get("write_mode") or "confirm") != "none"


def redact(text: str) -> str:
    return _SECRET_RE.sub(lambda m: (m.group(1) + "=***") if m.group(1) else "***", str(text))


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
        raise InstanceError(f"cannot read {path}: {type(exc).__name__}") from None
    insts = {n: c for n, c in (doc.get("integrations") or {}).items()
             if isinstance(c, dict) and c.get("driver") == DRIVER_ID and c.get("enabled", True)}
    name = name or os.environ.get("SLACK_WS_INSTANCE") or None
    if name is None:
        if len(insts) != 1:
            raise InstanceError("pass --instance <name>; enabled slack_workspace instances: "
                                + (", ".join(sorted(insts)) or "none"))
        name = next(iter(insts))
    cfg = insts.get(name)
    if cfg is None:
        raise InstanceError(f"slack_workspace instance {name!r} not found/enabled; known: "
                            + (", ".join(sorted(insts)) or "none"))
    base = str(cfg.get("base_url") or "").strip().rstrip("/")
    if not base.startswith("https://"):
        raise InstanceError(f"instance {name!r}: base_url (https://<team>.slack.com) is required")
    _load_dotenv()
    try:
        token = _resolve_ref(str((cfg.get("auth") or {}).get("token") or ""))
    except Exception as exc:
        raise InstanceError(f"instance {name!r}: cannot resolve auth.token ({type(exc).__name__})") from None
    if not token:
        raise InstanceError(f"instance {name!r}: auth.token is empty (env/file/keychain ref not set?)")
    return Instance(name, base, token, dict(cfg.get("options") or {}))
