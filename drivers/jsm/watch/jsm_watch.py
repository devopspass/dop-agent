#!/usr/bin/env python3
"""Jira Service Management watcher (read-only). Script-watch exit codes: 0 = nothing new (silent), 200 = changed
(notice printed), 1 = error (bad config / auth / scope).

Modes (first arg); every mode's first run only records a baseline (exit 0):
  alerts     [--query 'status: open'] [--limit 100] [--report-initial]
             Ops alerts matching the query: NEW / GONE (closed or no longer matching) when the open set changes.
  queue      --desk ID --queue ID [--limit 50]
             Issues that appear in a service-desk queue (new to the queue).
  requests   [--ownership ALL_REQUESTS] [--status OPEN_REQUESTS] [--desk ID] [--limit 30]
             Requests that are new to the set or changed status.
  approvals  [--limit 30]
             Requests waiting for MY approval that were not pending before.
  sla        [--minutes 30] [--desk ID] [--limit 25]
             Open requests with an SLA breached or due within N minutes (each (request, SLA, state) reported once).
  oncall     (--schedule ID | --name TEXT)
             The on-call people of a schedule changed.

Instance: --instance or the JSM_INSTANCE env (set by the driver); base_url, email and the token are resolved from
config.yaml by the CLI (never passed through argv/env, never printed). State: $WATCH_STATE_DIR/jsm-<mode>-<hash of args>.json (ids/status only,
no ticket text). 429/5xx/network errors are skipped silently (exit 0) and retried on the next run.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
from pathlib import Path

RC_CHANGED = 200
KEEP = 2000
TRANSIENT = re.compile(r"^(HTTP (429|5\d\d)|network error)")


def load_cli():
    path = Path(__file__).resolve().parents[1] / "cli" / "cli.py"
    spec = importlib.util.spec_from_file_location("jsm_cli", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def state_path(mode, ident):
    d = Path(os.environ.get("WATCH_STATE_DIR") or tempfile.gettempdir())
    d.mkdir(parents=True, exist_ok=True)
    return d / f"jsm-{mode}-{hashlib.sha1(ident.encode()).hexdigest()[:12]}.json"


def load(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def save(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def cap(d):
    return dict(list(d.items())[-KEEP:])


def link(cli, key):
    return f"{cli.CONF['base_url'].rstrip('/')}/browse/{key}"


def mode_alerts(a, cli):
    cur = {x["id"]: x for x in cli.fetch_alerts(a.query, max(1, min(a.limit, 500)))}
    path = state_path("alerts", a.query)
    prev = load(path)
    save(path, {"ids": sorted(cur)})
    if prev is None and not a.report_initial:
        return []
    old = set((prev or {}).get("ids", []))
    out = [f"NEW   {cli.alert_line(cur[i])}" for i in cur if i not in old]
    out += [f"GONE  id={i} (closed/no longer matching)" for i in sorted(old - set(cur))]
    if out:
        out = [f"JSM alerts for '{a.query}': {len(cur)} open"] + out
    return out


def mode_queue(a, cli):
    if not (a.desk and a.queue):
        sys.exit("queue mode needs --desk and --queue")
    rows = cli.queue_issues(a.desk, a.queue, max(1, min(a.limit, 100)))
    cur = {r["key"]: cli.short((r.get("fields") or {}).get("summary"), 100) for r in rows}
    path = state_path("queue", f"{a.desk}/{a.queue}")
    prev = load(path)
    save(path, {"seen": cap({**(prev or {}).get("seen", {}), **cur})})
    if prev is None:
        return []
    seen = prev.get("seen", {})
    new = [f"{k} {s} — {link(cli, k)}" for k, s in cur.items() if k not in seen]
    return [f"JSM queue {a.queue}: {len(new)} new issue(s)"] + new if new else []


def mode_requests(a, cli):
    rows = cli.list_requests(a.ownership, a.status, a.desk, None, None, max(1, min(a.limit, 100)))
    cur = {r["issueKey"]: cli.status_of(r) for r in rows}
    path = state_path("requests", f"{a.ownership}/{a.status}/{a.desk}")
    prev = load(path)
    save(path, {"seen": cap({**(prev or {}).get("seen", {}), **cur})})
    if prev is None:
        return []
    seen = prev.get("seen", {})
    out = []
    for r in rows:
        k = r["issueKey"]
        if k not in seen:
            out.append(f"[new] {cli.req_line(r)} — {link(cli, k)}")
        elif seen[k] != cur[k]:
            out.append(f"[status {seen[k]} -> {cur[k]}] {cli.req_line(r)} — {link(cli, k)}")
    return [f"JSM requests: {len(out)} changed"] + out if out else []


def mode_approvals(a, cli):
    rows = cli.list_requests("ALL_REQUESTS", "OPEN_REQUESTS", None, None, "MY_PENDING_APPROVAL", max(1, min(a.limit, 100)))
    cur = {r["issueKey"]: cli.short(cli.summary_of(r), 100) for r in rows}
    path = state_path("approvals", "me")
    prev = load(path)
    save(path, {"pending": sorted(cur)})
    if prev is None:
        return []
    new = [f"{k} {s} — {link(cli, k)}" for k, s in cur.items() if k not in prev.get("pending", [])]
    return [f"JSM: {len(new)} request(s) waiting for your approval"] + new if new else []


def mode_sla(a, cli):
    limit_ms = max(0, a.minutes) * 60000
    rows = cli.list_requests("ALL_REQUESTS", "OPEN_REQUESTS", a.desk, None, None, max(1, min(a.limit, 50)))
    path = state_path("sla", f"{a.minutes}/{a.desk}")
    prev = load(path)
    reported = set((prev or {}).get("reported", []))
    live, out = set(), []
    for r in rows:
        k = r["issueKey"]
        for s in cli.sla_rows(k):
            rem = s["remaining_ms"]
            if s["state"] == "BREACHED":
                state = "breached"
            elif s["state"] == "running" and rem is not None and rem <= limit_ms:
                state = "due"
            else:
                continue
            tag = f"{k}|{s['name']}|{state}"
            live.add(tag)
            if tag not in reported and prev is not None:
                out.append(f"{k} SLA '{s['name']}' {state.upper()} (remaining {s['remaining']}, goal {s['goal']}) — "
                           f"{cli.short(cli.summary_of(r), 80)} — {link(cli, k)}")
    save(path, {"reported": sorted(live | (reported & live))})
    return [f"JSM SLA: {len(out)} request(s) breached or due within {a.minutes} min"] + out if out else []


def mode_oncall(a, cli):
    sid, name = cli.resolve_schedule(a.schedule, a.name)
    cur = sorted(cli.on_call(sid))
    path = state_path("oncall", sid)
    prev = load(path)
    save(path, {"on_call": cur})
    if prev is None or prev.get("on_call") == cur:
        return []
    names = lambda ids: ", ".join(cli.user_name(i) for i in ids) or "nobody"
    return [f"JSM on-call changed in {name}: {names(prev.get('on_call', []))} -> {names(cur)}"]


MODES = {"alerts": mode_alerts, "queue": mode_queue, "requests": mode_requests, "approvals": mode_approvals,
         "sla": mode_sla, "oncall": mode_oncall}


def run(a, cli):
    cli.configure(a.instance)
    out = MODES[a.mode](a, cli)
    if not out:
        return 0
    print("\n".join(out))
    return RC_CHANGED


def build():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=sorted(MODES))
    p.add_argument("--instance", default=os.environ.get("JSM_INSTANCE"))
    p.add_argument("--query", default="status: open")
    p.add_argument("--report-initial", action="store_true")
    p.add_argument("--desk")
    p.add_argument("--queue")
    p.add_argument("--ownership", default="ALL_REQUESTS", choices=["ALL_REQUESTS", "OWNED_REQUESTS", "PARTICIPATED_REQUESTS", "APPROVER"])
    p.add_argument("--status", default="OPEN_REQUESTS", choices=["OPEN_REQUESTS", "CLOSED_REQUESTS", "ALL_REQUESTS"])
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--minutes", type=int, default=30)
    p.add_argument("--schedule")
    p.add_argument("--name")
    return p


def main():
    a = build().parse_args()
    if a.limit is None:
        a.limit = {"alerts": 100, "queue": 50, "requests": 30, "approvals": 30, "sla": 25}.get(a.mode, 0)
    cli = load_cli()
    try:
        sys.exit(run(a, cli))
    except SystemExit as e:
        if e.code in (0, RC_CHANGED, None):
            raise
        if isinstance(e.code, str) and TRANSIENT.match(e.code):
            sys.exit(0)
        print(e.code, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
