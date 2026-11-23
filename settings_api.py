"""Settings REST API (backend for the Web UI Settings view).

Mounted from web_server.py (`app.include_router(settings_api.router)`). Logic lives on top of
`config.py` (YAML + secret refs) and `integrations.registry` (driver catalog); this module never
reads secrets except to (a) test a connection and (b) build the response scrubber.

Security model (all endpoints)
  * Auth cookie required (same as every /api route).
  * POST/PATCH/DELETE additionally need header `X-Requested-With: dop` and, when an `Origin`
    header is present, its host must be the request Host or in ALLOWED_HOSTS (else 403).
  * NO response, error message or log line contains a secret value. Responses only carry refs
    (`${env:X}`, `${file:~/..}`, `${keychain:svc}`) and `state`. As defence in depth every JSON
    body is scrubbed: any known resolved secret (config secrets, integration auth, and values
    submitted in the request) found in a string is replaced by "***".
  * Secret `value`s are accepted only from loopback clients and are write-only.

Error format (FastAPI compatible: UI reads `detail`)
    {"detail": "Validation failed", "errors": [{"path": "adapters.web.port", "code": "invalid_type",
      "message": "expected int"}]}
  400 bad request | 403 CSRF/origin/remote-secret | 409 revision mismatch or unwritable/invalid file
  (carries `revision`, `file_errors`) | 422 validation | 401 no cookie.
  Error codes: unknown_setting invalid_type invalid_choice inline_secret invalid secret_value_remote
  secret_invalid secret_file_missing secret_write_failed name_invalid exists not_found driver_unknown
  invalid_config timeout connection_failed.

Revision: sha256(file bytes)[:16], "0" when no file. Send it back as `revision` in write bodies
(optional; when given and stale -> 409 {revision: <current>}).

Secret input (settings values for secret paths and integration auth fields):
    "${env:NAME}" | "${file:~/p}" | "${keychain:svc}"      existing ref string
    {"mode":"env","env":"NAME"}
    {"mode":"ref","ref":"${...}"}
    {"mode":"file","value":"..","name":"optional"}          writes $DOP_AGENT_SECRETS_DIR (default
                                                            ~/.dop-agent/secrets)/<name> chmod 600 (dir 700)
    {"mode":"file","name":"existing"}                       reference an already existing secret file
    {"mode":"keychain","value":"..","service":"optional"}   macOS `security add-generic-password`
    null                                                    remove the key (back to env/default)
  A plain non-ref string is rejected (inline_secret).

Endpoints
  GET  /api/settings                -> {revision, config_path, file_present, mode_ok, file_errors[],
        restart_pending[{path,reason}], settings:{path:{value|null, default, source:"env"|"file"|"default",
        env_var?, type, secret, set?, ref?, state?, restart_required, reason?, env_vars[]}}}
        e.g. "agent.session_timeout_s": {"value":120.0,"source":"file","type":"float","restart_required":false,...}
             "adapters.slack.bot_token": {"value":null,"secret":true,"set":true,"source":"env","env_var":
             "SLACK_BOT_TOKEN","ref":null,"state":"set","restart_required":true,...}
  GET  /api/settings/schema         -> JSON-Schema-like tree (x-env, x-secret, x-restart-required,
        x-section) + config_revision
  POST /api/settings/validate       body {revision?, changes:{path:value}} -> {valid, errors[], applied[],
        restart_required[]} (never writes anything, not even secret files)
  PATCH /api/settings               same body -> {revision, applied[], restart_required[{path,reason}],
        env_overrides[{path,env_var}] (env still wins over the file!), integrations_reloaded[]}
  GET  /api/settings/yaml           -> {yaml, revision, mode_ok, file_present, error?} refs verbatim, inline
        secret-looking values masked
  POST /api/settings/import-env     {dry_run:true, env_path?, secret_handling:{VAR:"env"|"file"|"keychain"}}
        -> {mappings:[{var,path,secret,action:"set"|"unchanged"|"error",preview,shadowed_by_env,
        message?}], unmapped:[VAR], revision}
  POST /api/settings/restart        {confirm:true} -> {restarting, mode}; process exits/re-execs ~1s later
  POST /api/settings/reload         -> {revision, file_errors[], restart_required[], integrations_reloaded[]}
  GET  /api/drivers                 -> registry.catalog() (auth types/fields with secret flag, options,
        kinds, settings) + `secret_modes`
  GET  /api/integrations            -> [{name, driver, title, enabled, valid, errors[], base_url,
        auth:{type, fields:{f:{secret, required, ref?, state?, value?}}}, options, kinds, status:{state,
        detail, checked_at}, watch_count}]   state of a secret field: set | unset | bad_mode
  POST /api/integrations            {name, driver, enabled?, base_url, auth:{type, fields:{token:<secret input>}},
        options?, kinds?, revision?} -> 201 integration object
  PATCH /api/integrations/{name}    partial: enabled, base_url, auth{type?,fields{}}, options (null removes a key),
        kinds, secret:{field,mode,value} -> integration object + restart/reload info
  DELETE /api/integrations/{name}?confirm=<name> -> {paused_watches:n, revision}
  POST /api/integrations/test       {name} (saved) | draft as in create (omitted secret fields are inherited
        from a saved integration with the same name) -> 200 {ok, identity?, latency_ms, error?:{code,message}};
        nothing persisted; 15 s timeout
  Status rule (`status.state`: ok|error|checking|unknown|invalid|disabled): a test result is recorded in
        the in-memory status of the saved integration `name` when the test is by name only, or when the
        submitted draft is identical to the saved config (the wizard always sends the full draft incl.
        `driver`). A draft that differs from the saved config describes the draft, so it is returned but
        never overwrites the saved status. Saving (POST/PATCH) resets the status. GET /api/integrations
        starts non-blocking background tests (<=BG_WORKERS parallel, TEST_TIMEOUT_S each, at most one
        per integration per BG_THROTTLE_S) for enabled+valid integrations whose status is unknown, stale
        (older than STATUS_TTL_S) or recorded for a different config; while it runs the state is
        `checking` (the UI re-fetches until it settles).
  GET  /api/adapters                -> {adapters:[{id, title, enabled, configured, running, valid, errors[], required[],
        fields:{f:<setting view>}, restart_pending[], status:{state, detail}}], revision}
        (Slack/Telegram; manifests in adapter_catalog.py; fields map to adapters.<id>.*)
  PATCH /api/adapters/{id}          {enabled?, fields:{f:value|<secret input>}, revision?} -> as PATCH /api/settings
        + adapter view (adapter changes are restart-class)
  POST /api/adapters/test           {id, fields?} -> {ok, identity?, latency_ms, error?}; omitted fields use the
        effective saved value; the result is recorded as the adapter status only when no draft fields are sent
  GET  /api/watch-types             -> {registry:{type:{...watch_type_meta, integrations[]}}, builtin:[..]}

Hot vs restart: settings apply on save (config.reload() fires the change callbacks) except the
restart-class ones (`restart_required`): adapters.*, mcp.*, agent.copilot.*, and the paths in
_RESTART_PATHS (agent.workdir, agent.session_timeout_s, agent.table_render_mode, some
agent.transcription.* and watchers.* values). `restart_pending` in GET lists restart-class settings
whose effective value differs from the value the process started with.
"""
from __future__ import annotations

import concurrent.futures
import copy
import functools
import hashlib
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

import config
import adapter_catalog
from integrations.registry import build_registry

try:
    import yaml
except ModuleNotFoundError:
    yaml = None

log = logging.getLogger("dop-agent.settings_api")

MASK = config.MASK
TEST_TIMEOUT_S = 15
PACKAGE_TEST_TIMEOUT_S = 70  # package CLIs may reload the browser tab to renew a token (CLI itself caps at 60s)
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SECRET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LOCAL_CLIENTS = {"127.0.0.1", "::1", "localhost", "testclient"}
_WRITE_LOCK = threading.RLock()
_STATUS: dict[str, dict] = {}
_STATUS_FP: dict[str, str] = {}
_CHECKING: set[str] = set()
_LAST_ATTEMPT: dict[str, float] = {}
_BG_LOCK = threading.Lock()
_BG_POOL: concurrent.futures.ThreadPoolExecutor | None = None
BG_WORKERS = 3
BG_THROTTLE_S = 60
STATUS_TTL_S = 300
_RESTART_PREFIXES = ("adapters.", "mcp.", "agent.copilot.")
_RESTART_PATHS = {
    "agent.workdir", "agent.startup_notify_channel", "agent.session_timeout_s", "agent.table_render_mode",
    "agent.transcription.whisper_model", "agent.transcription.audio_auto_send",
    "agent.transcription.audio_max_bytes", "agent.transcription.audio_retention_days",
    "watchers.tick_s", "watchers.fetch_timeout_s",
    "watchers.coalesce_window_s", "watchers.session_turn.stale_after_days", "watchers.session_turn.default_max_chain",
}
_SECRET_MODES = ["env", "file", "keychain", "ref"]

router = APIRouter()


class _Fail(Exception):
    def __init__(self, status: int, detail: str, errors: list | None = None, **extra):
        self.status, self.detail, self.errors, self.extra = status, detail, errors or [], extra


def _err(path: str, code: str, message: str) -> dict:
    return {"path": path, "code": code, "message": message}


# --- guards / responses --------------------------------------------------------

def _auth(request: Request) -> None:
    import web_server

    web_server._require_auth(request)


def _guard_write(request: Request) -> None:
    _auth(request)
    import web_server

    if request.headers.get("x-requested-with", "").lower() != "dop":
        raise HTTPException(403, "Missing X-Requested-With: dop header")
    origin = request.headers.get("origin")
    if origin:
        netloc = origin.split("://", 1)[-1].split("/", 1)[0].lower()
        host = request.headers.get("host", "").lower()
        if netloc != host and netloc not in web_server.ALLOWED_HOSTS:
            raise HTTPException(403, "Origin not allowed")


def _is_local(request: Request) -> bool:
    return bool(request.client and request.client.host in _LOCAL_CLIENTS)


_SHORT_SECRET_LEN = 4


def _known_secrets(extra: list[str] | None = None) -> set[str]:
    out = {s for s in (extra or []) if isinstance(s, str) and s}
    try:
        for s in config.SETTINGS:
            if s.type == "secret":
                v = config.value(s.path)
                if v and v.reveal():
                    out.add(v.reveal())
        for i in config.integrations().values():
            for k, v in i.resolved_auth().items():
                if isinstance(v, config.Secret) and v and v.reveal():
                    out.add(v.reveal())
    except Exception:
        log.warning("scrubber: could not collect all secrets")
    return out


def _scrub(obj: Any, secrets: set[str]) -> Any:
    if isinstance(obj, str):
        for s in secrets:
            if len(s) < _SHORT_SECRET_LEN:
                # very short secrets would mangle ordinary text: only mask whole tokens
                obj = re.sub(r"(?<!\w)" + re.escape(s) + r"(?!\w)", MASK, obj)
            elif s in obj:
                obj = obj.replace(s, MASK)
        return obj
    if isinstance(obj, dict):
        return {k: _scrub(v, secrets) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub(v, secrets) for v in obj]
    return obj


def _json(data: Any, status: int = 200, secrets: list[str] | None = None) -> JSONResponse:
    return JSONResponse(_scrub(data, _known_secrets(secrets)), status_code=status)


def _api(fn):
    """Convert _Fail into the documented error body (scrubbed)."""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except _Fail as f:
            return _fail_with(f, None)
        except Exception as exc:
            log.error("settings api %s failed: %s", fn.__name__, type(exc).__name__)
            return _json({"detail": "Internal error"}, 500)
    return wrapper


def _fail_with(f: _Fail, secrets):
    body = {"detail": f.detail, **f.extra}
    if f.errors:
        body["errors"] = f.errors
    return _json(body, f.status, secrets=secrets)


# --- config file state -----------------------------------------------------------

def _revision(raw: bytes | None) -> str:
    return "0" if raw is None else hashlib.sha256(raw).hexdigest()[:16]


def _read_state() -> dict:
    path = config.config_path()
    st: dict = {"path": path, "exists": path.exists(), "raw": None, "doc": {}, "errors": [], "mode_ok": True}
    if not st["exists"]:
        st["revision"] = "0"
        return st
    try:
        st["raw"] = path.read_bytes()
        st["mode_ok"] = not (path.stat().st_mode & 0o077)
    except OSError as e:
        st["errors"].append(f"cannot read config file: {e.strerror}")
        st["revision"] = "0"
        return st
    st["revision"] = _revision(st["raw"])
    if yaml is None:
        st["errors"].append("PyYAML is not installed")
        return st
    try:
        doc = yaml.safe_load(st["raw"])
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark else ""
        st["errors"].append(f"YAML syntax error{where}: {getattr(e, 'problem', 'invalid')}")
        return st
    errs, _ = config.validate(doc)
    st["errors"] += errs
    st["doc"] = doc if isinstance(doc, dict) else {}
    return st


def _file_errors(st: dict) -> list[str]:
    return list(st["errors"])


# --- helpers: paths / types -------------------------------------------------------

def _restart_reason(path: str) -> str | None:
    if path in _RESTART_PATHS or path.startswith(_RESTART_PREFIXES):
        return "read once at daemon startup"
    return None


def _set_path(doc: dict, path: str, val: Any) -> None:
    cur = doc
    parts = path.split(".")
    for p in parts[:-1]:
        if not isinstance(cur.get(p), dict):
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = val


def _unset_path(doc: dict, path: str) -> None:
    parts = path.split(".")
    chain = [doc]
    for p in parts[:-1]:
        nxt = chain[-1].get(p)
        if not isinstance(nxt, dict):
            return
        chain.append(nxt)
    chain[-1].pop(parts[-1], None)
    for i in range(len(parts) - 1, 0, -1):
        if not chain[i]:
            chain[i - 1].pop(parts[i - 1], None)


def _typecheck(s: config.Setting, val: Any) -> str | None:
    t = s.type
    if t == "str":
        return None if isinstance(val, str) else "expected string"
    if t == "bool":
        return None if isinstance(val, bool) else "expected boolean"
    if t == "int":
        return None if isinstance(val, int) and not isinstance(val, bool) else "expected integer"
    if t == "float":
        return None if isinstance(val, (int, float)) and not isinstance(val, bool) else "expected number"
    if t == "list":
        ok = isinstance(val, list) and all(isinstance(x, (str, int)) and not isinstance(x, bool) for x in val)
        return None if ok else "expected list of strings"
    return None


def _classify(msg: str) -> str:
    m = msg.lower()
    if "unknown setting" in m or "unknown key" in m:
        return "unknown_setting"
    if "must be one of" in m:
        return "invalid_choice"
    if "inline" in m:
        return "inline_secret"
    if "expected" in m:
        return "invalid_type"
    return "invalid"


def _parse_errors(errs: list[str]) -> list[dict]:
    out = []
    for e in errs:
        path, _, msg = e.partition(": ")
        if not msg or " " in path:
            path, msg = "", e
        out.append(_err(path, _classify(msg), msg))
    return out


# --- secrets ----------------------------------------------------------------------

def _secrets_dir() -> Path:
    return Path(os.environ.get("DOP_AGENT_SECRETS_DIR") or "~/.dop-agent/secrets").expanduser()


def _display_path(p: Path) -> str:
    home = Path.home()
    try:
        return "~/" + str(p.relative_to(home))
    except ValueError:
        return str(p)


def _ref_state(ref: str | None) -> str:
    m = config._REF_RE.match((ref or "").strip())
    if not m:
        return "unset"
    scheme, arg = m.group(1), m.group(2).strip()
    try:
        if scheme == "env":
            return "set" if os.environ.get(arg, "").strip() else "unset"
        if scheme == "file":
            p = Path(arg).expanduser()
            if not p.exists():
                return "unset"
            if p.stat().st_mode & 0o077:
                return "bad_mode"
            return "set" if p.stat().st_size else "unset"
        return "set" if config.resolve_ref(ref) else "unset"
    except Exception:
        return "unset"


class _SecretCtx:
    def __init__(self, local: bool):
        self.local = local
        self.writes: list[tuple[str, str, str]] = []
        self.plain: list[str] = []
        self.errors: list[dict] = []


def _secret_input(ctx: _SecretCtx, path: str, inp: Any, default_name: str) -> tuple[bool, str | None, str | None]:
    """-> (keep, ref, plaintext_for_test). keep=False means remove the key."""
    if inp is None or inp == "":
        return False, None, None
    if isinstance(inp, str):
        if config.is_ref(inp):
            return True, inp.strip(), _try_resolve(inp)
        ctx.errors.append(_err(path, "inline_secret",
                               'secrets must be refs; send {"mode":"file","value":...} to store a value'))
        return True, None, None
    if not isinstance(inp, dict) or "mode" not in inp:
        ctx.errors.append(_err(path, "invalid_type", "expected ref string or {mode, ...}"))
        return True, None, None
    mode = inp.get("mode")
    if mode == "env":
        name = str(inp.get("env") or "")
        if not _ENV_NAME_RE.match(name):
            ctx.errors.append(_err(path, "invalid", "env must be a valid variable name"))
            return True, None, None
        return True, f"${{env:{name}}}", os.environ.get(name, "").strip()
    if mode == "ref":
        ref = str(inp.get("ref") or "").strip()
        if not config.is_ref(ref):
            ctx.errors.append(_err(path, "invalid", "ref must be ${env:..}, ${file:..} or ${keychain:..}"))
            return True, None, None
        return True, ref, _try_resolve(ref)
    if mode in ("file", "keychain"):
        value = inp.get("value")
        if value is not None:
            if not isinstance(value, str) or not value.strip():
                ctx.errors.append(_err(path, "invalid", "value must be a non-empty string"))
                return True, None, None
            if not ctx.local:
                ctx.errors.append(_err(path, "secret_value_remote", "secret values are accepted from localhost only"))
                return True, None, None
        if mode == "file":
            name = str(inp.get("name") or default_name)
            if not _SECRET_NAME_RE.match(name):
                ctx.errors.append(_err(path, "name_invalid", "secret file name may contain letters, digits . _ -"))
                return True, None, None
            ref = f"${{file:{_display_path(_secrets_dir() / name)}}}"
            if value is None:
                if not (_secrets_dir() / name).exists():
                    ctx.errors.append(_err(path, "secret_file_missing", f"secret file {name!r} does not exist"))
                return True, ref, _try_resolve(ref)
            ctx.writes.append(("file", name, value.strip()))
            ctx.plain.append(value.strip())
            return True, ref, value.strip()
        service = str(inp.get("service") or f"dop-agent.{default_name}")
        if "}" in service or not service.strip():
            ctx.errors.append(_err(path, "invalid", "invalid keychain service"))
            return True, None, None
        ref = f"${{keychain:{service}}}"
        if value is None:
            return True, ref, _try_resolve(ref)
        if sys.platform != "darwin":
            ctx.errors.append(_err(path, "invalid", "keychain is only available on macOS"))
            return True, None, None
        ctx.writes.append(("keychain", service, value.strip()))
        ctx.plain.append(value.strip())
        return True, ref, value.strip()
    ctx.errors.append(_err(path, "invalid", f"unknown secret mode {mode!r} (use {_SECRET_MODES})"))
    return True, None, None


def _try_resolve(ref: str) -> str:
    try:
        return config.resolve_ref(ref)
    except Exception:
        return ""


def _apply_writes(writes: list[tuple[str, str, str]]) -> list:
    """Write secret material; returns undo callables. Raises _Fail on error (after undoing)."""
    undo: list = []
    try:
        for kind, name, value in writes:
            if kind == "file":
                undo.append(_write_secret_file(name, value))
            else:
                r = subprocess.run(["security", "add-generic-password", "-U", "-a", "dop-agent", "-s", name,
                                    "-w", value], capture_output=True, text=True, timeout=15)
                if r.returncode != 0:
                    raise OSError("keychain write failed")
    except Exception as e:
        for u in reversed(undo):
            u()
        msg = e.strerror if isinstance(e, OSError) and e.strerror else "could not store secret"
        raise _Fail(500, "Could not store secret", [_err("", "secret_write_failed", str(msg))]) from None
    return undo


def _write_secret_file(name: str, value: str):
    d = _secrets_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    if d.stat().st_mode & 0o077:
        raise OSError(f"{d} must be chmod 700")
    target = d / name
    prev = target.read_bytes() if target.exists() else None
    fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".sec.")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(value)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    def undo():
        try:
            if prev is None:
                target.unlink(missing_ok=True)
            else:
                target.write_bytes(prev)
                os.chmod(target, 0o600)
        except OSError:
            pass
    return undo


# --- commit pipeline ----------------------------------------------------------------

def _commit(mutator, revision: str | None, *, dry: bool, local: bool):
    """mutator(doc, ctx) mutates a deep copy of the stored doc and returns an info dict."""
    ctx = _SecretCtx(local)
    with _WRITE_LOCK:
        st = _read_state()
        if st["errors"]:
            raise _Fail(409, "Config file has errors; fix it first", revision=st["revision"],
                        file_errors=_file_errors(st))
        if revision is not None and revision != st["revision"]:
            raise _Fail(409, "Config changed since it was loaded", revision=st["revision"])
        doc = copy.deepcopy(st["doc"] or {})
        info = mutator(doc, ctx) or {}
        errors = ctx.errors + info.pop("errors", [])
        if not errors:
            doc.setdefault("version", config.CONFIG_VERSION)
            errors = _parse_errors(config.validate(doc)[0])
        if errors:
            if dry:
                return {"valid": False, "errors": errors, **info}
            raise _Fail(422, "Validation failed", errors)
        if dry:
            return {"valid": True, "errors": [], **info}
        path = st["path"]
        if st["raw"] is not None:
            bak = path.with_name(path.name + ".bak")
            bak.write_bytes(st["raw"])
            os.chmod(bak, 0o600)
        undo = _apply_writes(ctx.writes)
        try:
            config.save(doc)
        except config.ConfigError as e:
            for u in reversed(undo):
                u()
            raise _Fail(422, "Validation failed", _parse_errors(str(e).split("; "))) from None
        except Exception:
            for u in reversed(undo):
                u()
            raise
        new = _read_state()
        return {"valid": True, "revision": new["revision"], **info}


def _snapshot() -> dict[str, str]:
    out = {}
    for s in config.SETTINGS:
        if _restart_reason(s.path):
            v = config.value(s.path)
            out[s.path] = hashlib.sha256((v.reveal() if isinstance(v, config.Secret) else repr(v)).encode()).hexdigest()
    return out


try:
    _BASELINE = _snapshot()
except Exception:
    _BASELINE = {}


def _pending() -> list[dict]:
    now = _snapshot()
    return [{"path": p, "reason": _restart_reason(p)} for p, h in now.items() if _BASELINE.get(p) != h]


def _env_overrides(paths: list[str]) -> list[dict]:
    out = []
    for p in paths:
        s = config._BY_PATH.get(p)
        if s:
            for v in s.env:
                if os.environ.get(v, "").strip():
                    out.append({"path": p, "env_var": v})
                    break
    return out


def _change_info(paths: list[str]) -> dict:
    return {"applied": paths,
            "restart_required": [{"path": p, "reason": r} for p in paths if (r := _restart_reason(p))],
            "env_overrides": _env_overrides(paths)}


# --- settings -----------------------------------------------------------------------

def _setting_view(s: config.Setting, raw_doc: dict) -> dict:
    v, src = config._resolve(s.path)
    kind, _, env_var = src.partition(":")
    d: dict = {"type": s.type, "source": kind, "secret": s.type == "secret", "env_vars": list(s.env),
               "restart_required": bool(_restart_reason(s.path))}
    if env_var:
        d["env_var"] = env_var
    if d["restart_required"]:
        d["reason"] = _restart_reason(s.path)
    if s.type == "secret":
        ok, ref = config._walk(raw_doc, s.path)
        ref = ref if (ok and isinstance(ref, str) and ref.strip()) else None
        d.update({"value": None, "set": bool(v), "ref": ref if kind == "file" else None,
                  "state": _ref_state(ref) if kind == "file" else ("set" if v else "unset")})
        if kind == "file" and ref and not v and d["state"] == "set":
            d["state"] = "bad_mode"
    else:
        d.update({"value": v, "default": s.default})
    return d


@router.get("/api/settings")
@_api
def get_settings(request: Request, _a=Depends(_auth)):
    st = _read_state()
    raw = st["doc"] if not st["errors"] else {}
    return _json({
        "revision": st["revision"], "config_path": str(st["path"]), "file_present": st["exists"],
        "mode_ok": st["mode_ok"], "file_errors": _file_errors(st), "restart_pending": _pending(),
        "settings": {s.path: _setting_view(s, raw) for s in config.SETTINGS},
    })


_JSON_TYPES = {"str": "string", "int": "integer", "float": "number", "bool": "boolean", "secret": "string"}


@router.get("/api/settings/schema")
@_api
def get_schema(request: Request, _a=Depends(_auth)):
    root: dict = {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object", "properties": {},
                  "x-integrations": "/api/drivers"}
    for s in config.SETTINGS:
        parts = s.path.split(".")
        cur = root
        for p in parts[:-1]:
            cur = cur["properties"].setdefault(p, {"type": "object", "properties": {}})
        if s.type == "list":
            leaf: dict = {"type": "array", "items": {"type": "string"}}
        else:
            t = _JSON_TYPES[s.type]
            leaf = {"type": [t, "null"] if s.optional else t}
        if s.type != "secret":
            leaf["default"] = s.default
        enum = config._VALID_ENUMS.get(s.path)
        if enum:
            leaf["enum"] = list(enum)
        leaf.update({"title": parts[-1].replace("_", " "), "x-section": parts[0], "x-env": list(s.env),
                     "x-secret": s.type == "secret", "x-restart-required": bool(_restart_reason(s.path)),
                     "description": config.DESCRIPTIONS.get(s.path, "")})
        cur["properties"][parts[-1]] = leaf
    root["config_revision"] = _read_state()["revision"]
    return _json(root)


def _changes_body(body: dict) -> tuple[str | None, dict]:
    changes = body.get("changes")
    if not isinstance(changes, dict):
        raise _Fail(400, "Body must be {revision?, changes:{path:value}}")
    rev = body.get("revision")
    return (str(rev) if rev is not None else None), changes


def _settings_mutator(changes: dict):
    def mut(doc: dict, ctx: _SecretCtx) -> dict:
        errors, paths = [], []
        for path, val in changes.items():
            s = config._BY_PATH.get(path)
            if s is None:
                hint = " (use /api/integrations)" if str(path).startswith("integrations") else ""
                errors.append(_err(str(path), "unknown_setting", f"unknown setting{hint}"))
                continue
            if s.type == "secret":
                keep, ref, _ = _secret_input(ctx, path, val, path.replace(".", "_"))
                if not keep:
                    _unset_path(doc, path)
                elif ref:
                    _set_path(doc, path, ref)
            elif val is None:
                _unset_path(doc, path)
            else:
                bad = _typecheck(s, val)
                if bad:
                    errors.append(_err(path, "invalid_type", bad))
                    continue
                _set_path(doc, path, val)
            paths.append(path)
        return {**_change_info(paths), "errors": errors,
                "integrations_reloaded": sorted((doc.get("integrations") or {}).keys())}
    return mut


def _body_secrets(obj: Any) -> list[str]:
    out: list[str] = []
    if isinstance(obj, dict):
        if isinstance(obj.get("value"), str) and obj.get("mode") in ("file", "keychain"):
            out.append(obj["value"])
        for v in obj.values():
            out += _body_secrets(v)
    elif isinstance(obj, list):
        for v in obj:
            out += _body_secrets(v)
    return out


@router.post("/api/settings/validate")
@_api
def validate_settings(request: Request, body: dict = Body(default_factory=dict), _g=Depends(_guard_write)):
    rev, changes = _changes_body(body)
    try:
        res = _commit(_settings_mutator(changes), rev, dry=True, local=_is_local(request))
    except _Fail as f:
        return _fail_with(f, _body_secrets(body))
    res.pop("integrations_reloaded", None)
    return _json(res, secrets=_body_secrets(body))


@router.patch("/api/settings")
@_api
def patch_settings(request: Request, body: dict = Body(default_factory=dict), _g=Depends(_guard_write)):
    rev, changes = _changes_body(body)
    try:
        res = _commit(_settings_mutator(changes), rev, dry=False, local=_is_local(request))
    except _Fail as f:
        return _fail_with(f, _body_secrets(body))
    res.pop("valid", None)
    res.pop("errors", None)
    log.info("settings saved: %s", ",".join(res.get("applied", [])))
    return _json(res, secrets=_body_secrets(body))


def _mask_doc(obj: Any, key: str = "") -> Any:
    if isinstance(obj, dict):
        return {k: _mask_doc(v, str(k)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask_doc(v, key) for v in obj]
    if isinstance(obj, str) and obj and config._SECRET_KEY_RE.search(key) and not config.is_ref(obj):
        return MASK
    return obj


@router.get("/api/settings/yaml")
@_api
def get_yaml(request: Request, _a=Depends(_auth)):
    path = config.config_path()
    out: dict = {"revision": "0", "mode_ok": True, "file_present": path.exists(), "yaml": ""}
    if not path.exists():
        return _json(out)
    try:
        raw = path.read_bytes()
        out["revision"] = _revision(raw)
        out["mode_ok"] = not (path.stat().st_mode & 0o077)
        doc = yaml.safe_load(raw)
    except Exception as e:
        mark = getattr(e, "problem_mark", None)
        out["error"] = f"cannot parse config file{f' (line {mark.line + 1})' if mark else ''}"
        return _json(out)
    out["yaml"] = yaml.safe_dump(_mask_doc(doc), sort_keys=False, default_flow_style=False, allow_unicode=True) if doc else ""
    return _json(out)


@router.post("/api/settings/reload")
@_api
def reload_settings(request: Request, _g=Depends(_guard_write)):
    config.reload()
    st = _read_state()
    return _json({"revision": st["revision"], "file_errors": _file_errors(st), "restart_required": _pending(),
                  "integrations_reloaded": sorted(config.integrations().keys())})


# --- restart bot -----------------------------------------------------------------

def _under_launchd() -> bool:
    return os.environ.get("XPC_SERVICE_NAME") == "com.dop-agent"


def _do_restart() -> None:
    """Hard-stop (like `launchctl kickstart -k`), never a graceful SIGTERM: a graceful stop aborts
    in-flight Copilot turns, which _handle() records as ":x: Error" and clears from active_status.json,
    so nothing would auto-resume. A hard stop leaves the markers for _resume_interrupted_turns().
    launchd (KeepAlive) respawns the job; otherwise re-exec in place."""
    if _under_launchd():
        os._exit(0)
    else:
        os.execv(sys.executable, [sys.executable, *sys.argv])


@router.post("/api/settings/restart")
@_api
def restart_bot(request: Request, body: dict = Body(default_factory=dict), _g=Depends(_guard_write)):
    if body.get("confirm") is not True:
        raise _Fail(400, "Restart must be confirmed: send {\"confirm\": true}")
    log.warning("bot restart requested via Web UI (%s)", "launchd" if _under_launchd() else "re-exec")
    t = threading.Timer(1.0, _do_restart)  # let the HTTP response flush first
    t.daemon = True
    t.start()
    return _json({"restarting": True, "mode": "launchd" if _under_launchd() else "re-exec"})


# --- import .env ----------------------------------------------------------------------

def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip().removeprefix("export ").strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        elif " #" in v:
            v = v.split(" #", 1)[0].strip()
        if _ENV_NAME_RE.match(k) and v:
            out[k] = v
    return out


def _env_var_map() -> dict[str, config.Setting]:
    m: dict[str, config.Setting] = {}
    for s in config.SETTINGS:
        for v in s.env:
            m.setdefault(v, s)
        if s.type == "secret" and s.env:
            m.setdefault(f"{s.env[0]}_FILE", s)
    return m


@router.post("/api/settings/import-env")
@_api
def import_env(request: Request, body: dict = Body(default_factory=dict), _g=Depends(_guard_write)):
    dry = body.get("dry_run", True) is not False
    handling = body.get("secret_handling") or {}
    env_path = Path(str(body.get("env_path") or (Path(config.__file__).parent / ".env"))).expanduser()
    if not (env_path.name.startswith(".env") or env_path.name.endswith(".env")):
        raise _Fail(400, "env_path must be a .env file")
    if not env_path.is_file():
        raise _Fail(404, f"{env_path} not found")
    values = _parse_env_file(env_path)
    vmap = _env_var_map()
    st = _read_state()
    stored = st["doc"] or {}
    # only secret-typed vars are scrubbed: non-secret previews (e.g. SESSION_TIMEOUT=300) are shown on purpose
    secret_vals = [v for k, v in values.items() if vmap.get(k) is not None and vmap[k].type == "secret"]
    mappings, unmapped, changes = [], [], {}
    for var, val in values.items():
        s = vmap.get(var)
        if s is None:
            unmapped.append(var)
            continue
        m = {"var": var, "path": s.path, "secret": s.type == "secret", "action": "set",
             "shadowed_by_env": bool(os.environ.get(var, "").strip())}
        ok, cur = config._walk(stored, s.path)
        if s.type == "secret":
            m["preview"] = f"{MASK} ({len(val)} chars)"
            mode = handling.get(var, "env")
            if var.endswith("_FILE"):
                planned: Any = "${file:%s}" % val
            elif mode == "env":
                planned = "${env:%s}" % var
            elif mode in ("file", "keychain"):
                planned = {"mode": mode, "value": val, "name": var.lower(), "service": f"dop-agent.{var.lower()}"}
                if mode == "file":
                    planned.pop("service")
                else:
                    planned.pop("name")
            else:
                m.update(action="error", message=f"unknown secret handling {mode!r}")
                mappings.append(m)
                continue
            if isinstance(planned, str) and ok and cur == planned:
                m["action"] = "unchanged"
            else:
                changes[s.path] = planned
        else:
            try:
                typed = config._coerce(s, val, True)
            except config.ConfigError as e:
                m.update(action="error", message=str(e), preview=val[:60])
                mappings.append(m)
                continue
            m["preview"] = str(val)[:60]
            if ok and cur == typed:
                m["action"] = "unchanged"
            else:
                changes[s.path] = typed
        mappings.append(m)
    try:
        res = _commit(_settings_mutator(changes), None, dry=dry, local=_is_local(request))
    except _Fail as f:
        return _fail_with(f, secret_vals)
    if dry and not res["valid"]:
        for e in res["errors"]:
            for m in mappings:
                if m["path"] == e["path"]:
                    m.update(action="error", message=e["message"])
    out = {"mappings": mappings, "unmapped": sorted(unmapped), "revision": res.get("revision", st["revision"]),
           "dry_run": dry}
    if not dry:
        out.update(restart_required=res.get("restart_required", []), env_overrides=res.get("env_overrides", []))
    return _json(out, secrets=secret_vals)


# --- drivers / integrations --------------------------------------------------------------

def _registry_for(entries: dict[str, dict]) -> Any:
    return build_registry(entries)


def _stored_integrations() -> dict:
    st = _read_state()
    return dict((st["doc"] or {}).get("integrations") or {}) if not st["errors"] else {}


def _plain_entry(entry: dict) -> dict:
    e = copy.deepcopy(entry)
    auth = e.get("auth") or {}
    for k, v in list(auth.items()):
        if isinstance(v, str) and config.is_ref(v):
            auth[k] = _try_resolve(v)
    e["auth"] = auth
    return e


def _dependent_watches(name: str, entry: dict, entries: dict) -> list[int]:
    try:
        import json as _json_mod

        import watchers

        reg = build_registry({name: entry})
        types = {k.spec.watch.type for it in reg.integrations.values() for k in it.kinds.values() if k.spec.watch}
        siblings = [n for n, e in entries.items() if e.get("driver") == entry.get("driver")]
        ids = []
        for w in watchers.list_watches():
            meta = w.get("meta")
            if isinstance(meta, str):
                try:
                    meta = _json_mod.loads(meta)
                except ValueError:
                    meta = {}
            pinned = (meta or {}).get("integration")
            if w.get("status") in ("active",) and (pinned == name or (not pinned and w.get("type") in types
                                                                       and siblings == [name])):
                ids.append(int(w["id"]))
        return ids
    except Exception:
        return []


def _norm_entry(entry: dict) -> dict:
    e = copy.deepcopy(entry)
    e["enabled"] = bool(e.get("enabled", True))
    return e


def _fingerprint(entry: dict) -> str:
    import json as _j

    return hashlib.sha256(_j.dumps(_norm_entry(entry), sort_keys=True, default=str).encode()).hexdigest()


def _record_status(name: str, entry: dict, out: dict) -> None:
    _STATUS[name] = {"state": "ok" if out["ok"] else "error",
                     "detail": out.get("identity") or out["error"]["message"],
                     "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    _STATUS_FP[name] = _fingerprint(entry)


def _reset_status(name: str) -> None:
    _STATUS.pop(name, None)
    _STATUS_FP.pop(name, None)
    _LAST_ATTEMPT.pop(name, None)


_JWT_RE = re.compile(r"eyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]*")


def _run_test(key: str, plain: dict) -> dict:
    reg = build_registry({key: {**plain, "enabled": True}})
    t0 = time.monotonic()
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(reg.test_connection, key)
    it = reg.integrations.get(key)
    limit = PACKAGE_TEST_TIMEOUT_S if it is not None and it.manifest.is_package else TEST_TIMEOUT_S
    try:
        r = fut.result(timeout=limit)
        out: dict = {"ok": bool(r.get("ok"))}
        msg = str(r.get("message") or "")
        if out["ok"]:
            out["identity"] = msg
        else:
            out["error"] = {"code": "connection_failed", "message": msg}
        if isinstance(r.get("steps"), list):  # package drivers: per-step result so the UI shows what failed
            out["steps"] = [{"step": str(st.get("step"))[:40], "ok": bool(st.get("ok")),
                             "detail": _JWT_RE.sub("<token>", str(st.get("detail") or ""))[:300]}
                            for st in r["steps"] if isinstance(st, dict)]
        if isinstance(r.get("set_options"), dict) and it is not None:
            allowed = {it.manifest.tenant.get("option")} - {None}  # only options the manifest says a test may learn
            out["set_options"] = {k: v for k, v in r["set_options"].items()
                                  if k in allowed and isinstance(v, str) and v and len(v) <= 100}
    except concurrent.futures.TimeoutError:
        out = {"ok": False, "error": {"code": "timeout", "message": f"no answer within {limit}s"}}
    except Exception as e:
        out = {"ok": False, "error": {"code": "connection_failed", "message": type(e).__name__}}
    finally:
        ex.shutdown(wait=False)
    out["latency_ms"] = int((time.monotonic() - t0) * 1000)
    return out


def _bg_check(name: str, entry: dict) -> None:
    try:
        out = _run_test(name, _plain_entry(entry))
        if _fingerprint(_stored_integrations().get(name) or {}) == _fingerprint(entry):
            _record_status(name, entry, _scrub(out, _known_secrets(
                [v for v in (_plain_entry(entry).get("auth") or {}).values() if isinstance(v, str) and v])))
    except Exception:
        log.warning("background connection test of %s failed", name)
    finally:
        with _BG_LOCK:
            _CHECKING.discard(name)


def _schedule_checks(entries: dict, reg) -> None:
    """Start background tests for enabled+valid integrations with no fresh status (never blocks)."""
    global _BG_POOL
    now = time.time()
    for name, entry in entries.items():
        it = reg.integrations.get(name)
        if not entry.get("enabled", True) or not it or not it.valid:
            continue
        st = _STATUS.get(name)
        if st and _STATUS_FP.get(name) == _fingerprint(entry):
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(st["checked_at"])).total_seconds()
            except (KeyError, TypeError, ValueError):
                age = STATUS_TTL_S + 1
            if age < STATUS_TTL_S:
                continue
        with _BG_LOCK:
            if name in _CHECKING or now - _LAST_ATTEMPT.get(name, 0) < BG_THROTTLE_S:
                continue
            _LAST_ATTEMPT[name] = now
            _CHECKING.add(name)
            if _BG_POOL is None:
                _BG_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=BG_WORKERS,
                                                                 thread_name_prefix="int-status")
            _BG_POOL.submit(_bg_check, name, copy.deepcopy(entry))


def _integration_view(name: str, entry: dict, reg, entries: dict) -> dict:
    it = reg.integrations.get(name)
    man = reg.manifests.get(entry.get("driver"))
    auth = entry.get("auth") or {}
    at = man.auth_types.get(auth.get("type")) if man else None
    fields: dict = {}
    for fname, spec in (at.fields.items() if at else []):
        raw = auth.get(fname)
        if spec.secret:
            ref = raw if isinstance(raw, str) and raw else None
            fields[fname] = {"secret": True, "required": spec.required, "ref": ref, "state": _ref_state(ref)}
        else:
            fields[fname] = {"secret": False, "required": spec.required, "value": raw}
    enabled = bool(entry.get("enabled", True))
    st = _STATUS.get(name)
    if st and _STATUS_FP.get(name) != _fingerprint(entry):
        st = None
    if not st:
        state = "disabled" if not enabled else ("invalid" if it and not it.valid else "unknown")
        if state == "unknown" and name in _CHECKING:
            state = "checking"
        st = {"state": state, "detail": "; ".join(it.errors) if it and it.errors else "", "checked_at": None}
    return {"name": name, "driver": entry.get("driver"), "title": man.title if man else entry.get("driver"),
            "enabled": enabled, "valid": bool(it and it.valid), "errors": list(it.errors) if it else ["unknown driver"],
            "base_url": entry.get("base_url", ""), "auth": {"type": auth.get("type"), "fields": fields},
            "options": dict(entry.get("options") or {}), "kinds": copy.deepcopy(entry.get("kinds") or {}),
            "status": st, "watch_count": len(_dependent_watches(name, entry, entries))}


def _view_all(check: bool = False) -> tuple[list[dict], str]:
    entries = _stored_integrations()
    reg = build_registry(entries)
    if check:
        _schedule_checks(entries, reg)
    return [_integration_view(n, e, reg, entries) for n, e in entries.items()], _read_state()["revision"]


@router.get("/api/drivers")
@_api
def get_drivers(request: Request, _a=Depends(_auth)):
    reg = build_registry({})
    return _json([{**d, "secret_modes": _SECRET_MODES} for d in reg.catalog()])


@router.get("/api/integrations")
@_api
def list_integrations(request: Request, _a=Depends(_auth)):
    return _json(_view_all(check=True)[0])


def _build_auth(ctx: _SecretCtx, name: str, man, auth_in: Any, existing: dict | None, where: str):
    """-> (auth_cfg flat dict as stored, plain_auth for tests)."""
    errs = ctx.errors
    if auth_in is None:
        auth_in = {}
    if not isinstance(auth_in, dict):
        errs.append(_err(f"{where}.auth", "invalid_type", "expected object"))
        return dict(existing or {}), dict(existing or {})
    old = dict(existing or {})
    atype = auth_in.get("type", old.get("type"))
    if man.auth_types and atype not in man.auth_types:
        errs.append(_err(f"{where}.auth.type", "invalid_choice", f"must be one of {sorted(man.auth_types)}"))
        return old, old
    new: dict = {"type": atype} if atype is not None else {}
    spec = man.auth_types.get(atype)
    fields_in = auth_in.get("fields") or {}
    if not isinstance(fields_in, dict):
        errs.append(_err(f"{where}.auth.fields", "invalid_type", "expected object"))
        fields_in = {}
    same_type = atype == old.get("type")
    plain = dict(new)
    for fname, fspec in (spec.fields.items() if spec else []):
        path = f"{where}.auth.{fname}"
        if fname in fields_in:
            if fspec.secret:
                keep, ref, pv = _secret_input(ctx, path, fields_in[fname], f"integration_{name}_{fname}")
                if keep and ref:
                    new[fname], plain[fname] = ref, pv or ""
            else:
                v = fields_in[fname]
                if v not in (None, ""):
                    new[fname] = plain[fname] = v
        elif same_type and old.get(fname):
            new[fname] = old[fname]
            plain[fname] = _try_resolve(old[fname]) if fspec.secret and isinstance(old[fname], str) else old[fname]
    for extra in set(fields_in) - set(spec.fields if spec else []):
        errs.append(_err(f"{where}.auth.{extra}", "unknown_setting", f"unknown auth field for type {atype!r}"))
    return new, plain


def _entry_from(ctx: _SecretCtx, name: str, body: dict, existing: dict | None, reg_manifests) -> tuple[dict, dict]:
    where = f"integrations.{name}"
    driver = body.get("driver", (existing or {}).get("driver"))
    man = reg_manifests.get(driver)
    if man is None:
        raise _Fail(422, "Validation failed", [_err(f"{where}.driver", "driver_unknown",
                                                    f"unknown driver {driver!r} (known: {sorted(reg_manifests)})")])
    ex = existing or {}
    if "auth" in body or "secret" in body or not existing:
        auth_cfg, auth_plain = _build_auth(ctx, name, man, body.get("auth"), ex.get("auth"), where)
    else:
        auth_cfg = dict(ex.get("auth") or {})
        auth_plain = {k: (_try_resolve(v) if isinstance(v, str) else v) for k, v in auth_cfg.items()}
    entry: dict = {"driver": driver, "enabled": body.get("enabled", ex.get("enabled", True)),
                   "base_url": str(body.get("base_url", ex.get("base_url", "")) or "").strip().rstrip("/")}
    if auth_cfg:
        entry["auth"] = auth_cfg
    opts = dict(ex.get("options") or {})
    for k, v in (body.get("options") or {}).items() if isinstance(body.get("options") or {}, dict) else []:
        if v is None:
            opts.pop(k, None)
        else:
            opts[k] = v
    if opts:
        entry["options"] = opts
    kinds = copy.deepcopy(ex.get("kinds") or {})
    kin = body.get("kinds") or {}
    if isinstance(kin, dict):
        for k, v in kin.items():
            if isinstance(v, dict):
                merged = {**(kinds.get(k) or {}), **{a: b for a, b in v.items() if a != "watch"}}
                if isinstance(v.get("watch"), dict):
                    merged["watch"] = {**((kinds.get(k) or {}).get("watch") or {}), **v["watch"]}
                kinds[k] = merged
            else:
                ctx.errors.append(_err(f"{where}.kinds.{k}", "invalid_type", "expected object"))
    if kinds:
        entry["kinds"] = kinds
    plain = {**entry, "auth": auth_plain}
    return entry, plain


def _driver_errors(name: str, entry: dict) -> list[dict]:
    reg = build_registry({name: entry})
    it = reg.integrations.get(name)
    out = []
    for e in (it.errors if it else reg.errors):
        m = re.match(r"^(auth\.\w+|base_url|auth\.type)\b", e) or re.match(r"^option (\w+)", e) or re.match(
            r"^kinds\.(\S+?)\.", e)
        path = f"integrations.{name}"
        if m:
            g = m.group(0)
            path += "." + (g if "." in g or g == "base_url" else f"options.{m.group(1)}")
        out.append(_err(path, "invalid_config", e))
    return out


def _host_of(url: str) -> str:
    from urllib.parse import urlsplit

    try:
        u = urlsplit((url or "").strip())
        return f"{u.scheme}://{(u.hostname or '').lower()}:{u.port or ''}" if u.hostname else ""
    except ValueError:
        return "invalid"


def _host_change_problem(entry: dict, saved: dict | None) -> str:
    """Non-empty when `entry` would send secret refs to a different host than the saved instance."""
    if not saved or _host_of(entry.get("base_url", "")) == _host_of(saved.get("base_url", "")):
        return ""
    if not any(config.is_ref(v) for v in (entry.get("auth") or {}).values() if isinstance(v, str)):
        return ""
    return (f"base_url host changes from {_host_of(saved.get('base_url', '')) or '(none)'} to "
            f"{_host_of(entry.get('base_url', '')) or '(none)'} while secret refs are attached; "
            "confirm the change in the Web UI (localhost)")


def _integration_mutator(name: str, body: dict, *, create: bool):
    def mut(doc: dict, ctx: _SecretCtx) -> dict:
        ints = doc.setdefault("integrations", {})
        existing = ints.get(name)
        if create and existing is not None:
            raise _Fail(409, f"integration {name!r} already exists", [_err(f"integrations.{name}", "exists",
                                                                         "name already used")])
        if not create and existing is None:
            raise _Fail(404, f"integration {name!r} not found")
        b = dict(body)
        if "secret" in b and isinstance(b["secret"], dict):
            s = b.pop("secret")
            fld = s.get("field")
            if not isinstance(fld, str):
                ctx.errors.append(_err(f"integrations.{name}.secret", "invalid", "secret.field required"))
            else:
                fields = dict((b.get("auth") or {}).get("fields") or {})
                fields[fld] = {k: v for k, v in s.items() if k != "field"}
                b["auth"] = {**(b.get("auth") or {}), "fields": fields}
        entry, _plain = _entry_from(ctx, name, b, existing, build_registry({}).manifests)
        problem = _host_change_problem(entry, existing)
        if problem and not (ctx.local and b.get("confirm_base_url") is True):
            ctx.errors.append(_err(f"integrations.{name}.base_url", "base_url_change_unconfirmed", problem))
        errors = []
        if not ctx.errors:
            errors = _driver_errors(name, entry)
        ints[name] = entry
        return {"errors": errors, "integrations_reloaded": [name], "applied": [f"integrations.{name}"],
                "restart_required": [], "_name": name}
    return mut


def _finish_integration(res: dict, name: str, status: int = 200):
    entries = _stored_integrations()
    reg = build_registry(entries)
    view = _integration_view(name, entries[name], reg, entries)
    view.update(revision=res["revision"], integrations_reloaded=[name])
    return _json(view, status)


@router.post("/api/integrations")
@_api
def create_integration(request: Request, body: dict = Body(default_factory=dict), _g=Depends(_guard_write)):
    name = str(body.get("name") or "")
    if not _NAME_RE.match(name):
        raise _Fail(422, "Validation failed", [_err("name", "name_invalid", "use a-z 0-9 _ - (max 64)")])
    if not body.get("driver"):
        raise _Fail(422, "Validation failed", [_err("driver", "driver_unknown", "driver is required")])
    try:
        res = _commit(_integration_mutator(name, body, create=True), body.get("revision"), dry=False,
                      local=_is_local(request))
        _reset_status(name)
        return _finish_integration(res, name, 201)
    except _Fail as f:
        return _fail_with(f, _body_secrets(body))


@router.patch("/api/integrations/{name}")
@_api
def patch_integration(name: str, request: Request, body: dict = Body(default_factory=dict),
                      _g=Depends(_guard_write)):
    if "driver" in body:
        raise _Fail(400, "driver cannot be changed; remove and re-create the integration")
    try:
        res = _commit(_integration_mutator(name, body, create=False), body.get("revision"), dry=False,
                      local=_is_local(request))
        _reset_status(name)
        return _finish_integration(res, name)
    except _Fail as f:
        return _fail_with(f, _body_secrets(body))


@router.delete("/api/integrations/{name}")
@_api
def delete_integration(name: str, request: Request, confirm: str = "", _g=Depends(_guard_write)):
    if confirm != name:
        raise _Fail(400, "Pass ?confirm=<integration name> to delete")
    paused: list[int] = []

    def mut(doc: dict, ctx: _SecretCtx) -> dict:
        ints = doc.get("integrations") or {}
        if name not in ints:
            raise _Fail(404, f"integration {name!r} not found")
        paused.extend(_dependent_watches(name, ints[name], ints))
        del ints[name]
        if not ints:
            doc.pop("integrations", None)
        return {}

    res = _commit(mut, None, dry=False, local=_is_local(request))
    n = 0
    if paused:
        try:
            import watchers

            n = sum(1 for wid in paused if watchers.set_status(wid, "paused"))
        except Exception:
            log.warning("could not pause dependent watches of %s", name)
    _reset_status(name)
    return _json({"paused_watches": n, "revision": res["revision"], "integrations_reloaded": [name]})


@router.post("/api/integrations/test")
@_api
def test_integration(request: Request, body: dict = Body(default_factory=dict), _g=Depends(_guard_write)):
    entries = _stored_integrations()
    name = str(body.get("name") or "")
    saved = entries.get(name)
    secrets = _body_secrets(body)
    if "driver" not in body:
        if saved is None:
            raise _Fail(404, f"integration {name!r} not found")
        entry, plain = saved, _plain_entry(saved)
    else:
        ctx = _SecretCtx(_is_local(request))
        try:
            entry, plain = _entry_from(ctx, name or "draft", body, saved if saved and saved.get("driver") == body["driver"] else None,
                                       build_registry({}).manifests)
        except _Fail as f:
            return _fail_with(f, secrets)
        problem = _host_change_problem(entry, saved if saved and saved.get("driver") == body["driver"] else None)
        if problem and not (ctx.local and body.get("confirm_base_url") is True):
            ctx.errors.append(_err(f"integrations.{name}.base_url", "base_url_change_unconfirmed", problem))
        errs = ctx.errors + (_driver_errors(name or "draft", entry) if not ctx.errors else [])
        secrets += [p for p in plain.get("auth", {}).values() if isinstance(p, str) and p] + ctx.plain
        if errs:
            return _json({"ok": False, "latency_ms": 0, "error": {"code": "invalid_config",
                                                                  "message": "; ".join(e["message"] for e in errs),
                                                                  "errors": errs}}, secrets=secrets)
    secrets += [v for v in (plain.get("auth") or {}).values() if isinstance(v, str) and v]
    out = _run_test(name or "draft", plain)
    if saved is not None and ("driver" not in body or _fingerprint(entry) == _fingerprint(saved)):
        _persist_test_options(name, out, request)
        _record_status(name, _stored_integrations().get(name) or saved, out)
    return _json(out, secrets=secrets)


def _persist_test_options(name: str, out: dict, request: Request) -> None:
    """A successful test may report options it learned (Teams: `tenant_id` from the token's tid claim);
    write the changed ones into the saved instance so link matching works without manual input."""
    new = out.get("set_options") if out.get("ok") else None
    if not new:
        return
    cur = (_stored_integrations().get(name) or {}).get("options") or {}
    changed = {k: v for k, v in new.items() if cur.get(k) != v}
    if not changed:
        return
    try:
        _commit(_integration_mutator(name, {"options": changed}, create=False), None, dry=False,
                local=_is_local(request))
        _reset_status(name)
        out["saved_options"] = changed
    except Exception:
        log.warning("could not persist test-discovered options for %s", name)


# --- chat adapters (Slack / Telegram) -------------------------------------------------------------
# Singletons mapped onto the existing adapters.<id>.* settings; saving goes through _settings_mutator so
# validation, secret handling and restart-class reporting are identical to PATCH /api/settings.

def _adapter_or_404(aid: str) -> dict:
    man = adapter_catalog.ADAPTERS.get(aid)
    if man is None:
        raise _Fail(404, f"adapter {aid!r} not found")
    return man


def _adapter_status(aid: str, enabled: bool, configured: bool) -> dict:
    if not configured:
        return {"state": "disabled", "detail": "not configured"}
    if not enabled:
        return {"state": "disabled", "detail": "disabled"}
    return _STATUS.get("adapter:" + aid) or {"state": "unknown", "detail": ""}


def _adapter_view(aid: str, raw: dict) -> dict:
    import adapters

    man = adapter_catalog.ADAPTERS[aid]
    fields = {f: _setting_view(config._BY_PATH[f"adapters.{aid}.{f}"], raw) for f in man["fields"]}
    configured = all(fields[f].get("set") if fields[f]["secret"] else bool(fields[f]["value"]) for f in man["required"])
    enabled = bool(config.value(f"adapters.{aid}.enabled"))
    pending = [p for p in _pending() if p["path"].startswith(f"adapters.{aid}.")]
    errors: list[str] = []  # an unconfigured adapter is simply off, not an error
    return {"id": aid, "title": man["title"], "icon": man["icon"], "icon_url": f"/static/icons/{man['icon']}.svg", "help": man["help"], "enabled": enabled and configured,
            "configured": configured, "running": aid in adapters.REGISTRY, "valid": not errors, "errors": errors,
            "required": man["required"], "fields": fields, "restart_pending": pending,
            "status": _adapter_status(aid, enabled, configured)}


@router.get("/api/adapters")
@_api
def list_adapters(request: Request, _a=Depends(_auth)):
    st = _read_state()
    raw = st["doc"] if not st["errors"] else {}
    return _json({"adapters": [_adapter_view(a, raw) for a in adapter_catalog.ADAPTERS], "revision": st["revision"]})


@router.patch("/api/adapters/{aid}")
@_api
def patch_adapter(aid: str, request: Request, body: dict = Body(default_factory=dict), _g=Depends(_guard_write)):
    man = _adapter_or_404(aid)
    fields = body.get("fields") or {}
    if not isinstance(fields, dict):
        raise _Fail(400, "fields must be an object")
    bad = [_err(f"adapters.{aid}.{f}", "unknown_setting", "unknown adapter field") for f in fields if f not in man["fields"]]
    if bad:
        raise _Fail(422, "Validation failed", bad)
    changes = {f"adapters.{aid}.{f}": v for f, v in fields.items()}
    if "enabled" in body:
        changes[f"adapters.{aid}.enabled"] = body["enabled"]
    rev = body.get("revision")
    try:
        res = _commit(_settings_mutator(changes), str(rev) if rev is not None else None, dry=False, local=_is_local(request))
    except _Fail as f:
        return _fail_with(f, _body_secrets(body))
    for k in ("valid", "errors", "integrations_reloaded"):
        res.pop(k, None)
    _STATUS.pop("adapter:" + aid, None)
    log.info("adapter %s saved: %s", aid, ",".join(res.get("applied", [])))
    return _json({**res, "adapter": _adapter_view(aid, _read_state()["doc"] or {})}, secrets=_body_secrets(body))


@router.post("/api/adapters/test")
@_api
def test_adapter(request: Request, body: dict = Body(default_factory=dict), _g=Depends(_guard_write)):
    """Body {id, fields?}: omitted fields use the effective saved value; nothing is persisted."""
    aid = str(body.get("id") or "")
    man = _adapter_or_404(aid)
    draft = body.get("fields") or {}
    secrets = _body_secrets(body)
    ctx = _SecretCtx(_is_local(request))
    values: dict = {}
    for f in man["fields"]:
        path = f"adapters.{aid}.{f}"
        s = config._BY_PATH[path]
        if f in draft:
            if s.type == "secret":
                keep, ref, pv = _secret_input(ctx, path, draft[f], path.replace(".", "_"))
                values[f] = pv if keep and ref else ""
            elif draft[f] in (None, ""):
                values[f] = s.default
            else:
                bad = _typecheck(s, draft[f])
                if bad:
                    ctx.errors.append(_err(path, "invalid_type", bad))
                values[f] = draft[f]
        else:
            values[f] = config.secret_value(path) if s.type == "secret" else config.value(path)
    if ctx.errors:
        return _json({"ok": False, "latency_ms": 0, "error": {"code": "invalid_config",
                      "message": "; ".join(e["message"] for e in ctx.errors), "errors": ctx.errors}}, secrets=secrets)
    secrets += [v for v in values.values() if isinstance(v, str) and len(v) >= 4]
    out = adapter_catalog.run_test(aid, values)
    if not draft:
        _STATUS["adapter:" + aid] = {"state": "ok" if out["ok"] else "error",
                                     "detail": out.get("identity") or out["error"]["message"],
                                     "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    return _json(out, secrets=secrets)


# --- watch types ----------------------------------------------------------------------------

@router.get("/api/watch-types")
@_api
def get_watch_types(request: Request, _a=Depends(_auth)):
    reg = build_registry(_stored_integrations())
    proj = reg.ui_projection()
    return _json({"registry": proj["watch_types"], "builtin": list(config.value("watchers.builtin_types"))})


def health_extra() -> dict:
    """Optional additions for /api/health (config_revision + integrations status) for the UI session."""
    try:
        views, rev = _view_all()
        return {"config_revision": rev, "integrations": [{"name": v["name"], "status": v["status"]["state"]} for v in views]}
    except Exception:
        return {}
