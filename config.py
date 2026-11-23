"""Single generic configuration for the whole agent.

Source of truth: ``~/.dop-agent/config.yaml`` (override the path with env
``DOP_AGENT_CONFIG``). Resolution order per setting: environment variable
(incl. ``.env``) > config.yaml > built-in default; the agent runs on env and
defaults alone when no config file exists. ``SETTINGS`` below is the ONE table
mapping every config path to its env var(s), type and default.

Secrets are never inline in the file: ``${env:NAME}``, ``${file:~/path}``
(must be chmod 600) or ``${keychain:service}`` (macOS). Secret settings are
returned as ``Secret`` (masked repr/str); use ``secret_value(path)`` or
``Secret.reveal()`` only at the point of use.

Reads are live (env is re-read on every call so tests can monkeypatch it);
the YAML file is cached until ``reload()``. An invalid file is logged and
ignored (the daemon must not crash-loop); ``errors()`` / ``python3 -m config
check`` report the problems.

File-permission hygiene: secret files (``${file:}``) are rejected when group/world
accessible; ``config.yaml`` (or the ``DOP_AGENT_CONFIG`` target) and the code-dir
``.env`` are checked with the same strictness (group/world-accessible or owned by
another user) but only WARN (logged + stderr) so a loose mode never locks the agent
out. Files are opened first and checked with ``fstat`` on the open fd (no
stat-then-read race).

``integrations`` is validated only as a generic envelope; driver-specific
checks plug in via ``register_integration_validator``.
"""

from __future__ import annotations

import logging
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable



def _loose_perm_problem(st, p) -> str:
    """Why `p` (with fstat/stat result `st`) is unsafe for secrets, or ''."""
    probs = []
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        probs.append("is group/world accessible (chmod 600)")
    if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
        probs.append("is owned by another user")
    return f"{p} " + " and ".join(probs) if probs else ""


def _warn_dotenv(p: Path) -> None:
    try:
        with open(p, "rb") as fh:
            msg = _loose_perm_problem(os.fstat(fh.fileno()), p)
    except OSError:
        return
    if msg:
        print(f"WARNING: dop-agent config: {msg}", file=sys.stderr)


try:
    from dotenv import load_dotenv

    _warn_dotenv(Path(__file__).parent / ".env")
    load_dotenv(Path(__file__).parent / ".env")
except ModuleNotFoundError:
    pass

try:
    import yaml
except ModuleNotFoundError:
    yaml = None

log = logging.getLogger("dop-agent.config")

CONFIG_VERSION = 1
MASK = "***"
_REF_RE = re.compile(r"^\$\{(env|file|keychain):([^}]+)\}$")
_SECRET_KEY_RE = re.compile(r"(^|_)(token|password|secret|api_key|key)$")


class ConfigError(ValueError):
    pass


class SecretError(ConfigError):
    pass


class Secret:
    """Opaque secret value: str()/repr() are masked, ``reveal()`` returns it."""

    __slots__ = ("_v",)

    def __init__(self, value: str = ""):
        self._v = value or ""

    def reveal(self) -> str:
        return self._v

    def __bool__(self) -> bool:
        return bool(self._v)

    def __str__(self) -> str:
        return MASK if self._v else ""

    def __repr__(self) -> str:
        return f"Secret({MASK})" if self._v else "Secret()"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and other._v == self._v

    def __hash__(self) -> int:
        return hash(self._v)


def is_ref(value: Any) -> bool:
    return isinstance(value, str) and bool(_REF_RE.match(value.strip()))


def resolve_ref(value: str) -> str:
    """Resolve one ``${scheme:arg}`` ref to its plaintext; non-refs pass through."""
    m = _REF_RE.match(value.strip()) if isinstance(value, str) else None
    if not m:
        return value
    scheme, arg = m.group(1), m.group(2).strip()
    if scheme == "env":
        return os.environ.get(arg, "").strip()
    if scheme == "file":
        return _read_secret_file(arg)
    return _keychain(arg)


def _read_secret_file(path: str) -> str:
    p = Path(path).expanduser()
    try:
        with open(p, encoding="utf-8") as fh:
            if os.fstat(fh.fileno()).st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                raise SecretError(f"{p} must not be group/world accessible (chmod 600)")
            return fh.read().strip()
    except SecretError:
        raise
    except OSError as e:
        raise SecretError(f"cannot read secret file {p}: {e.strerror or e}") from None


def _keychain(service: str) -> str:
    if sys.platform != "darwin":
        raise SecretError("keychain refs are only supported on macOS")
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", service, "-w"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        raise SecretError(f"keychain lookup failed for {service!r}: {e}") from None
    if r.returncode != 0:
        raise SecretError(f"keychain item {service!r} not found")
    return r.stdout.strip()


# --- the settings table ------------------------------------------------------

_TRUE = ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Setting:
    path: str
    env: tuple[str, ...]
    type: str  # str int float bool list secret
    default: Any = None
    optional: bool = False  # None allowed


def _s(path, env, type="str", default="", optional=False):
    return Setting(path, tuple(env.split()) if env else (), type, default, optional)


SETTINGS: list[Setting] = [
    _s("agent.workdir", "COPILOT_WORKDIR", default=""),  # "" -> $HOME
    _s("agent.session_timeout_s", "SESSION_TIMEOUT", "float", 120.0),
    _s("agent.startup_notify_channel", "STARTUP_NOTIFY_CHANNEL"),
    _s("agent.copilot.github_token", "COPILOT_GITHUB_TOKEN GITHUB_TOKEN", "secret"),
    _s("agent.model_routing.mode", "MODEL_ROUTING", default="on"),
    _s("agent.model_routing.cheap_model", "MODEL_ROUTING_CHEAP", default="claude-haiku-5.5"),
    _s("agent.model_routing.default_model", "MODEL_ROUTING_DEFAULT"),
    _s("agent.auto_title", "AUTO_TITLE", "bool", True),
    _s("agent.auto_title_model", "AUTO_TITLE_MODEL"),
    _s("agent.new_session_platform", "NEW_SESSION_PLATFORM"),
    _s("agent.usage_widget", "USAGE_WIDGET", "bool", True),
    _s("agent.quota_ttl_s", "QUOTA_TTL_SECONDS", "float", None, optional=True),
    _s("agent.table_render_mode", "TABLE_RENDER_MODE", default="image"),
    _s("agent.transcription.whisper_model", "WHISPER_MODEL", default="large"),
    _s("agent.transcription.audio_auto_send", "AUDIO_AUTO_SEND", "bool", True),
    _s("agent.transcription.audio_max_bytes", "AUDIO_MAX_BYTES", "int", 2 * 1024 * 1024 * 1024),
    _s("agent.transcription.audio_retention_days", "AUDIO_RETENTION_DAYS", "int", 7),
    _s("adapters.web.host", "WEB_UI_HOST", default="127.0.0.1"),
    _s("adapters.web.port", "WEB_UI_PORT", "int", 10818),
    _s("adapters.web.public_host", "WEB_UI_PUBLIC_HOST"),  # "" -> host
    _s("adapters.web.allowed_hosts", "WEB_UI_ALLOWED_HOSTS", "list", []),
    _s("adapters.web.frontend_enabled", "WEB_UI_FRONTEND_ENABLED", "bool", True),
    _s("adapters.web.password", "WEB_UI_PASSWORD", "secret"),
    _s("adapters.web.secret_key", "WEB_UI_SECRET_KEY", "secret"),
    _s("adapters.slack.enabled", "", "bool", True),
    _s("adapters.slack.bot_token", "SLACK_BOT_TOKEN", "secret"),
    _s("adapters.slack.app_token", "SLACK_APP_TOKEN", "secret"),
    _s("adapters.slack.allowed_user_id", "ALLOWED_USER_ID"),
    _s("adapters.telegram.enabled", "", "bool", True),  # a token is still required
    _s("adapters.telegram.bot_token", "TELEGRAM_BOT_TOKEN", "secret"),  # + TELEGRAM_BOT_TOKEN_FILE
    _s("adapters.telegram.allowed_user_ids", "TELEGRAM_ALLOWED_USER_IDS", "list", []),
    _s("adapters.telegram.forum_chat_id", "TELEGRAM_FORUM_CHAT_ID", "int", None, optional=True),
    _s("adapters.telegram.max_update_age_s", "TELEGRAM_MAX_UPDATE_AGE", "float", 600.0),
    _s("adapters.telegram.poll_timeout_s", "TELEGRAM_POLL_TIMEOUT", "float", 50.0),
    _s("mcp.auth_token", "MCP_AUTH_TOKEN", "secret"),
    _s("watchers.tick_s", "WATCHER_POLL_INTERVAL", "float", 60.0),
    _s("watchers.fetch_timeout_s", "WATCH_POLL_TIMEOUT", "float", 45.0),
    _s("watchers.session_turn.stale_after_days", "", "float", 7.0),
    _s("watchers.session_turn.default_max_chain", "", "int", 10),
    _s("watchers.coalesce_window_s", "", "float", 6.0),
    _s("watchers.builtin_types", "", "list", ["time", "self_restart", "session_turn", "script"]),
    _s("unfurl.cache_ttl_s", "", "float", 60.0),
]
_BY_PATH = {s.path: s for s in SETTINGS}
# Accepted but ignored in config files; they have no effect.
_RETIRED = {"adapters.web.enabled", "watchers.plugin_dirs", "unfurl.hide_delay_ms"}
_TOP_SECTIONS = {"version", "agent", "adapters", "mcp", "watchers", "unfurl", "integrations"}
DESCRIPTIONS = {
    "agent.workdir": "Working directory for Copilot sessions; empty means $HOME.",
    "agent.session_timeout_s": "Seconds a session turn may run before it is considered timed out.",
    "agent.startup_notify_channel": "Chat channel that receives a message when the daemon starts.",
    "agent.copilot.github_token": "GitHub token used to authenticate the Copilot SDK.",
    "agent.model_routing.mode": "Automatic model choice by task kind: on = route, dry = log only, off = always use the default.",
    "agent.model_routing.cheap_model": "Model used for cheap task kinds (research, summaries, polling).",
    "agent.model_routing.default_model": "Model used for regular tasks; empty uses the Copilot default.",
    "agent.auto_title": "Generate a short title for new sessions automatically.",
    "agent.auto_title_model": "Model used to generate session titles; empty uses the routing default.",
    "agent.new_session_platform": "Platform where new sessions are created by default (e.g. web or slack).",
    "agent.usage_widget": "Show the Copilot quota/usage widget in the Web UI.",
    "agent.quota_ttl_s": "How long (seconds) fetched quota data is cached; empty uses the built-in default.",
    "agent.table_render_mode": "How tables are rendered in chat: image (needs PyMuPDF/WeasyPrint) or ascii.",
    "agent.transcription.whisper_model": "Whisper model size used to transcribe audio (larger is slower but more accurate).",
    "agent.transcription.audio_auto_send": "Send the transcript to the agent automatically after transcription.",
    "agent.transcription.audio_max_bytes": "Maximum accepted size of an audio upload, in bytes.",
    "agent.transcription.audio_retention_days": "Days to keep uploaded audio files before cleanup.",
    "adapters.web.host": "Interface the Web UI listens on (127.0.0.1 = local only).",
    "adapters.web.port": "TCP port of the Web UI.",
    "adapters.web.public_host": "Host name used in generated links; empty uses the listen host.",
    "adapters.web.allowed_hosts": "Extra Host header values accepted by the Web UI (comma separated).",
    "adapters.web.frontend_enabled": "Serve the browser frontend; disable for API-only use.",
    "adapters.web.password": "Password required to log in to the Web UI.",
    "adapters.web.secret_key": "Secret used to sign Web UI session cookies.",
    "adapters.slack.enabled": "Run the Slack adapter.",
    "adapters.slack.bot_token": "Slack bot token (xoxb-...).",
    "adapters.slack.app_token": "Slack app-level token for Socket Mode (xapp-...).",
    "adapters.slack.allowed_user_id": "Slack user ID that is allowed to talk to the bot.",
    "adapters.telegram.enabled": "Run the Telegram adapter (also needs a bot token).",
    "adapters.telegram.bot_token": "Telegram bot token from BotFather.",
    "adapters.telegram.allowed_user_ids": "Telegram user IDs allowed to use the bot (comma separated).",
    "adapters.telegram.forum_chat_id": "Telegram forum chat used to host one topic per session.",
    "adapters.telegram.max_update_age_s": "Ignore Telegram updates older than this many seconds.",
    "adapters.telegram.poll_timeout_s": "Long-poll timeout for Telegram getUpdates, in seconds.",
    "mcp.auth_token": "Bearer token required by the local MCP server.",
    "watchers.tick_s": "How often the watcher loop checks for due watches, in seconds.",
    "watchers.fetch_timeout_s": "Upper bound for a single blocking fetch made by a watch poll.",
    "watchers.session_turn.stale_after_days": "Drop session_turn watches idle for more than this many days.",
    "watchers.session_turn.default_max_chain": "Default number of automatic resumes a session_turn watch may chain.",
    "watchers.coalesce_window_s": "Merge notifications for the same watch arriving within this window (seconds).",
    "watchers.builtin_types": "Watch types that need no integration.",
    "unfurl.cache_ttl_s": "How long link-hover card data is cached, in seconds.",
}
_VALID_ENUMS = {
    "agent.model_routing.mode": ("on", "dry", "off"),
    "agent.table_render_mode": ("image", "ascii"),
}


# --- value coercion ----------------------------------------------------------

def _coerce(s: Setting, raw: Any, from_env: bool) -> Any:
    if raw is None or (isinstance(raw, str) and not raw.strip() and s.type not in ("str", "bool")):
        return None
    t = s.type
    if t == "str" and isinstance(raw, bool):
        return "on" if raw else "off"  # YAML 1.1 parses bare on/off as booleans
    if t in ("str", "secret"):
        if isinstance(raw, (dict, list)):
            raise ConfigError("expected string")
        return str(raw).strip() if isinstance(raw, str) or from_env else str(raw)
    if t == "bool":
        if isinstance(raw, bool):
            return raw
        if from_env or isinstance(raw, str):
            return str(raw).strip().lower() in _TRUE
        raise ConfigError("expected boolean")
    if t in ("int", "float"):
        if isinstance(raw, bool):
            raise ConfigError(f"expected {t}")
        try:
            return int(raw) if t == "int" else float(raw)
        except (TypeError, ValueError):
            raise ConfigError(f"expected {t}, got {raw!r}") from None
    if t == "list":
        if isinstance(raw, str):
            return [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
        if isinstance(raw, list):
            return [str(p).strip() for p in raw if str(p).strip()]
        raise ConfigError("expected list")
    raise ConfigError(f"unknown type {t}")


# --- file model --------------------------------------------------------------

@dataclass
class Integration:
    name: str
    driver: str
    enabled: bool = True
    base_url: str = ""
    auth: dict = field(default_factory=dict)
    options: dict = field(default_factory=dict)
    kinds: dict = field(default_factory=dict)

    def resolved_auth(self) -> dict:
        """Auth dict with refs resolved; secret-looking keys wrapped in Secret."""
        out: dict = {}
        for k, v in self.auth.items():
            if isinstance(v, str):
                try:
                    val = resolve_ref(v)
                except SecretError as e:
                    log.warning("integration %s auth.%s: %s", self.name, k, e)
                    val = ""
                out[k] = Secret(val) if _SECRET_KEY_RE.search(k) else val
            else:
                out[k] = v
        return out

    def as_dict(self) -> dict:
        return {"driver": self.driver, "enabled": self.enabled, "base_url": self.base_url,
                "auth": dict(self.auth), "options": dict(self.options), "kinds": dict(self.kinds)}


_lock = threading.RLock()
_doc: dict | None = None  # raw parsed file ({} when absent/invalid)
_errors: list[str] = []
_integrations: dict[str, Integration] = {}
_loaded = False
_callbacks: list[Callable[[], None]] = []
_validators: list[Callable[[dict], list[str]]] = []


def config_path() -> Path:
    return Path(os.environ.get("DOP_AGENT_CONFIG") or "~/.dop-agent/config.yaml").expanduser()


def _walk(doc: dict, path: str) -> tuple[bool, Any]:
    cur: Any = doc
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


def _leaf_paths(doc: Any, prefix: str = "") -> list[tuple[str, Any]]:
    if isinstance(doc, dict) and not (prefix in _BY_PATH):
        out: list[tuple[str, Any]] = []
        for k, v in doc.items():
            out += _leaf_paths(v, f"{prefix}.{k}" if prefix else str(k))
        return out
    return [(prefix, doc)]


def validate(doc: Any) -> tuple[list[str], dict[str, Integration]]:
    """Validate a parsed document; return (errors, integrations)."""
    errs: list[str] = []
    ints: dict[str, Integration] = {}
    if doc is None:
        return errs, ints
    if not isinstance(doc, dict):
        return ["config root must be a mapping"], ints
    for k in doc:
        if k not in _TOP_SECTIONS:
            errs.append(f"unknown top-level key {k!r}")
    if "version" in doc and doc["version"] != CONFIG_VERSION:
        errs.append(f"unsupported version {doc['version']!r} (expected {CONFIG_VERSION})")
    for sec in ("agent", "adapters", "mcp", "watchers", "unfurl"):
        if sec in doc and not isinstance(doc[sec], dict):
            errs.append(f"{sec} must be a mapping")
            continue
        for path, val in _leaf_paths(doc.get(sec) or {}, sec):
            s = _BY_PATH.get(path)
            if s is None and path in _RETIRED:
                continue
            if s is None:
                errs.append(f"{path}: unknown setting")
                continue
            if val is None and (s.optional or s.type in ("str", "secret", "list")):
                continue
            try:
                if s.type == "secret":
                    if not isinstance(val, str):
                        raise ConfigError("expected a ${env|file|keychain:...} reference")
                    if val.strip() and not is_ref(val):
                        raise ConfigError("secrets must be refs (${env:NAME}, ${file:path}, ${keychain:service}), never inline")
                else:
                    v = _coerce(s, val, False)
                    en = _VALID_ENUMS.get(path)
                    if en and str(v).lower() not in en:
                        raise ConfigError(f"must be one of {', '.join(en)}")
            except ConfigError as e:
                errs.append(f"{path}: {e}")
    raw = doc.get("integrations")
    if raw is not None and not isinstance(raw, dict):
        errs.append("integrations must be a mapping")
        raw = {}
    for name, body in (raw or {}).items():
        where = f"integrations.{name}"
        if not isinstance(name, str) or not name.strip():
            errs.append("integration names must be non-empty strings")
            continue
        if not isinstance(body, dict):
            errs.append(f"{where}: must be a mapping")
            continue
        for k in body:
            if k not in ("driver", "enabled", "base_url", "auth", "options", "kinds"):
                errs.append(f"{where}.{k}: unknown key")
        driver = body.get("driver")
        if not isinstance(driver, str) or not driver.strip():
            errs.append(f"{where}.driver: required string")
            continue
        if not isinstance(body.get("enabled", True), bool):
            errs.append(f"{where}.enabled: expected boolean")
        if not isinstance(body.get("base_url", ""), str):
            errs.append(f"{where}.base_url: expected string")
        for k in ("auth", "options", "kinds"):
            if not isinstance(body.get(k) or {}, dict):
                errs.append(f"{where}.{k}: expected mapping")
        auth = body.get("auth") or {}
        if isinstance(auth, dict):
            for k, v in auth.items():
                if isinstance(v, str) and v.strip() and _SECRET_KEY_RE.search(str(k)) and not is_ref(v):
                    errs.append(f"{where}.auth.{k}: secrets must be refs, never inline")
        if any(e.startswith(where) for e in errs):
            continue
        ints[name] = Integration(name, driver.strip(), bool(body.get("enabled", True)),
                                 (body.get("base_url") or "").strip(), dict(auth),
                                 dict(body.get("options") or {}), dict(body.get("kinds") or {}))
    for v in list(_validators):
        try:
            errs += [str(e) for e in (v(ints) or [])]
        except Exception as e:  # a buggy validator must not break config loading
            errs.append(f"integration validator {getattr(v, '__name__', v)} failed: {e}")
    return errs, ints


def load() -> None:
    global _doc, _errors, _integrations, _loaded
    path = config_path()
    doc: dict = {}
    errs: list[str] = []
    ints: dict[str, Integration] = {}
    if path.exists():
        try:
            if yaml is None:
                raise ConfigError("PyYAML is not installed")
            with open(path, encoding="utf-8") as fh:
                warn = _loose_perm_problem(os.fstat(fh.fileno()), path)
                text = fh.read()
            if warn:  # warn-and-continue: a hard fail would lock the user out of the agent
                errs.append(warn)
            parsed = yaml.safe_load(text)
            errs2, ints = validate(parsed)
            errs += errs2
            doc = parsed if isinstance(parsed, dict) else {}
            if errs2:
                doc, ints = {}, {}
        except Exception as e:
            errs.append(f"cannot load {path}: {e}")
            doc, ints = {}, {}
        for e in errs:
            log.error("config: %s", e)
    with _lock:
        _doc, _errors, _integrations, _loaded = doc, errs, ints, True


def _ensure() -> None:
    if not _loaded:
        load()


def reload() -> None:
    load()
    for cb in list(_callbacks):
        try:
            cb()
        except Exception:
            log.exception("config change callback failed")


def on_change(fn: Callable[[], None]) -> Callable[[], None]:
    _callbacks.append(fn)
    return fn


def register_integration_validator(fn: Callable[[dict], list[str]]) -> None:
    """fn(integrations: dict[str, Integration]) -> list of error strings."""
    _validators.append(fn)
    if _loaded:
        load()


def errors() -> list[str]:
    _ensure()
    return list(_errors)


# --- accessors ---------------------------------------------------------------

def _resolve(path: str) -> tuple[Any, str]:
    """-> (value, source) where source is env:NAME / file / default. Secrets: (Secret, source)."""
    _ensure()
    s = _BY_PATH.get(path)
    if s is None:
        raise KeyError(path)
    if s.type == "secret":
        return _resolve_secret(s)
    for name in s.env:
        raw = os.environ.get(name)
        if raw is not None and (raw.strip() or s.type == "bool"):
            try:
                return _coerce(s, raw, True), f"env:{name}"
            except ConfigError:
                log.warning("ignoring invalid %s", name)
    ok, raw = _walk(_doc or {}, path)
    if ok and raw is not None and not (isinstance(raw, str) and not raw.strip() and s.type != "str"):
        try:
            v = _coerce(s, raw, False)
            if v is not None or s.optional:
                return v, "file"
        except ConfigError:
            pass
    return s.default, "default"


def _resolve_secret(s: Setting) -> tuple[Secret, str]:
    for name in s.env:
        raw = os.environ.get(name)
        if raw is not None and raw.strip():
            return Secret(raw.strip()), f"env:{name}"
    fname = f"{s.env[0]}_FILE" if s.env else ""
    if fname and os.environ.get(fname, "").strip():
        try:
            return Secret(_read_secret_file(os.environ[fname])), f"env:{fname}"
        except SecretError as e:
            log.warning("%s: %s", fname, e)
            return Secret(), f"env:{fname}"
    ok, raw = _walk(_doc or {}, s.path)
    if ok and isinstance(raw, str) and raw.strip():
        try:
            return Secret(resolve_ref(raw)), "file"
        except SecretError as e:
            log.warning("%s: %s", s.path, e)
            return Secret(), "file"
    return Secret(), "default"


def value(path: str) -> Any:
    """Effective value (Secret for secret settings)."""
    return _resolve(path)[0]


def source(path: str) -> str:
    return _resolve(path)[1]


def secret_value(path: str) -> str:
    """Plaintext of a secret setting ('' if unset/unresolvable). Use only at point of use."""
    v = value(path)
    return v.reveal() if isinstance(v, Secret) else str(v or "")


def _str(path: str) -> str:
    return str(value(path) or "")


def workdir() -> str:
    return _str("agent.workdir") or str(Path.home())


def copilot_github_token() -> str:
    return secret_value("agent.copilot.github_token")


def integrations() -> dict[str, Integration]:
    _ensure()
    return dict(_integrations)


class _Facade:
    @property
    def integrations(self) -> dict[str, Integration]:
        return integrations()

    def __getattr__(self, name):
        return getattr(sys.modules[__name__], name)


def get() -> _Facade:
    """Handle exposing ``.integrations`` plus every module-level function."""
    _ensure()
    return _Facade()


# --- dump / save -------------------------------------------------------------

def _nest(flat: dict[str, Any]) -> dict:
    root: dict = {}
    for path, v in flat.items():
        cur = root
        parts = path.split(".")
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = v
    return root


def dump(redact: bool = True) -> dict:
    """redact=True: effective config (env > file > default), secrets masked.
    redact=False: the stored document as written (refs verbatim, no defaults)."""
    _ensure()
    if not redact:
        import copy

        return copy.deepcopy(_doc or {})
    flat: dict[str, Any] = {"version": CONFIG_VERSION}
    for s in SETTINGS:
        v, _ = _resolve(s.path)
        flat[s.path] = (MASK if v else "") if isinstance(v, Secret) else v
    out = _nest(flat)
    out["integrations"] = {}
    for n, i in _integrations.items():
        d = i.as_dict()
        d["auth"] = {k: (MASK if isinstance(v, str) and v and _SECRET_KEY_RE.search(k) else v)
                     for k, v in d["auth"].items()}
        out["integrations"][n] = d
    return out


def _restore_masked(new: Any, old: Any) -> Any:
    if isinstance(new, dict):
        o = old if isinstance(old, dict) else {}
        return {k: _restore_masked(v, o.get(k)) for k, v in new.items()}
    if new == MASK and isinstance(old, str):
        return old
    return new


def save(new_dict: dict) -> None:
    """Validate and atomically write config (chmod 600); then reload + fire callbacks.
    Masked placeholders ('***') keep the previously stored ref; inline secrets are rejected."""
    if yaml is None:
        raise ConfigError("PyYAML is not installed")
    _ensure()
    merged = _restore_masked(new_dict, _doc or {})
    merged.setdefault("version", CONFIG_VERSION)
    errs, _ = validate(merged)
    if errs:
        raise ConfigError("; ".join(errs))
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(merged, f, sort_keys=False, default_flow_style=False, allow_unicode=True)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    reload()


# --- CLI ---------------------------------------------------------------------

def _cli(argv: list[str]) -> int:
    if not argv or argv[0] != "check":
        print("usage: python3 -m config check", file=sys.stderr)
        return 2
    load()
    path = config_path()
    print(f"config file: {path} ({'present' if path.exists() else 'absent -> defaults/env only'})")
    for s in SETTINGS:
        v, src = _resolve(s.path)
        shown = (MASK if v else "(unset)") if isinstance(v, Secret) else repr(v)
        print(f"  {s.path:<48} {shown:<28} [{src}]")
    for n, i in _integrations.items():
        auth = {k: (MASK if isinstance(v, str) and v and _SECRET_KEY_RE.search(k) else v) for k, v in i.auth.items()}
        print(f"  integrations.{n}: driver={i.driver} enabled={i.enabled} base_url={i.base_url} auth={auth}")
    errs = errors()
    for e in errs:
        print(f"ERROR: {e}", file=sys.stderr)
    print("OK" if not errs else f"{len(errs)} error(s)")
    return 1 if errs else 0


if __name__ == "__main__":
    import config as _canonical

    sys.exit(_canonical._cli(sys.argv[1:]))
