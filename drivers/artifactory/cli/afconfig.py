"""Resolve an Artifactory integration instance (base_url, auth, options) from dop-agent's config.yaml.

The CLI and the watcher scripts run in minimal environments (agent shell, `watch_script` subprocess), so the
instance is looked up here instead of being passed through argv/env. Auth types:
  anonymous     no credentials (read access only, whatever the server allows anonymously)
  access_token  `integrations.<instance>.auth.token` (`${env:X}` / `${file:P}` / `${keychain:S}` ref, resolved with
                dop-agent's own `config.resolve_ref`; `dop-agent/.env` is loaded first)
  sso_token     token minted by `token authorize` (browser SSO over CDP) and kept in a chmod-600 state file
Nothing here ever prints a token.

    load_instance(name=None) -> Instance
        name=None: ARTIFACTORY_INSTANCE, else the only enabled artifactory instance (several -> error listing them).
Overrides: DOP_AGENT_CONFIG (config path), DOP_AGENT_STATE_DIR (default ~/.dop-agent/driver-state).
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[3]
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")


class InstanceError(RuntimeError):
    pass


@dataclass
class Instance:
    name: str
    base_url: str            # server root, without a trailing /artifactory
    auth_type: str
    token: str = ""
    options: dict = field(default_factory=dict)

    @property
    def api(self) -> str:
        return f"{self.base_url}/artifactory"


def _config_path() -> Path:
    env = os.environ.get("DOP_AGENT_CONFIG")
    return Path(env).expanduser() if env else Path.home() / ".dop-agent" / "config.yaml"


def state_path(name: str) -> Path:
    if not _NAME_RE.match(name):
        raise InstanceError(f"invalid instance name {name!r}")
    root = os.environ.get("DOP_AGENT_STATE_DIR")
    base = Path(root).expanduser() if root else Path.home() / ".dop-agent" / "driver-state"
    return base / "artifactory" / f"{name}.json"


def load_state(name: str) -> dict:
    try:
        d = json.loads(state_path(name).read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(name: str, data: dict) -> None:
    """Atomic, chmod 600 (the file holds the SSO-minted token)."""
    p = state_path(name)
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".tok")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


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


def normalize_base(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    return url[: -len("/artifactory")] if url.endswith("/artifactory") else url


def load_instance(name: str | None = None) -> Instance:
    import yaml
    path = _config_path()
    try:
        doc = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise InstanceError(f"cannot read {path}: {exc}") from None
    insts = {n: c for n, c in (doc.get("integrations") or {}).items()
             if isinstance(c, dict) and c.get("driver") == "artifactory" and c.get("enabled", True)}
    name = name or os.environ.get("ARTIFACTORY_INSTANCE") or None
    if name is None:
        if len(insts) != 1:
            raise InstanceError("pass --instance <name>; enabled artifactory instances: "
                                + (", ".join(sorted(insts)) or "none"))
        name = next(iter(insts))
    cfg = insts.get(name)
    if cfg is None:
        raise InstanceError(f"artifactory instance {name!r} not found/enabled; known: "
                            + (", ".join(sorted(insts)) or "none"))
    base = normalize_base(str(cfg.get("base_url") or ""))
    if not base.startswith("https://") and not base.startswith("http://"):
        raise InstanceError(f"instance {name!r}: base_url (https://host) is required")
    auth = cfg.get("auth") or {}
    atype = str(auth.get("type") or "anonymous")
    token = ""
    if atype == "access_token":
        _load_dotenv()
        try:
            token = _resolve_ref(str(auth.get("token") or ""))
        except Exception as exc:
            raise InstanceError(f"instance {name!r}: cannot resolve auth.token ({type(exc).__name__})") from None
        if not token:
            raise InstanceError(f"instance {name!r}: auth.token is empty (env/file/keychain ref not set?)")
    elif atype == "sso_token":
        token = str(load_state(name).get("token") or "")
    return Instance(name, base, atype, token, dict(cfg.get("options") or {}))
