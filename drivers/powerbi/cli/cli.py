#!/usr/bin/env python3
"""Power BI CLI (workspaces, reports, dashboards, datasets, refresh history/trigger, DAX, access) over the Power BI
REST API with the token of a signed-in Power BI / Fabric web tab (see api.py).

Global options go before the command: --account <upn> (env POWERBI_ACCOUNT), --cdp-url <url> (env POWERBI_CDP_URL),
--json. Output is compact TSV (or JSON lines); every list is bounded by --top. Refresh needs --yes.
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import api  # noqa: E402

MAX_ROWS_CAP = 500
MAX_QUERY_CHARS = 4000


def table(rows, cols, as_json=False):
    if as_json:
        for r in rows:
            print(json.dumps(r, ensure_ascii=False))
        return
    if not rows:
        print("(none)")
        return
    print("\t".join(cols))
    for r in rows:
        print("\t".join("" if r.get(c) is None else str(r[c]).replace("\n", " ").replace("\t", " ") for c in cols))


def die(msg, code=1):
    print(f"error: {api.redact(msg)}", file=sys.stderr)
    sys.exit(code)


def _filter(items, name, key="name"):
    if not name:
        return items
    n = name.lower()
    return [i for i in items if n in (i.get(key) or "").lower()]


def _cut(s, n):
    s = "" if s is None else str(s).replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


def cmd_whoami(args):
    upn, tid = api.identity()
    print(f"as={upn} tenant={tid}")


def cmd_workspaces(args):
    items = _filter(api.get_list(f"/groups?$top={max(args.top, 1) if not args.name else 1000}", 1000), args.name)[:args.top]
    table([{"id": g.get("id"), "name": g.get("name"), "type": g.get("type"), "capacity": g.get("isOnDedicatedCapacity")}
           for g in items], ["id", "name", "type", "capacity"], args.json)


def _listing(kind, cols, mapper):
    def run(args):
        items = _filter(api.get_list(f"{api.ws_prefix(args.workspace)}/{kind}", 2000), args.name)[:args.top]
        table([mapper(i) for i in items], cols, args.json)
    return run


cmd_reports = _listing("reports", ["id", "name", "datasetId", "webUrl"],
                       lambda r: {"id": r.get("id"), "name": r.get("name"), "datasetId": r.get("datasetId"),
                                  "webUrl": r.get("webUrl")})
cmd_dashboards = _listing("dashboards", ["id", "name", "isReadOnly", "webUrl"],
                          lambda d: {"id": d.get("id"), "name": d.get("displayName") or d.get("name"),
                                     "isReadOnly": d.get("isReadOnly"), "webUrl": d.get("webUrl")})
cmd_datasets = _listing("datasets", ["id", "name", "owner", "refreshable"],
                        lambda d: {"id": d.get("id"), "name": d.get("name"), "owner": d.get("configuredBy"),
                                   "refreshable": d.get("isRefreshable")})


def _ds(args):
    return f"{api.ws_prefix(args.workspace)}/datasets/{api.guid(args.dataset, 'dataset')}"


def _duration(r):
    from datetime import datetime
    try:
        a = datetime.fromisoformat(r["startTime"].rstrip("Z"))
        b = datetime.fromisoformat(r["endTime"].rstrip("Z"))
        return f"{int((b - a).total_seconds())}s"
    except Exception:
        return ""


def _refresh_error(r):
    raw = r.get("serviceExceptionJson")
    if not raw:
        return ""
    try:
        e = json.loads(raw)
        return _cut(f"{e.get('errorCode', '')} {e.get('errorDescription', '')}".strip(), 160)
    except ValueError:
        return _cut(raw, 160)


def cmd_refresh_history(args):
    rows = api.get_list(f"{_ds(args)}/refreshes?$top={min(max(args.top, 1), 100)}", args.top)
    table([{"status": r.get("status"), "type": r.get("refreshType"), "start": r.get("startTime"),
            "end": r.get("endTime"), "duration": _duration(r), "error": _refresh_error(r)} for r in rows],
          ["status", "type", "start", "end", "duration", "error"], args.json)


def cmd_refresh(args):
    path = _ds(args) + "/refreshes"
    ws = args.workspace or "me"
    if not args.yes:
        print(f"DRY RUN: would POST a refresh of dataset {args.dataset} in workspace {ws} as the signed-in account. "
              "Confirm with the user, then re-run with --yes.")
        return
    api.request(path, "post", {"notifyOption": "NoNotification"})
    print(f"refresh requested for dataset {args.dataset} (workspace {ws}); check `refresh-history` for the result")


def _cell(v, n):
    return _cut(v, n)


def cmd_dax(args):
    if args.query_file:
        q = sys.stdin.read() if args.query_file == "-" else open(args.query_file).read()
    else:
        q = args.query
    if not q or not q.strip():
        die("give --query or --query-file")
    if len(q) > MAX_QUERY_CHARS:
        die(f"query is longer than {MAX_QUERY_CHARS} chars")
    cap = min(max(args.max_rows, 1), MAX_ROWS_CAP)
    d = api.request(_ds(args) + "/executeQueries",
                    "post", {"queries": [{"query": q}], "serializerSettings": {"includeNulls": True}}) or {}
    res = (d.get("results") or [{}])[0]
    if res.get("error"):
        die("DAX error: " + _cut(json.dumps(res["error"]), 300))
    rows = ((res.get("tables") or [{}])[0]).get("rows") or []
    cols = []
    for r in rows[:cap]:
        for k in r:
            if k not in cols:
                cols.append(k)
    shown = [{re.sub(r"^.*\[(.*)\]$", r"\1", c) or c: _cell(r.get(c), args.max_cell) for c in cols} for r in rows[:cap]]
    names = [re.sub(r"^.*\[(.*)\]$", r"\1", c) or c for c in cols]
    table(shown, names, args.json)
    if len(rows) > cap:
        print(f"... [{len(rows) - cap} more rows, raise --max-rows (cap {MAX_ROWS_CAP}) or narrow the query]", file=sys.stderr)


def _user_row(u):
    return {"name": u.get("displayName") or u.get("identifier"), "identifier": u.get("emailAddress") or u.get("identifier"),
            "type": u.get("principalType"),
            "access": u.get("groupUserAccessRight") or u.get("datasetUserAccessRight") or u.get("reportUserAccessRight")
            or u.get("dashboardUserAccessRight")}


def cmd_access(args):
    """Owner/permissions: dataset owner = configuredBy; users via the non-admin *users endpoints."""
    ws = api.ws_prefix(args.workspace)
    ident = api.guid(args.id, args.kind)
    if args.kind == "workspace":
        users = api.get_list(f"/groups/{ident}/users", args.top)
    elif args.kind == "dataset":
        ds = api.request(f"{ws}/datasets/{ident}") or {}
        print(f"owner (configuredBy): {ds.get('configuredBy') or '?'}")
        users = api.get_list(f"{ws}/datasets/{ident}/users", args.top)
    elif args.kind == "dashboard":
        users = api.get_list(f"{ws}/dashboards/{ident}/users", args.top)
    else:  # report: no non-admin users endpoint; show the dataset owner and the workspace members instead
        rep = api.request(f"{ws}/reports/{ident}") or {}
        ds_id = rep.get("datasetId")
        if ds_id:
            ds = api.request(f"{ws}/datasets/{api.guid(ds_id, 'dataset')}") or {}
            print(f"dataset {ds_id} owner (configuredBy): {ds.get('configuredBy') or '?'}")
        users = api.get_list(f"{ws}/users", args.top) if ws else []
        print("(report-level ACL needs admin API; showing workspace members)" if ws else "(My workspace: no ACL)")
    table([_user_row(u) for u in users], ["name", "identifier", "type", "access"], args.json)


def _card(kind, ident):
    ws = api.ws_prefix(ident.get("group") or "me") if ident.get("group") else ""
    i = api.guid(ident["id"], kind)
    if kind == "report":
        r = api.request(f"{ws}/reports/{i}") or {}
        return {"title": r.get("name") or i, "url": r.get("webUrl") or "", "fields": {"dataset": r.get("datasetId", "")}}
    if kind == "dashboard":
        d = api.request(f"{ws}/dashboards/{i}") or {}
        return {"title": d.get("displayName") or i, "url": d.get("webUrl") or "", "fields": {"readOnly": d.get("isReadOnly", "")}}
    if kind == "dataset":
        d = api.request(f"{ws}/datasets/{i}") or {}
        runs = (api.request(f"{ws}/datasets/{i}/refreshes?$top=1") or {}).get("value") or []
        last = runs[0] if runs else {}
        st = last.get("status") or "n/a"
        return {"title": d.get("name") or i, "status": st, "status_emoji": {"Completed": "✅", "Failed": "❌"}.get(st, "⏳" if st == "Unknown" else "•"),
                "url": d.get("webUrl") or "", "fields": {"owner": d.get("configuredBy", ""), "lastRefresh": last.get("endTime", "")}}
    raise api.PowerBIError(f"unknown kind {kind!r}")


def cmd_fetch(args):
    print(json.dumps(_card(args.kind, json.loads(args.ident))))


def cmd_test(args):
    steps = []

    def step(name, ok, detail=""):
        steps.append({"step": name, "ok": ok, "detail": api.redact(detail)})
        return ok

    def done(message, **extra):
        print(json.dumps({"ok": all(s["ok"] for s in steps), "message": message, "steps": steps, **extra}))

    if sys.version_info < (3, 10):
        step("python", False, f"Python {sys.version_info[0]}.{sys.version_info[1]} too old (3.10+ needed)")
        return done("CLI needs Python 3.10+")
    try:
        import websocket  # noqa: F401
        step("python", True, "websocket-client available")
    except ImportError:
        step("python", False, f"websocket-client is missing for {sys.executable}: pip install websocket-client")
        return done("websocket-client is not installed for the CLI's Python")
    try:
        auth = api._auth()
    except ImportError as exc:
        step("teams_auth", False, f"ms_teams driver CLI not found at {api._TEAMS_DIR}: {exc}")
        return done("Power BI reuses the ms_teams driver's auth helper, which is missing")
    try:
        auth._cdp_json("/json/version")
        step("cdp", True, f"CDP endpoint {auth._cdp_url} reachable")
        tabs = [t for t in auth._cdp_json("/json") if auth._is_teams(t)]
    except Exception as exc:
        step("cdp", False, str(exc))
        return done(f"CDP not reachable: {exc}")
    if not step("tab", bool(tabs), f"{len(tabs)} Power BI/Teams tab(s)" if tabs else "no Power BI or Teams tab open"):
        return done("No tab found - open https://app.powerbi.com in that browser and sign in")
    try:
        upn, tid = api.identity()
        step("token", True, f"Power BI token for {upn or '?'}" + (f", tenant {tid}" if tid else ""))
    except Exception as exc:
        step("token", False, str(exc))
        return done(f"No usable token: {api.redact(exc)[:200]}")
    try:
        n = len(api.get_list("/groups?$top=5", 5))
        step("workspaces", True, f"API answered ({n} workspace(s) sampled)")
    except api.PowerBIError as exc:
        step("workspaces", False, str(exc)[:200])
        return done(f"Token present but the Power BI API failed: {str(exc)[:120]}")
    done(f"Signed in as {upn or '?'}, tenant {tid or '?'}", set_options={"tenant_id": tid} if tid else {}, tenant_id=tid)


def build():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--account", default=os.environ.get("POWERBI_ACCOUNT"))
    p.add_argument("--json", action="store_true", help="JSON lines instead of a table")
    p.add_argument("--cdp-url", help="Browser CDP endpoint (env POWERBI_CDP_URL, default http://localhost:9222)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_, ws=False, name_filter=False, top=None):
        s = sub.add_parser(name, help=help_)
        s.set_defaults(fn=fn)
        s.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        if ws:
            s.add_argument("--workspace", default="me", help="workspace GUID or 'me' (My workspace, default)")
        if name_filter:
            s.add_argument("--name", help="case-insensitive name substring")
        if top:
            s.add_argument("--top", type=int, default=top)
        return s

    add("whoami", cmd_whoami, "signed-in identity and tenant")
    add("workspaces", cmd_workspaces, "list workspaces you belong to", name_filter=True, top=50)
    add("reports", cmd_reports, "list reports", ws=True, name_filter=True, top=50)
    add("dashboards", cmd_dashboards, "list dashboards", ws=True, name_filter=True, top=50)
    add("datasets", cmd_datasets, "list datasets (owner = configuredBy)", ws=True, name_filter=True, top=50)
    s = add("refresh-history", cmd_refresh_history, "dataset refresh history, newest first", ws=True, top=10)
    s.add_argument("dataset", help="dataset GUID")
    s = add("refresh", cmd_refresh, "trigger a dataset refresh (dry run unless --yes)", ws=True)
    s.add_argument("dataset")
    s.add_argument("--yes", action="store_true", help="REALLY trigger the refresh (confirm with the user first)")
    s = add("dax", cmd_dax, "run a DAX query (EVALUATE ...) against a dataset, bounded compact table", ws=True)
    s.add_argument("dataset")
    s.add_argument("--query", help="DAX, e.g. 'EVALUATE TOPN(10, Sales)'")
    s.add_argument("--query-file", help="file path or - for stdin")
    s.add_argument("--max-rows", type=int, default=50, help=f"rows shown (default 50, cap {MAX_ROWS_CAP})")
    s.add_argument("--max-cell", type=int, default=80, help="chars per cell (default 80)")
    s = add("access", cmd_access, "owner and permissions of a workspace/dataset/dashboard/report (read)", ws=True, top=50)
    s.add_argument("kind", choices=["workspace", "dataset", "dashboard", "report"])
    s.add_argument("id", help="GUID")
    s = add("fetch", cmd_fetch, "furl card for a Power BI link (used by dop-agent)")
    s.add_argument("--kind", required=True, choices=["report", "dashboard", "dataset"])
    s.add_argument("--ident", required=True)
    add("test", cmd_test, "check CDP, tab, token and the Power BI API (JSON; exit 0 even when a step fails)")
    return p


def main():
    args = build().parse_args()
    acct = args.account if args.account != "default" else None
    try:
        api.configure(args.cdp_url, acct)
        args.fn(args)
    except api.PowerBIError as e:
        die(str(e))
    except (ValueError, KeyError, OSError) as e:
        die(f"bad input: {e}")
    except BrokenPipeError:
        pass


if __name__ == "__main__":
    main()
