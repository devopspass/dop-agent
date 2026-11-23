"""Resolve a Kargo integration instance and run the `kargo` binary against it.

The CLI and the watcher scripts run in minimal environments, so the token is looked up here instead of being
passed through argv/env of the agent: `${env:X}` / `${file:P}` / `${keychain:S}` refs of
`integrations.<instance>.auth.token` are resolved with dop-agent's own `config.resolve_ref`; `dop-agent/.env`
is loaded first. The token reaches ONLY the `kargo` child process, via KARGO_API_TOKEN (never argv), and the
child gets an empty XDG_CONFIG_HOME so a personal `kargo login` of the user is never used or modified.
Nothing here prints a token.

    load_instance(name=None) -> (name, base_url, token, options)   name=None: the single enabled kargo instance
    run_kargo(inst, args, timeout=45) -> stdout; raises KargoError (message redacted, token never included)
    kargo_json(inst, args) -> parsed JSON items list
Overrides: KARGO_INSTANCE (default for --instance), KARGO_BIN (binary), DOP_AGENT_CONFIG (config path).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[3]
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$")
SECRET_RE = re.compile(r"(?i)(token|password|secret|authorization|bearer)([=:\s]+)\S+")


class InstanceError(RuntimeError):
    pass


class KargoError(RuntimeError):
    pass


def check_name(value: str, what: str = "name") -> str:
    """Names go into argv: reject anything that could be read as a flag or contains odd characters."""
    if not isinstance(value, str) or not NAME_RE.match(value):
        raise KargoError(f"invalid {what} {str(value)[:40]!r}: letters, digits, '.', '-', '_' only")
    return value


def redact(text: str, token: str = "") -> str:
    if token:
        text = text.replace(token, "***")
    return SECRET_RE.sub(lambda m: m.group(1) + m.group(2) + "***", text)


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
             if isinstance(c, dict) and c.get("driver") == "kargo" and c.get("enabled", True)}
    name = name or os.environ.get("KARGO_INSTANCE") or None
    if name is None:
        if len(insts) != 1:
            raise InstanceError("pass --instance <name>; enabled kargo instances: " + (", ".join(sorted(insts)) or "none"))
        name = next(iter(insts))
    cfg = insts.get(name)
    if cfg is None:
        raise InstanceError(f"kargo instance {name!r} not found/enabled; known: " + (", ".join(sorted(insts)) or "none"))
    base = str(cfg.get("base_url") or "").rstrip("/")
    raw = str((cfg.get("auth") or {}).get("token") or "")
    _load_dotenv()
    try:
        token = _resolve_ref(raw)
    except Exception as exc:
        raise InstanceError(f"instance {name!r}: cannot resolve auth.token ({exc})") from None
    if not base or not token:
        raise InstanceError(f"instance {name!r}: base_url and a non-empty auth.token (env/file/keychain ref) are required")
    return name, base, token, dict(cfg.get("options") or {})


def find_binary() -> str:
    cands = [os.environ.get("KARGO_BIN") or "", shutil.which("kargo") or "",
             str(Path.home() / ".local" / "bin" / "kargo"), "/opt/homebrew/bin/kargo", "/usr/local/bin/kargo"]
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    raise KargoError("`kargo` binary not found: install it (https://github.com/akuity/kargo/releases, e.g. to "
                     "~/.local/bin/kargo) or set KARGO_BIN")


def project_of(inst, project: str | None) -> str:
    p = project or str(inst[3].get("default_project") or "")
    if not p:
        raise KargoError("no project: pass --project <name> (or set the instance option default_project)")
    return check_name(p, "project")


def run_kargo(inst, args: list[str], timeout: int = 45) -> str:
    _name, base, token, opts = inst
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", ""),
           "KARGO_API_ADDRESS": base, "KARGO_API_TOKEN": token, "LANG": "C"}
    if opts.get("insecure_skip_tls_verify") is True:
        print("WARNING: kargo: TLS certificate verification is DISABLED (insecure_skip_tls_verify=true in config) - "
              "the token can be intercepted; use a trusted CA instead.", file=sys.stderr)
        env["KARGO_INSECURE_SKIP_TLS_VERIFY"] = "true"
    with tempfile.TemporaryDirectory(prefix="dop-kargo-") as xdg:
        env["XDG_CONFIG_HOME"] = xdg
        try:
            r = subprocess.run([find_binary(), *args], env=env, capture_output=True, text=True,
                               timeout=timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise KargoError(f"kargo timed out after {timeout}s: check base_url / VPN") from None
    if r.returncode != 0:
        msg = redact((r.stderr or r.stdout or "").strip(), token)
        raise KargoError(msg[:400] or f"kargo exited {r.returncode}")
    return r.stdout


def parse_items(out: str) -> list[dict]:
    """`kargo get -o json` prints a List, one object, or several concatenated objects."""
    dec, i, items = json.JSONDecoder(), 0, []
    out = out.strip()
    while i < len(out):
        obj, end = dec.raw_decode(out, i)
        items.extend(obj["items"] if isinstance(obj, dict) and isinstance(obj.get("items"), list) else [obj])
        i = end
        while i < len(out) and out[i].isspace():
            i += 1
    return [x for x in items if isinstance(x, dict)]


def kargo_json(inst, args: list[str], timeout: int = 45) -> list[dict]:
    return parse_items(run_kargo(inst, [*args, "-o", "json"], timeout))
