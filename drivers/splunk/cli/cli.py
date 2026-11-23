#!/usr/bin/env python3
"""Splunk CLI (read-only): SPL searches, jobs, saved searches, fired alerts, indexes, sourcetypes, test, fetch.

Run with the bot's python: `cli.py --instance NAME <command>`; run `-h` for flags. Output is a compact tab-separated
table (or JSON with --json); `--max-chars N` (default 6000, 0 = unlimited) cuts long output. The token comes from
config.yaml (never printed). The only writes are creating a search job (`search`) and cancelling one (`job cancel`).
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import splunk_api as api  # noqa: E402
import splunk_config as sconfig  # noqa: E402


def emit(obj, a, rows=None):
    if rows is not None and not a.json:
        text = (obj.get("note", "") + "\n" if isinstance(obj, dict) and obj.get("note") else "") + api.table(rows)
        head = {k: obj[k] for k in ("sid", "state", "total") if isinstance(obj, dict) and k in obj}
        text = (json.dumps(head) + "\n" if head else "") + text
    else:
        text = json.dumps(obj, ensure_ascii=False, default=str)
    text = api.redact(text)
    if a.max_chars and len(text) > a.max_chars:
        text = text[: a.max_chars] + f"\n… cut at {a.max_chars} chars (use --max-chars 0 or narrow the query)"
    print(text)


def cmd_search(c, a):
    fields = [f for f in (a.fields or "").split(",") if f] or None
    r = api.run_search(c, a.spl, a.earliest, a.latest, a.count, a.timeout)
    r["rows"] = api.compact_rows(r["rows"], fields, a.width)
    emit(r, a, r["rows"])


def cmd_job(c, a):
    if a.action == "list":
        rows = [{"sid": e.get("name"), "state": e["content"].get("dispatchState"), "results": e["content"].get("resultCount"),
                 "run_s": e["content"].get("runDuration"), "search": api.clip(e["content"].get("search") or "", 100)}
                for e in c.entries(c.path("/search/jobs"), {"count": api.lim(a.limit), "sort_key": "dispatch_time", "sort_dir": "desc"})]
        return emit(rows, a, rows)
    if not a.sid:
        raise ValueError(f"job {a.action} needs a SID")
    if a.action == "status":
        return emit(api.job_info(c, a.sid), a)
    if a.action == "cancel":
        return emit(api.cancel_job(c, a.sid), a)
    rows, _ = api.job_results(c, a.sid, a.count, a.offset)
    rows = api.compact_rows(rows, None, a.width)
    emit(rows, a, rows)


def _saved_row(e):
    ct = e.get("content") or {}
    return {"name": e.get("name"), "app": (e.get("acl") or {}).get("app"), "scheduled": ct.get("is_scheduled"),
            "disabled": ct.get("disabled"), "cron": ct.get("cron_schedule"), "alert": ct.get("alert_type"),
            "next": ct.get("next_scheduled_time"), "search": api.clip(ct.get("search") or "", 160)}


def cmd_saved(c, a):
    if a.action == "list":
        params = {"count": api.lim(a.limit)}
        if a.filter:
            params["search"] = a.filter
        rows = [_saved_row(e) for e in c.entries(c.path("/saved/searches"), params)]
        return emit(rows, a, rows)
    if not a.name:
        raise ValueError(f"saved {a.action} needs a NAME")
    path = c.path(f"/saved/searches/{api.q(a.name)}")
    if a.action == "get":
        ents = c.entries(path)
        ct = (ents[0].get("content") if ents else None) or {}
        keep = ("search", "description", "cron_schedule", "is_scheduled", "disabled", "alert_type", "alert_comparator",
                "alert_threshold", "alert.severity", "alert.track", "actions", "dispatch.earliest_time", "dispatch.latest_time",
                "next_scheduled_time", "action.email.to")
        return emit({k: ct.get(k) for k in keep if ct.get(k) not in (None, "")}, a)
    rows = [{"sid": e.get("name"), "state": (e.get("content") or {}).get("dispatchState"),
             "results": (e.get("content") or {}).get("resultCount"), "run_s": (e.get("content") or {}).get("runDuration"),
             "time": (e.get("content") or {}).get("published") or e.get("published")}
            for e in c.entries(path + "/history", {"count": api.lim(a.limit)})]
    emit(rows, a, rows)


def cmd_alerts(c, a):
    if a.name:
        rows = [{"sid": e.get("name"), "trigger_time": (e.get("content") or {}).get("trigger_time_rendered") or (e.get("content") or {}).get("trigger_time"),
                 "severity": (e.get("content") or {}).get("severity"), "results_link": (e.get("content") or {}).get("savedsearch_name")}
                for e in c.entries(c.path(f"/alerts/fired_alerts/{api.q(a.name)}"), {"count": api.lim(a.limit)})]
    else:
        rows = [{"alert": e.get("name"), "fired": (e.get("content") or {}).get("triggered_alert_count"),
                 "app": (e.get("acl") or {}).get("app")}
                for e in c.entries(c.path("/alerts/fired_alerts"), {"count": api.lim(a.limit)})]
    emit(rows, a, rows)


def cmd_indexes(c, a):
    rows = [{"index": e.get("name"), "events": (e.get("content") or {}).get("totalEventCount"),
             "size_mb": (e.get("content") or {}).get("currentDBSizeMB"),
             "earliest": (e.get("content") or {}).get("minTime"), "latest": (e.get("content") or {}).get("maxTime"),
             "disabled": (e.get("content") or {}).get("disabled")}
            for e in c.entries("/services/data/indexes", {"count": api.lim(a.limit), "datatype": "all"})
            if not a.filter or a.filter.lower() in (e.get("name") or "").lower()]
    emit(rows, a, rows)


def cmd_sourcetypes(c, a):
    index = api.check_id(a.index or c.index or "*", "index") if (a.index or c.index) else "*"
    r = api.run_search(c, f"| metadata type=sourcetypes index={index}", a.earliest, "now", api.lim(a.limit), a.timeout)
    rows = [{"sourcetype": x.get("sourcetype"), "events": x.get("totalCount"), "last_seen": x.get("recentTime")} for x in r["rows"]]
    if a.json:
        emit(rows, a)
    else:
        emit({"note": r["note"]}, a, rows)


def run_test(instance):
    steps = []

    def step(name, ok, detail=""):
        steps.append({"step": name, "ok": ok, "detail": api.redact(detail)})
        return ok

    def done(msg):
        return {"ok": all(s["ok"] for s in steps), "message": msg, "steps": steps}

    try:
        inst = sconfig.load_instance(instance)
    except sconfig.InstanceError as exc:
        step("config", False, str(exc))
        return done("instance config problem")
    step("config", True, f"{inst.name} -> {inst.base_url}")
    c = api.Client(inst)
    try:
        info = (c.entries("/services/server/info") or [{}])[0].get("content") or {}
    except api.ApiError as exc:
        hint = " (check auth.token validity/expiry and that base_url is the management port, usually :8089)" if exc.status in (0, 401, 403) else ""
        step("auth", False, f"{exc}{hint}")
        return done("cannot reach/authenticate to Splunk")
    step("auth", True, f"Splunk {info.get('version', '?')} ({info.get('product_type') or info.get('serverName', '?')})")
    try:
        ctx = (c.entries("/services/authentication/current-context") or [{}])[0].get("content") or {}
        roles = ctx.get("roles") or []
        step("whoami", True, f"user={ctx.get('username')} roles={','.join(roles) if isinstance(roles, list) else roles}")
    except api.ApiError as exc:
        step("whoami", False, str(exc))
    return done("ok" if all(s["ok"] for s in steps) else "see failed steps")


def fetch(instance, kind, ident):
    c = api.client(instance)
    if kind == "job":
        i = api.job_info(c, str(ident.get("sid", "")))
        return {"title": f"Search job {i['sid']}", "status": i["state"], "fields": {"results": i["results"], "search": i["search"]}}
    if kind == "saved_search":
        name = str(ident.get("name", ""))
        ents = c.entries(c.path(f"/saved/searches/{api.q(name)}"))
        ct = (ents[0].get("content") if ents else None) or {}
        return {"title": name, "status": "disabled" if ct.get("disabled") else "enabled",
                "fields": {"cron": ct.get("cron_schedule"), "search": api.clip(ct.get("search") or "", 300)}}
    raise ValueError(f"unknown kind {kind!r} (job|saved_search)")


def build_parser():
    p = argparse.ArgumentParser(description="Splunk (read-only)")
    p.add_argument("--instance", default=None)
    p.add_argument("--max-chars", type=int, default=6000)
    p.add_argument("--json", action="store_true", help="JSON instead of a table")
    p.add_argument("--width", type=int, default=200, help="max chars per cell")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("test")
    f = sub.add_parser("fetch")
    f.add_argument("--kind", required=True)
    f.add_argument("--ident", required=True)
    s = sub.add_parser("search", help="run SPL as an async job")
    s.add_argument("spl")
    s.add_argument("--earliest", default="-15m")
    s.add_argument("--latest", default="now")
    s.add_argument("--count", type=int, default=20)
    s.add_argument("--timeout", type=int, default=60)
    s.add_argument("--fields", help="comma-separated columns to show")
    j = sub.add_parser("job")
    j.add_argument("action", choices=["list", "status", "results", "cancel"])
    j.add_argument("sid", nargs="?")
    j.add_argument("--count", type=int, default=20)
    j.add_argument("--offset", type=int, default=0)
    j.add_argument("--limit", type=int, default=20)
    sv = sub.add_parser("saved")
    sv.add_argument("action", choices=["list", "get", "history"])
    sv.add_argument("name", nargs="?")
    sv.add_argument("--filter")
    sv.add_argument("--limit", type=int, default=30)
    al = sub.add_parser("alerts", help="fired alerts (per alert; NAME lists its fired instances)")
    al.add_argument("name", nargs="?")
    al.add_argument("--limit", type=int, default=30)
    ix = sub.add_parser("indexes")
    ix.add_argument("--filter")
    ix.add_argument("--limit", type=int, default=50)
    st = sub.add_parser("sourcetypes")
    st.add_argument("--index")
    st.add_argument("--earliest", default="-24h")
    st.add_argument("--limit", type=int, default=50)
    st.add_argument("--timeout", type=int, default=60)
    return p


CMDS = {"search": cmd_search, "job": cmd_job, "saved": cmd_saved, "alerts": cmd_alerts, "indexes": cmd_indexes,
        "sourcetypes": cmd_sourcetypes}


def main(argv=None):
    a = build_parser().parse_args(api.join_time_args(list(sys.argv[1:] if argv is None else argv)))
    try:
        if a.cmd == "test":
            print(json.dumps(run_test(a.instance)))
            return 0
        if a.cmd == "fetch":
            print(json.dumps(fetch(a.instance, a.kind, json.loads(a.ident))))
            return 0
        CMDS[a.cmd](api.client(a.instance), a)
        return 0
    except (api.ApiError, sconfig.InstanceError, ValueError, OSError) as exc:
        print(f"error: {api.redact(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
