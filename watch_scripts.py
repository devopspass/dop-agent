"""Script-only time watches: run an allow-listed read-only script on schedule
and only wake the model when the script says so.

Why: recurring polls ("is job X done", Teams poll, ...) would otherwise cost
a full model turn per tick just to answer "nothing yet". A script watch runs
the command in a subprocess and posts the stdout as a plain notice; the model
is woken only on the condition in ``meta.wake_if`` or on any failure.

meta = {"script": "name.py", "args": [...], "wake_if": "nonempty" |
        "changed" | "exit!=0" | "never"(default)}
(``script`` watches created with the exit-code contract use no ``wake_if``:
rc 0 = noop, 200 = changed, anything else = error; see decide_contract.)

Security model (tests/test_watch_scripts.py):
  * scripts resolve ONLY inside the allow-listed dirs (scripts/watch/,
    PLUGIN_DIR, driver ``watch/`` folders) - basename only, no
    separators/``..``, no symlinks, must be a regular, executable file
    that is not group/world-writable. An agent registering a watch cannot
    point it at an arbitrary command;
  * minimal environment (the ``_ENV_KEYS`` allow-list), no shell, args are
    plain strings passed as argv;
  * SCRIPT_TIMEOUT (120s), kills the whole process group;
  * scripts are read-only BY CONVENTION (mutating modes, e.g. the watcher
    review's --apply, must be an explicit arg of the watch).

Failure handling: any non-zero exit / timeout (auth, VPN, ...) WAKES the
model with a stderr tail, but an identical repeated failure is deduplicated
for FAIL_COOLDOWN_SECS so an outage does not wake the model every tick.
State (``watches.script_state``) is keyed by hash(script, args).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import watch_sanitize

log = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).parent / "scripts" / "watch"
# Agent/user-written plugins (not in git). Same permission checks as SCRIPT_DIR.
PLUGIN_DIR = Path.home() / ".dop-agent" / "watch-plugins"
DRIVERS_DIR = Path(__file__).parent / "drivers"
STATE_ROOT = Path.home() / ".dop-agent" / "watch-state"
OUT_ROOT = Path.home() / ".dop-agent" / "watch-out"
OUT_FILE_CAP = 1_000_000
# Exit-code contract of `script` watches (see decide_contract): 0 noop, 200 changed, other = error.
RC_NOOP = 0
RC_CHANGED = 200
SCRIPT_TIMEOUT = 120.0
FAIL_COOLDOWN_SECS = 6 * 3600
WAKE_MODES = ("never", "nonempty", "changed", "exit!=0")
MAX_ARGS = 10
MAX_ARG_LEN = 300
STDOUT_CAP = 3000
STDERR_TAIL = 1500
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")
_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "USER", "AWS_PROFILE", "AWS_REGION",
             "AWS_DEFAULT_REGION", "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE", "SSH_AUTH_SOCK",
             "OUTLOOK_ACCOUNT", "OUTLOOK_CDP_URL")


PLUGIN_DRIVERS_DIR = Path.home() / ".dop-agent" / "drivers"


def driver_watch_dirs() -> list[Path]:
    """`drivers/<id>/watch/` folders of shipped package drivers (in git, reviewed with the code)."""
    return sorted(d for d in DRIVERS_DIR.glob("*/watch") if d.is_dir())


def plugin_driver_watch_dirs() -> list[Path]:
    """`watch/` folders of user-installed package drivers; scripts there still need a dry-run like PLUGIN_DIR."""
    return sorted(d for d in PLUGIN_DRIVERS_DIR.glob("*/watch") if d.is_dir())


def _available(bases) -> str:
    names = sorted({p.name for b in bases if b.is_dir() for p in b.glob("*")
                    if _NAME_RE.match(p.name) and not p.name.startswith("_") and p.name != "README.md"})
    return ", ".join(names) or "none"


def resolve_script(name, base: Path | None = None) -> Path:
    bases = [base] if base else [SCRIPT_DIR, *driver_watch_dirs(), PLUGIN_DIR, *plugin_driver_watch_dirs()]
    hint = (f" Put a read-only executable script (chmod 755) in one of "
            f"{', '.join(str(b) for b in bases)} and pass only its file name "
            f"(available: {_available(bases)}).")
    if not isinstance(name, str) or not _NAME_RE.match(name) or name.startswith("_"):
        raise ValueError(f"invalid script name {name!r} (basename only, no paths).{hint}")
    found = [b for b in bases if (b / name).exists() or (b / name).is_symlink()]
    if len(found) > 1 and not base:
        raise ValueError(f"script name {name!r} exists in several allow-listed directories "
                         f"({', '.join(str(b) for b in found)}) - ambiguous; rename one of them.")
    if found:
        return _check_script(found[0], name, hint)
    raise ValueError(f"script {name!r} not found.{hint}")


def script_argv(path: Path, args) -> list[str]:
    """`.py` scripts run under the bot's own interpreter (it has the deps); others are exec'd directly."""
    return [sys.executable, str(path), *args] if path.suffix == ".py" else [str(path), *args]


def _is_user_dir(base: Path) -> bool:
    """True for user-installed locations (PLUGIN_DIR, ~/.dop-agent/drivers/*/watch); shipped dirs are git-reviewed."""
    try:
        home = Path.home() / ".dop-agent"
        return base.resolve().is_relative_to(home.resolve())
    except OSError:
        return False


def _check_script(base: Path, name: str, hint: str) -> Path:
    path = base / name
    st = path.lstat()
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise ValueError(f"script {name!r} must be a regular file, not a symlink.{hint}")
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ValueError(f"script {name!r} is group/world-writable - refusing to run it.")
    if _is_user_dir(base):
        # user-writable locations (plugins): the file and its directory must belong to us
        dst = base.stat()
        for what, s_ in (("script", st), ("directory", dst)):
            if s_.st_uid != os.getuid():
                raise ValueError(f"{what} for {name!r} is not owned by the current user - refusing to run it.")
        if dst.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            log.warning("watch script dir %s is group/world-writable - refusing %s", base, name)
            raise ValueError(f"directory {base} is group/world-writable (chmod go-w) - refusing to run {name!r}.")
        try:  # re-check on an opened fd (no symlink follow) to narrow the check-to-use window
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as exc:
            raise ValueError(f"script {name!r} cannot be opened safely: {exc}") from exc
        try:
            fst = os.fstat(fd)
        finally:
            os.close(fd)
        if (fst.st_ino, fst.st_dev) != (st.st_ino, st.st_dev) or fst.st_uid != os.getuid() \
                or fst.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ValueError(f"script {name!r} changed while being checked - refusing to run it.")
    if not os.access(path, os.X_OK):
        raise ValueError(f"script {name!r} is not executable (chmod 755).")
    if path.resolve().parent != base.resolve():
        raise ValueError(f"script {name!r} resolves outside the allow-listed directory.")
    return path


def validate_meta(meta: dict, base: Path | None = None) -> None:
    """Raise ValueError (with a fix hint) for a bad script-watch meta."""
    resolve_script(meta.get("script"), base)
    args = meta.get("args", [])
    if (not isinstance(args, list) or len(args) > MAX_ARGS
            or any(not isinstance(a, str) or len(a) > MAX_ARG_LEN for a in args)):
        raise ValueError(f"meta.args must be a list of <= {MAX_ARGS} strings of <= {MAX_ARG_LEN} chars.")
    wake = meta.get("wake_if", "never")
    if wake not in WAKE_MODES:
        raise ValueError(f"meta.wake_if must be one of {WAKE_MODES}, got {wake!r}.")


def apply_watcher(meta: dict, registry=None) -> dict:
    """Resolve `meta.watcher` (package-driver recipe, see manifest `watchers:`) against the enabled instances.

    Validates the recipe/script pair, pins `meta.integration`, adds the recipe's leading args (mode first) and
    returns the new meta. No-op without `meta.watcher`. Raises ValueError with a fix hint."""
    wid = meta.get("watcher")
    if not wid:
        if meta.get("integration"):
            raise ValueError("meta.integration is only used together with meta.watcher.")
        return meta
    if registry is None:
        from integrations.runtime import get_registry
        registry = get_registry()
    from drivers.base import DriverError
    try:
        it = registry.resolve_watcher(wid, meta.get("integration"))
    except DriverError as exc:
        raise ValueError(str(exc)) from exc
    spec = it.manifest.watchers[wid]
    if meta.get("script") != spec.script:
        raise ValueError(f"watcher {wid!r} runs script {spec.script!r}, got {meta.get('script')!r}.")
    owned = (Path(it.manifest.source_dir) / "watch").resolve()
    if resolve_script(spec.script).parent.resolve() != owned:
        raise ValueError(f"script {spec.script!r} resolves outside the {it.manifest.id} driver's watch/ folder "
                         "(name clash with another script); rename it.")
    args = list(meta.get("args", []))
    if spec.args:
        mode = spec.args[0]
        if args and not args[0].startswith("-") and args[0] != mode:
            raise ValueError(f"watcher {wid!r} is the {mode!r} mode; got args starting with {args[0]!r}. "
                             "Pass only extra filters in args (the leading mode is added automatically).")
        if not args or args[0] != mode:
            args = [mode, *args]
        have = {a.split("=", 1)[0] for a in args if a.startswith("-")}
        group: list[str] = []
        for a in [*spec.args[1:], None]:
            if a is not None and not a.startswith("-") and group:
                group.append(a)  # value of the preceding flag
                continue
            if group and group[0].split("=", 1)[0] not in have:  # user-supplied flags win over the recipe's
                args += group
            group = [a] if a is not None else []
    return {**meta, "integration": it.name, "args": args}


def instance_env(meta: dict, registry=None) -> dict:
    """Env vars of the package instance a watch is pinned to (`meta.integration`); {} when not pinned.

    Raises ValueError when the pinned instance is gone, disabled or its templates no longer render: running the
    script without its env would silently use a default (possibly different) account."""
    name = meta.get("integration")
    if not name:
        return {}
    try:
        if registry is None:
            from integrations.runtime import get_registry
            registry = get_registry()
        it = registry.integrations.get(name)
        if it is None or not it.active or not it.manifest.is_package:
            raise ValueError("integration is not an enabled package integration")
        return registry.script_env(name)
    except Exception as exc:
        raise ValueError(f"integration {name!r} unavailable for this watch ({exc}); re-enable it in Settings -> "
                         "Integrations or recreate the watch.") from exc


def script_hash(meta: dict) -> str:
    return hashlib.sha1(json.dumps([meta.get("script"), meta.get("args", [])]).encode()).hexdigest()[:12]


def _minimal_env(extra: dict | None = None) -> dict:
    env = {k: os.environ[k] for k in _ENV_KEYS if k in os.environ}
    env.update(extra or {})
    return env


async def run_script(meta: dict, timeout: float = SCRIPT_TIMEOUT, base: Path | None = None,
                     extra_env: dict | None = None) -> dict:
    """Run the allow-listed script; never raises. Returns
    {rc, stdout, stderr, timed_out}."""
    try:
        path = resolve_script(meta.get("script"), base)
    except ValueError as exc:
        return {"rc": 127, "stdout": "", "stderr": str(exc), "timed_out": False}
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *script_argv(path, meta.get("args", [])),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=_minimal_env(extra_env), cwd=str(path.parent), start_new_session=True,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return {"rc": proc.returncode, "stdout": out.decode(errors="replace"),
                "stderr": err.decode(errors="replace"), "timed_out": False}
    except asyncio.TimeoutError:
        if proc is not None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except Exception:
                pass
        return {"rc": -9, "stdout": "", "stderr": f"timed out after {timeout:.0f}s", "timed_out": True}
    except Exception as exc:
        return {"rc": 126, "stdout": "", "stderr": f"failed to launch: {exc}", "timed_out": False}


def parse_state(raw, meta: dict) -> dict:
    try:
        st = json.loads(raw or "{}")
    except Exception:
        st = {}
    return st if st.get("h") == script_hash(meta) else {"h": script_hash(meta)}


def decide(meta: dict, result: dict, state: dict, now: float | None = None) -> tuple[str, str, dict]:
    """-> (action, text, new_state); action is 'wake' (model turn),
    'notice' (plain system notice, no model) or 'silent'."""
    now = now if now is not None else time.time()
    name = meta.get("script")
    state = dict(state)
    if result["rc"] != 0 or result["timed_out"]:
        err = watch_sanitize.sanitize(result["stderr"]).strip()
        last = err.splitlines()[-1] if err else ""
        sig = f"{result['rc']}:{last[:120]}"
        if state.get("fail_sig") == sig and now - state.get("fail_ts", 0) < FAIL_COOLDOWN_SECS:
            return "silent", "", state
        state.update(fail_sig=sig, fail_ts=now)
        why = "timed out" if result["timed_out"] else f"exit {result['rc']}"
        return "wake", (f":warning: watch script `{name}` failed ({why}) - possible auth/VPN problem.\n"
                        f"stderr tail:\n```\n{err[-STDERR_TAIL:]}\n```"), state
    state.pop("fail_sig", None)
    state.pop("fail_ts", None)
    out = watch_sanitize.sanitize(result["stdout"]).strip()
    out = out[:STDOUT_CAP]
    wake_if = meta.get("wake_if", "never")
    digest = hashlib.sha1(out.encode()).hexdigest()
    changed = digest != state.get("out")
    state["out"] = digest
    if wake_if == "changed":
        return ("wake", out or "(output is now empty)", state) if changed else ("silent", "", state)
    if not out:
        return "silent", "", state
    if wake_if == "nonempty":
        return "wake", out, state
    return "notice", out, state


# --------------------------------------------------------------------------
# `script` watch type: exit-code contract (0 noop / 200 changed / other error)
# --------------------------------------------------------------------------

def validate_script_watch_meta(meta: dict, base: Path | None = None) -> None:
    """Raise ValueError (with a fix hint) for a bad `script` watch meta."""
    resolve_script(meta.get("script"), base)
    args = meta.get("args", [])
    if (not isinstance(args, list) or len(args) > MAX_ARGS
            or any(not isinstance(a, str) or len(a) > MAX_ARG_LEN for a in args)):
        raise ValueError(f"meta.args must be a list of <= {MAX_ARGS} strings of <= {MAX_ARG_LEN} chars.")
    for k in ("retries", "max_runs"):
        v = meta.get(k)
        if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < 0 or (k == "max_runs" and v < 1)):
            raise ValueError(f"meta.{k} must be a non-negative integer (max_runs >= 1), got {v!r}.")
    if "wake_if" in meta:
        raise ValueError("`script` watches do not use wake_if - the script's exit code decides "
                         "(0 = nothing, 200 = changed, anything else = error).")


def _private_dir(d: Path, root: Path) -> None:
    """Create d (and its root) owner-only; chmod fixes pre-existing 0755 dirs."""
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    for p in (root, d):
        try:
            p.chmod(0o700)
        except OSError:
            pass


def state_dir_for(watch_id) -> Path:
    d = STATE_ROOT / str(watch_id)
    _private_dir(d, STATE_ROOT)
    return d


def write_output(watch_id, result: dict) -> Path:
    """Persist the raw combined output (capped per file). Files are kept forever (no pruning)."""
    d = OUT_ROOT / str(watch_id)
    _private_dir(d, OUT_ROOT)
    stem = f"{time.strftime('%Y%m%d-%H%M%S')}-rc{result['rc']}"
    text = watch_sanitize.sanitize(result["stdout"])
    if result["stderr"]:
        text += f"\n--- stderr ---\n{watch_sanitize.sanitize(result['stderr'])}"
    n = 0
    while True:  # O_EXCL: a same-second run never overwrites an earlier output
        path = d / (f"{stem}.log" if n == 0 else f"{stem}-{n}.log")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            n += 1
    with os.fdopen(fd, "w") as fh:
        fh.write(text[:OUT_FILE_CAP])
    return path


def _inline(result: dict, out_path: Path | None) -> str:
    text = result["stdout"].strip()
    if result["stderr"].strip():
        text += ("\n--- stderr ---\n" if text else "") + result["stderr"].strip()
    text = watch_sanitize.sanitize(text)
    if len(text) > STDOUT_CAP:
        text = text[:STDOUT_CAP] + "\n...(truncated)"
    if out_path is not None:
        text += f"\nfull output: {out_path}"
    return text.strip()


def decide_contract(meta: dict, result: dict, state: dict, out_path: Path | None = None,
                    now: float | None = None) -> tuple[str, str, dict]:
    """-> (outcome, inline_text, new_state); outcome in noop|changed|retry|error.
    error = non-{0,200} rc/timeout beyond meta.retries consecutive failures."""
    now = now if now is not None else time.time()
    st = dict(state)
    st["runs"] = st.get("runs", 0) + 1
    st["last_run_at"] = now
    st["last_rc"] = result["rc"]
    st["last_out_path"] = str(out_path) if out_path else None
    rc = result["rc"]
    if rc == RC_NOOP and not result["timed_out"]:
        st["fails"] = 0
        return "noop", "", st
    text = _inline(result, out_path)
    if rc == RC_CHANGED and not result["timed_out"]:
        st["fails"] = 0
        st["last_changed_at"] = now
        return "changed", text, st
    st["fails"] = st.get("fails", 0) + 1
    why = "timed out" if result["timed_out"] else f"exit {rc}"
    msg = f"script `{meta.get('script')}` failed ({why}, failure {st['fails']}):\n{text}"
    return ("retry" if st["fails"] <= int(meta.get("retries") or 0) else "error"), msg, st


def run_script_sync(meta: dict, state_dir: Path | None = None, timeout: float = SCRIPT_TIMEOUT,
                    extra_env: dict | None = None) -> dict:
    """Blocking one-off run for dry-runs (own temp state dir unless given); never raises."""
    try:
        path = resolve_script(meta.get("script"))
    except ValueError as exc:
        return {"rc": 127, "stdout": "", "stderr": str(exc), "timed_out": False}
    tmp = None
    if state_dir is None:
        tmp = tempfile.TemporaryDirectory(prefix="watch-dry-")
        state_dir = Path(tmp.name)
    try:
        p = subprocess.run(script_argv(path, meta.get("args", [])), stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=timeout, cwd=str(path.parent),
                           env=_minimal_env({**(extra_env or {}), "WATCH_STATE_DIR": str(state_dir), "WATCH_ID": "dry-run"}),
                           start_new_session=True)
        return {"rc": p.returncode, "stdout": p.stdout.decode(errors="replace"),
                "stderr": p.stderr.decode(errors="replace"), "timed_out": False}
    except subprocess.TimeoutExpired:
        return {"rc": -9, "stdout": "", "stderr": f"timed out after {timeout:.0f}s", "timed_out": True}
    except Exception as exc:
        return {"rc": 126, "stdout": "", "stderr": f"failed to launch: {exc}", "timed_out": False}
    finally:
        if tmp is not None:
            tmp.cleanup()


def _marker(path: Path) -> Path:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    return STATE_ROOT / ".dryrun-ok" / f"{path.name}-{digest}"


def mark_dry_run_ok(name: str) -> None:
    m = _marker(resolve_script(name))
    m.parent.mkdir(parents=True, exist_ok=True)
    m.touch()


def require_dry_run(name: str) -> None:
    """Plugin-dir (agent/user-written) scripts must have been dry-run successfully
    in their current content before a watch may use them; shipped scripts are exempt."""
    path = resolve_script(name)
    shipped = {SCRIPT_DIR.resolve(), *(d.resolve() for d in driver_watch_dirs())}
    if path.parent.resolve() in shipped:
        return
    if not _marker(path).exists():
        raise ValueError(f"script {name!r} is in the plugin dir and has not been dry-run in its current "
                         "version. Call watch_script(..., dry_run=True) first, show the output to the "
                         "user, then register it.")
