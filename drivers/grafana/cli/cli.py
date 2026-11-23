#!/usr/bin/env python3
"""Grafana CLI (REST API) for one configured integration instance.

Global: --instance NAME (grafana integration in config.yaml; env GRAFANA_INSTANCE; optional when exactly one is
enabled). Output is compact JSON; add --max-chars N to cut long output. Writes (dashboard save/move/delete, any
non-GET `api` call) do nothing without --yes. See README.md.
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import grafana_api as api  # noqa: E402
import grafana_furl as furl  # noqa: E402
import grafana_config as grconfig  # noqa: E402
import grafana_oauth as oauth  # noqa: E402

DEFAULT_MAX = 6000


def _expr(t):
    for k in ("expr", "expression", "rawSql", "query", "queryText", "target", "rawQuery"):
        if isinstance(t.get(k), str) and t[k]:
            return " ".join(t[k].split())[:240]
    return ""


def _panels(items, out):
    for p in items or []:
        if p.get("type") == "row":
            _panels(p.get("panels"), out)
            continue
        ds = p.get("datasource")
        out.append({"id": p.get("id"), "type": p.get("type"), "title": p.get("title", ""),
                    "datasource": (ds.get("uid") if isinstance(ds, dict) else ds) or "",
                    "queries": [e for e in (_expr(t) for t in p.get("targets") or []) if e]})
    return out


def dashboard_summary(d):
    dash, meta = d.get("dashboard") or {}, d.get("meta") or {}
    return {"uid": dash.get("uid"), "title": dash.get("title"), "folder": meta.get("folderTitle"),
            "folderUid": meta.get("folderUid"), "version": dash.get("version"), "updated": meta.get("updated"),
            "updatedBy": meta.get("updatedBy"), "tags": dash.get("tags"), "url": meta.get("url"),
            "variables": [v.get("name") for v in (dash.get("templating") or {}).get("list") or []],
            "panels": _panels(dash.get("panels"), [])}


def cmd_whoami(c, a):
    return c.get("/api/user")


def cmd_search(c, a):
    params = {"limit": max(1, min(a.limit, 200))}
    for k, v in (("query", a.query), ("type", a.type), ("folderUIDs", a.folder)):
        if v:
            params[k] = v
    if a.tag:
        params["tag"] = a.tag
    rows = c.get("/api/search", params)
    return [{k: r.get(k) for k in ("type", "uid", "title", "folderTitle", "tags", "url") if r.get(k)} for r in rows]


def cmd_dashboard(c, a):
    if a.action == "get":
        d = c.get(f"/api/dashboards/uid/{api.q(a.uid)}")
        return d if a.full else dashboard_summary(d)
    if a.action == "versions":
        return c.get(f"/api/dashboards/uid/{api.q(a.uid)}/versions", {"limit": 10})
    if a.action == "save":
        dash = json.loads(Path(a.file).read_text())
        dash.pop("id", None)
        if a.uid:
            dash["uid"] = a.uid
        body = {"dashboard": dash, "overwrite": a.overwrite, "message": a.message or "via dop-agent"}
        if a.folder_uid:
            body["folderUid"] = a.folder_uid
        return _write(c, a, "POST", "/api/dashboards/db", body, f"save dashboard {dash.get('uid') or dash.get('title')!r}")
    if a.action == "move":
        cur = c.get(f"/api/dashboards/uid/{api.q(a.uid)}")
        body = {"dashboard": cur["dashboard"], "folderUid": a.folder_uid, "overwrite": True,
                "message": a.message or f"Move to folder {a.folder_uid}"}
        return _write(c, a, "POST", "/api/dashboards/db", body, f"move dashboard {a.uid} to folder {a.folder_uid}")
    return _write(c, a, "DELETE", f"/api/dashboards/uid/{api.q(a.uid)}", None, f"delete dashboard {a.uid}")


def cmd_folder(c, a):
    return c.get("/api/folders") if a.action == "list" else c.get(f"/api/folders/{api.q(a.uid)}")


def cmd_datasources(c, a):
    rows = c.get("/api/datasources")
    return [{k: r.get(k) for k in ("uid", "name", "type", "isDefault", "url")} for r in rows]


def cmd_query(c, a):
    kind = {"prom": ("/api/v1/query", "/api/v1/query_range"), "loki": ("/loki/api/v1/query", "/loki/api/v1/query_range")}[a.kind]
    base = f"/api/datasources/proxy/uid/{api.q(a.datasource)}"
    now = int(time.time())
    if a.range:
        secs = _duration(a.range)
        params = {"query": a.expr, "start": now - secs, "end": now, "step": a.step}
        if a.kind == "loki":
            params = {"query": a.expr, "start": (now - secs) * 10**9, "end": now * 10**9, "limit": a.limit}
        return c.get(base + kind[1], params)
    params = {"query": a.expr}
    if a.kind == "loki":
        params["limit"] = a.limit
    return c.get(base + kind[0], params)


def _duration(s):
    unit = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if not s or s[-1] not in unit or not s[:-1].isdigit():
        raise ValueError(f"bad duration {s!r} (use e.g. 30m, 6h, 2d)")
    return int(s[:-1]) * unit[s[-1]]


def cmd_alerts(c, a):
    states = [a.state] if a.state != "all" else ["all"]
    rows = api.alert_instances(c, states, a.limit)
    return [{"rule": r["rule"], "uid": r["uid"], "folder": r["folder"], "state": r["state"], "labels": r["labels"],
             "since": r["active_at"]} for r in rows]


def cmd_permissions(c, a):
    perms = c.get("/api/access-control/user/permissions")
    return {k: v for k, v in perms.items() if not a.filter or a.filter in k}


def cmd_api(c, a):
    method = a.method.upper()
    params = dict(kv.split("=", 1) for kv in a.query)
    body = None
    if a.data_file:
        body = json.loads(Path(a.data_file).read_text())
    elif a.data:
        body = json.loads(a.data)
    if method == "GET":
        return c.get(a.path, params)
    return _write(c, a, method, a.path, body, f"{method} {a.path}")


def _write(c, a, method, path, body, what):
    if not a.yes:
        return {"dry_run": True, "would": what, "instance": c.inst.name,
                "hint": "re-run with --yes after the user confirmed the target and content"}
    return c.request(method, path, None, body)


def cmd_auth(c, a):
    inst = c.inst
    if inst.auth_type != grconfig.OAUTH_AUTH:
        return {"auth_type": inst.auth_type, "note": "token auth: nothing to authorize or refresh"}
    if a.action == "authorize":
        oauth.authorize(inst)
    elif a.action == "refresh":
        oauth.refresh(inst)
    st = oauth.load_state(inst.name)
    return {"auth_type": inst.auth_type, "has_session": bool(st.get("refresh_token")),
            "access_token_valid_for_s": max(0, int(st.get("expires_at", 0) - time.time()))}


def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--instance", help="grafana integration instance name (config.yaml)")
    p.add_argument("--max-chars", type=int, default=DEFAULT_MAX, help="cut output after N chars (0 = no limit)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, help_):
        s = sub.add_parser(name, help=help_)
        s.add_argument("--max-chars", type=int, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        return s

    add("whoami", "identity of the token (GET /api/user)")
    add("test", "connection check (JSON; exit 0 even when a step fails)")
    s = add("fetch", "furl card for a Grafana link (used by dop-agent)")
    s.add_argument("--kind", required=True, choices=sorted(furl.KINDS))
    s.add_argument("--ident", required=True)
    s = add("search", "search dashboards/folders")
    s.add_argument("query", nargs="?")
    s.add_argument("--type", choices=["dash-db", "dash-folder"])
    s.add_argument("--tag")
    s.add_argument("--folder", help="folder uid")
    s.add_argument("--limit", type=int, default=30)
    s = add("dashboard", "get (compact summary or --full JSON) / versions / save / move / delete")
    s.add_argument("action", choices=["get", "versions", "save", "move", "delete"])
    s.add_argument("uid", nargs="?", help="dashboard uid (save: optional override of the file's uid)")
    s.add_argument("--file", help="save: bare dashboard model JSON")
    s.add_argument("--folder-uid")
    s.add_argument("--overwrite", action="store_true")
    s.add_argument("--message")
    s.add_argument("--full", action="store_true")
    s.add_argument("--yes", action="store_true", help="really write (otherwise a dry run is printed)")
    s = add("folder", "list / get folders")
    s.add_argument("action", choices=["list", "get"])
    s.add_argument("uid", nargs="?")
    add("datasources", "list data sources (uid, name, type)")
    s = add("query", "datasource query through the proxy: prom (PromQL) or loki (LogQL)")
    s.add_argument("kind", choices=["prom", "loki"])
    s.add_argument("datasource", help="data source uid")
    s.add_argument("expr")
    s.add_argument("--range", help="range query over the last N (30m, 6h, 2d); default instant query")
    s.add_argument("--step", default="60s")
    s.add_argument("--limit", type=int, default=50, help="loki: max lines")
    s = add("alerts", "Grafana-managed alert instances")
    s.add_argument("--state", default="firing", choices=["firing", "pending", "normal", "all"])
    s.add_argument("--limit", type=int, default=100)
    s = add("permissions", "effective RBAC permissions of this identity")
    s.add_argument("--filter", help="substring of the action, e.g. dashboards")
    s = add("api", "any Grafana API call; non-GET needs --yes")
    s.add_argument("method", help="GET|POST|PUT|PATCH|DELETE")
    s.add_argument("path", help="e.g. /api/health")
    s.add_argument("--query", nargs="*", default=[], help="key=value query params")
    s.add_argument("--data")
    s.add_argument("--data-file")
    s.add_argument("--yes", action="store_true")
    s = add("auth", "oauth_cdp instances: status / authorize (browser consent) / refresh")
    s.add_argument("action", choices=["status", "authorize", "refresh"])
    return p


CMDS = {"whoami": cmd_whoami, "search": cmd_search, "dashboard": cmd_dashboard, "folder": cmd_folder,
        "datasources": cmd_datasources, "query": cmd_query, "alerts": cmd_alerts, "permissions": cmd_permissions,
        "api": cmd_api, "auth": cmd_auth}


def main(argv=None):
    a = build_parser().parse_args(argv)
    try:
        if a.cmd == "test":
            print(json.dumps(furl.run_test(a.instance)))
            return 0
        if a.cmd == "fetch":
            print(json.dumps(furl.fetch(a.instance, a.kind, json.loads(a.ident))))
            return 0
        if a.cmd in ("dashboard", "folder") and a.action not in ("list", "save") and not a.uid:
            raise ValueError(f"{a.cmd} {a.action} needs a uid")
        if a.cmd == "dashboard" and a.action == "save" and not a.file:
            raise ValueError("dashboard save needs --file")
        if a.cmd == "dashboard" and a.action == "move" and not a.folder_uid:
            raise ValueError("dashboard move needs --folder-uid")
        print(api.dumps(CMDS[a.cmd](api.client(a.instance), a), a.max_chars))
        return 0
    except (api.ApiError, grconfig.InstanceError, oauth.OAuthError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
