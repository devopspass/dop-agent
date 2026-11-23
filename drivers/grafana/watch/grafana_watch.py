#!/usr/bin/env python3
"""Grafana watcher (read-only). Script-watch exit codes: 0 = nothing new (silent), 200 = changed (notice
printed), 1 = error (instance/auth/API failure).

Modes (first arg):
  alerts             Grafana-managed alert instances that newly entered --state (default firing), once each.
                     --label k=v (repeatable, all must match), --rule SUBSTR (rule name), --resolved also reports
                     instances that stopped being in that state, --limit N=200 (instances scanned).
  dashboard-changed  dashboards whose version changed since the last run. Targets: --uid U (repeatable),
                     --folder FOLDER_UID, --tag T (repeatable); at most --limit N=30 dashboards per run.
                     New dashboards matching --folder/--tag are reported too.
  datasource-health  data sources that turned unhealthy (or healthy again). --uid U (repeatable, default all),
                     --limit N=20 data sources checked.

Instance: --instance or the GRAFANA_INSTANCE env (set by the driver). State:
$WATCH_STATE_DIR/grafana-<instance>-<mode>-<hash of filters>.json (ids/versions only, never dashboard content).
The first run only records a baseline (exit 0). 429/5xx/network errors are transient (silent exit 0, warning on stderr).
"""
import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

CLI_DIR = Path(__file__).resolve().parents[1] / "cli"
RC_CHANGED = 200
MODES = ["alerts", "dashboard-changed", "datasource-health"]


class Transient(Exception):
    pass


def _client(instance):
    sys.path.insert(0, str(CLI_DIR))
    import grafana_api as api
    return api, api.client(instance)


def _call(api, fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except api.ApiError as exc:
        if exc.transient:
            raise Transient(str(exc)) from exc
        raise


def state_path(instance, mode, key):
    d = Path(os.environ.get("WATCH_STATE_DIR") or Path.home() / ".dop-agent" / "watch-state" / "grafana-default")
    d.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha1(json.dumps(key, sort_keys=True).encode()).hexdigest()[:8]
    return d / f"grafana-{re.sub(r'[^A-Za-z0-9_.-]+', '_', instance)}-{mode}-{h}.json"


def load_state(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def save_state(path, st):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".gf.", suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        json.dump(st, fh)
    os.replace(tmp, path)


def _labels(d):
    return " ".join(f"{k}={v}" for k, v in sorted(d.items()))[:160]


def run_alerts(api, c, args, st, first):
    want = dict(kv.split("=", 1) for kv in args.label)
    rows = [r for r in _call(api, api.alert_instances, c, (args.state,), args.limit)
            if all(r["labels"].get(k) == v for k, v in want.items())
            and (not args.rule or args.rule.lower() in r["rule"].lower())]
    cur = {r["fp"]: r for r in rows}
    prev = set(st.get("fps") or [])
    st["fps"] = sorted(cur)[:1000]
    if first:
        return []
    lines = [f"[{r['state']}] {r['rule']} {_labels(r['labels'])} (folder {r['folder']}, since {r['active_at']}) "
             f"{c.base}/alerting/grafana/{r['uid']}/view" for fp, r in cur.items() if fp not in prev]
    if args.resolved:
        lines += [f"[no longer {args.state}] {fp.split('|', 1)[0]} {fp.split('|', 1)[1][:120]}" for fp in sorted(prev - set(cur))]
    return lines


def _targets(api, c, args):
    uids = list(args.uid)
    if args.folder or args.tag:
        params = {"type": "dash-db", "limit": args.limit}
        if args.folder:
            params["folderUIDs"] = args.folder
        for t in args.tag or [None]:
            if t:
                params["tag"] = t
            uids += [r["uid"] for r in _call(api, c.get, "/api/search", dict(params))]
    return list(dict.fromkeys(uids))[: args.limit]


def run_dashboards(api, c, args, st, first):
    seen = st.setdefault("versions", {})
    lines = []
    for uid in _targets(api, c, args):
        try:
            d = _call(api, c.get, f"/api/dashboards/uid/{api.q(uid)}")
        except api.ApiError as exc:
            if exc.status == 404:
                continue
            raise
        dash, meta = d.get("dashboard") or {}, d.get("meta") or {}
        ver = dash.get("version")
        old = seen.get(uid)
        seen[uid] = ver
        if first or old == ver:
            continue
        what = "new" if old is None else f"v{old} -> v{ver}"
        lines.append(f"dashboard {dash.get('title', uid)!r} ({what}) updated {meta.get('updated', '')} by "
                     f"{meta.get('updatedBy', '?')} {c.base}{meta.get('url', '/d/' + uid)}")
    return lines


def run_datasources(api, c, args, st, first):
    prev = st.setdefault("ok", {})
    rows = _call(api, c.get, "/api/datasources")
    if args.uid:
        rows = [r for r in rows if r.get("uid") in args.uid]
    lines = []
    for r in rows[: args.limit]:
        try:
            h = c.get(f"/api/datasources/uid/{api.q(r['uid'])}/health")
            ok = str(h.get("status", "")).upper() == "OK"
            msg = h.get("message", "")
        except api.ApiError as exc:
            ok, msg = False, str(exc)
        old = prev.get(r["uid"])
        prev[r["uid"]] = ok
        if first or old is None or old == ok:
            continue
        lines.append(f"datasource {r.get('name')} ({r.get('type')}) is {'healthy again' if ok else 'UNHEALTHY'}: {' '.join(str(msg).split())[:160]}")
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("mode", choices=MODES)
    ap.add_argument("--instance", default=os.environ.get("GRAFANA_INSTANCE"))
    ap.add_argument("--state", default="firing", choices=["firing", "pending", "normal"])
    ap.add_argument("--label", action="append", default=[], metavar="K=V")
    ap.add_argument("--rule")
    ap.add_argument("--resolved", action="store_true")
    ap.add_argument("--uid", action="append", default=[])
    ap.add_argument("--folder")
    ap.add_argument("--tag", action="append", default=[])
    ap.add_argument("--limit", type=int)
    args = ap.parse_args(argv)
    args.limit = max(1, min(args.limit or {"alerts": 200, "dashboard-changed": 30, "datasource-health": 20}[args.mode], 500))
    if any("=" not in x for x in args.label):
        print("--label needs k=v", file=sys.stderr)
        return 1
    if args.mode == "dashboard-changed" and not (args.uid or args.folder or args.tag):
        print("dashboard-changed needs --uid, --folder or --tag", file=sys.stderr)
        return 1
    try:
        api, c = _client(args.instance)
        key = {k: v for k, v in vars(args).items() if k not in ("instance", "mode", "limit")}
        path = state_path(c.inst.name, args.mode, key)
        st = load_state(path)
        first = st is None
        st = st or {}
        run = {"alerts": run_alerts, "dashboard-changed": run_dashboards, "datasource-health": run_datasources}[args.mode]
        lines = run(api, c, args, st, first)
    except Transient as exc:
        print(f"transient Grafana API error, skipping this tick: {exc}", file=sys.stderr)
        return 0
    except Exception as exc:
        print(f"grafana_watch {args.mode} failed: {str(exc)[:300]}", file=sys.stderr)
        return 1
    save_state(path, st)
    if lines:
        print("\n".join(lines[:20]) + (f"\n+{len(lines) - 20} more" if len(lines) > 20 else ""))
        return RC_CHANGED
    return 0


if __name__ == "__main__":
    sys.exit(main())
