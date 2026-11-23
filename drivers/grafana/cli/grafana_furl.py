"""`fetch` (furl card) and `test` (connection check) subcommands used by dop-agent's package driver.

Both print ONE JSON object to stdout and exit 0 for expected outcomes (a failed `test` is data, not a crash).
Tokens are never printed.

fetch --kind dashboard|folder|alert --ident '<json from the link regex>'  -> {title, url, status?, fields:{...}}
test -> {ok, message, steps:[{step, ok, detail}]}   (Python deps -> config -> reach+auth GET /api/user -> org access)
"""
import sys

import grafana_api as api
import grafana_config as grconfig


def _dashboard(c, ident):
    d = c.get(f"/api/dashboards/uid/{api.q(ident['uid'])}")
    dash, meta = d.get("dashboard") or {}, d.get("meta") or {}
    return {"title": dash.get("title") or ident["uid"], "url": c.base + (meta.get("url") or f"/d/{ident['uid']}"),
            "fields": {"folder": meta.get("folderTitle", ""), "tags": ", ".join(dash.get("tags") or []),
                       "updated": meta.get("updated", ""), "version": dash.get("version", "")}}


def _folder(c, ident):
    f = c.get(f"/api/folders/{api.q(ident['uid'])}")
    return {"title": f.get("title") or ident["uid"], "url": c.base + (f.get("url") or f"/dashboards/f/{ident['uid']}"),
            "fields": {"parents": " / ".join(p.get("title", "") for p in f.get("parents") or []),
                       "updated": f.get("updated", "")}}


def _alert(c, ident):
    r = c.get(f"/api/v1/provisioning/alert-rules/{api.q(ident['uid'])}")
    return {"title": r.get("title") or ident["uid"], "url": f"{c.base}/alerting/grafana/{ident['uid']}/view",
            "status": "paused" if r.get("isPaused") else r.get("noDataState", ""),
            "fields": {"folder": r.get("folderUID", ""), "group": r.get("ruleGroup", ""), "for": r.get("for", ""),
                       "updated": r.get("updated", "")}}


KINDS = {"dashboard": _dashboard, "folder": _folder, "alert": _alert}


def fetch(instance, kind, ident):
    if kind not in KINDS:
        raise RuntimeError(f"unknown kind {kind!r}")
    return KINDS[kind](api.client(instance), ident)


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
        inst = grconfig.load_instance(instance)
        step("config", True, f"instance {inst.name} -> {inst.base_url} ({inst.auth_type})")
    except grconfig.InstanceError as exc:
        step("config", False, str(exc))
        return done(str(exc))
    c = api.Client(inst)
    if inst.auth_type == grconfig.OAUTH_AUTH:
        if not _importable("websocket"):
            step("oauth", False, f"websocket-client is missing for {sys.executable} (only needed for `auth authorize`)")
    try:
        user = c.get("/api/user")
    except api.ApiError as exc:
        hint = " (run `auth authorize` for oauth_cdp instances)" if inst.auth_type == grconfig.OAUTH_AUTH and exc.status == 401 else ""
        step("auth", False, f"{exc}{hint}")
        return done(f"{inst.base_url}: {exc}{hint}")
    who = user.get("login") or user.get("email") or user.get("name") or "service account"
    step("auth", True, f"authenticated as {who}")
    try:
        org = c.get("/api/org")
        step("org", True, f"org {org.get('name', '?')} (id {org.get('id', '?')})")
    except api.ApiError as exc:
        step("org", False, str(exc))
    return done(f"authenticated as {who}" if all(s["ok"] for s in steps) else "connected, but some checks failed")


def _importable(mod):
    try:
        __import__(mod)
        return True
    except ImportError:
        return False
