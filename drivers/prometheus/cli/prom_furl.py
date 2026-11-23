"""`fetch` and `test` subcommands used by dop-agent's package driver.

Both print ONE JSON object to stdout and exit 0 for expected outcomes (a failed `test` is data, not a crash).
Secrets are never printed.

fetch --kind K --ident '<json>'  -> this driver declares no link kinds, so it always answers with an error object.
test -> {ok, message, steps:[{step, ok, detail}]}
        (python deps -> config -> prometheus reach+auth (buildinfo) -> targets -> alertmanager status (if configured))
"""
import sys

import prom_api as api
import prom_config as pconfig


def fetch(instance, kind, ident):
    raise RuntimeError(f"prometheus has no link kinds (got {kind!r})")


def _importable(mod):
    try:
        __import__(mod)
        return True
    except ImportError:
        return False


def run_test(instance):
    steps = []

    def step(name, ok, detail=""):
        steps.append({"step": name, "ok": ok, "detail": detail})
        return ok

    def done(message):
        return {"ok": all(s["ok"] for s in steps), "message": message, "steps": steps}

    if sys.version_info < (3, 10):
        step("python", False, f"Python {sys.version_info[0]}.{sys.version_info[1]} is too old (3.10+ needed)")
        return done("CLI needs Python 3.10+")
    missing = [m for m in ("requests", "yaml") if not _importable(m)]
    if missing:
        step("python", False, f"missing for {sys.executable}: pip install {' '.join(missing).replace('yaml', 'pyyaml')}")
        return done("required Python packages are missing")
    step("python", True, f"Python {sys.version_info[0]}.{sys.version_info[1]}")
    try:
        inst = pconfig.load_instance(instance)
        step("config", True, f"instance {inst.name} -> {inst.prometheus_url}"
             + (f", alertmanager {inst.alertmanager_url}" if inst.alertmanager_url else ", no alertmanager_url")
             + f" (auth {inst.auth_type})")
    except pconfig.InstanceError as exc:
        step("config", False, api.redact(str(exc)))
        return done(str(exc))
    c = api.Client(inst)
    try:
        info = c.prom("/api/v1/status/buildinfo")
        step("prometheus", True, f"reachable, version {info.get('version', '?')}")
    except api.ApiError as exc:
        hint = " (check auth type / secret ref and token scope)" if exc.status in (401, 403) else ""
        step("prometheus", False, f"{exc}{hint}")
        return done(f"{inst.prometheus_url}: {exc}{hint}")
    try:
        ts = c.prom("/api/v1/targets", {"state": "active"}).get("activeTargets") or []
        down = sum(1 for t in ts if t.get("health") == "down")
        step("targets", True, f"{len(ts)} active targets, {down} down")
    except api.ApiError as exc:
        step("targets", False, str(exc))
    if inst.alertmanager_url:
        try:
            st = c.am("GET", "/api/v2/status")
            step("alertmanager", True, f"reachable, version {(st.get('versionInfo') or {}).get('version', '?')}")
        except api.ApiError as exc:
            step("alertmanager", False, str(exc))
    ok = all(s["ok"] for s in steps)
    return done("connected" if ok else "connected, but some checks failed")
