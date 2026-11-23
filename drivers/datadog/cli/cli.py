#!/usr/bin/env python3
"""Datadog CLI (compact output; keys come from the integration config and are never printed).

Run as:  <python> cli.py [--instance NAME] <command> ...
Reads (all bounded by --limit):
  monitors [--tags a:b,c:d] [--name TEXT] [--state alert|warn|no-data|ok] [--limit 50]
  monitor-search QUERY [--limit 30]           Datadog monitor search syntax, e.g. 'status:alert tag:team:x'
  monitor ID                                  one monitor: state, query, thresholds, message (truncated)
  metrics QUERY [--from 1h] [--to now] [--points 8]    timeseries, compact summary per series (min/avg/max/last + sparse points)
  logs QUERY [--from 15m] [--to now] [--limit 30] [--full]   logs search (message truncated unless --full)
  hosts [--filter TEXT] [--limit 50]          infrastructure hosts; host-tags [HOST]  (all tag keys summary, or tags of one host)
  events [--from 1h] [--tags t1,t2] [--priority normal|low] [--limit 30]
  dashboards [--query TEXT] [--limit 50]; dashboard ID  (title, template variables, widgets summary)
  downtimes [--all] [--limit 50]              active downtimes (--all = also scheduled)
  slos [--query TEXT] [--tags t] [--limit 30]; slo ID [--window 7d]   (definition + SLI history summary)
Writes (dry run unless --yes):
  mute MONITOR_ID [--duration 1h] [--message TEXT] [--yes]    creates a downtime scoped to that monitor
  unmute MONITOR_ID [--yes]                                   cancels the active downtimes of that monitor
Other: test | fetch --kind monitor|dashboard|slo --ident '{"id": ...}'
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ddconfig  # noqa: E402
from client import ApiError, Client, monitor_states, parse_time, redact, search_logs  # noqa: E402

STATE_ALIASES = {"alert": "Alert", "warn": "Warn", "no-data": "No Data", "ok": "OK"}
_EMOJI = {"OK": "🟢", "Alert": "🔴", "Warn": "🟡", "No Data": "⚪"}


def clip(s, n=160) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _int_id(v) -> int:
    try:
        n = int(str(v).strip())
    except ValueError:
        raise ApiError(f"id must be a number, got {v!r}") from None
    if n <= 0:
        raise ApiError("id must be positive")
    return n


def _slug_id(v) -> str:
    v = str(v).strip()
    if not v or not all(ch.isalnum() or ch in "-_" for ch in v):
        raise ApiError(f"bad id {v!r}")
    return v


def _lim(a, default):
    return max(1, min(int(getattr(a, "limit", default) or default), 500))


def _ts(epoch) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(float(epoch))) + "Z"
    except (TypeError, ValueError):
        return str(epoch or "")


def cmd_monitors(c, a):
    rows = monitor_states(c, a.tags or "", a.name or "", "", 1000)
    if a.state:
        rows = [r for r in rows if r["state"] == STATE_ALIASES[a.state]]
    print(f"{len(rows)} monitors" + (f" (showing {_lim(a, 50)})" if len(rows) > _lim(a, 50) else ""))
    for r in rows[: _lim(a, 50)]:
        print(f"{_EMOJI.get(r['state'], '·')} {r['id']}  {r['state']:<7} {clip(r['name'], 90)}  [{clip(','.join(r['tags'][:4]), 70)}]")


def cmd_monitor_search(c, a):
    rows = monitor_states(c, query=a.query, limit=_lim(a, 30))
    for r in rows:
        print(f"{_EMOJI.get(r['state'], '·')} {r['id']}  {r['state']:<7} {clip(r['name'], 100)}")
    print(f"{len(rows)} monitors")


def cmd_monitor(c, a):
    m = c.get(f"api/v1/monitor/{_int_id(a.id)}")
    print(f"{m.get('id')} {_EMOJI.get(m.get('overall_state'), '')} {m.get('overall_state')}  {m.get('name')}")
    print(f"type: {m.get('type')}  priority: {m.get('priority')}  muted-by-downtime: {bool((m.get('matching_downtimes') or []))}")
    print(f"query: {clip(m.get('query'), 400)}")
    print(f"thresholds: {json.dumps((m.get('options') or {}).get('thresholds') or {})}")
    print(f"tags: {','.join(m.get('tags') or [])}")
    print(f"message: {clip(m.get('message'), 400)}")


def _series_line(s, n_points) -> str:
    pts = [p[1] for p in (s.get("pointlist") or []) if p and len(p) > 1 and p[1] is not None]
    name = s.get("display_name") or s.get("metric") or "?"
    scope = s.get("scope") or ""
    if not pts:
        return f"{name}{{{scope}}}: no data"
    step = max(1, len(pts) // max(1, n_points))
    sample = pts[::step][-n_points:]
    f = lambda v: f"{v:.4g}"  # noqa: E731
    return (f"{name}{{{clip(scope, 80)}}}: n={len(pts)} min={f(min(pts))} avg={f(sum(pts) / len(pts))} "
            f"max={f(max(pts))} last={f(pts[-1])} points=[{', '.join(f(v) for v in sample)}]")


def cmd_metrics(c, a):
    d = c.get("api/v1/query", query=a.query, **{"from": parse_time(a.from_), "to": parse_time(a.to)})
    series = d.get("series") or []
    if d.get("error"):
        raise ApiError(clip(d["error"], 300))
    print(f"{len(series)} series for {clip(a.query, 120)}")
    for s in series[:20]:
        print(_series_line(s, max(2, min(a.points, 30))))
    if len(series) > 20:
        print(f"... +{len(series) - 20} more series (narrow the query / add a group-by filter)")


def cmd_logs(c, a):
    body = {"filter": {"query": a.query, "from": parse_time(a.from_) * 1000, "to": parse_time(a.to) * 1000},
            "sort": "-timestamp", "page": {"limit": _lim(a, 30)}}
    d = c.request("POST", "api/v2/logs/events/search", body=body)
    data = d.get("data") or []
    print(f"{len(data)} logs (newest first)")
    for e in data:
        at = e.get("attributes") or {}
        msg = str(at.get("message", ""))
        print(f"{str(at.get('timestamp', ''))[:19]} {at.get('status', ''):<6} {at.get('service', '')}@{at.get('host', '')}: "
              + (msg if a.full else clip(msg, 220)))


def cmd_hosts(c, a):
    d = c.get("api/v1/hosts", filter=a.filter, count=_lim(a, 50), sort_field="status", sort_dir="asc")
    rows = d.get("host_list") or []
    print(f"{d.get('total_matching', len(rows))} hosts matching ({len(rows)} shown)")
    for h in rows:
        apps = ",".join(h.get("apps") or [])
        print(f"{h.get('name')}  up={h.get('up')}  last_seen={_ts(h.get('last_reported_time'))}  apps={clip(apps, 60)}  "
              f"aws={h.get('aws_name') or '-'}  tags={clip(','.join(sum((v for v in (h.get('tags_by_source') or {}).values()), [])[:6]), 100)}")


def cmd_host_tags(c, a):
    if a.host:
        d = c.get(f"api/v1/tags/hosts/{_slug_host(a.host)}")
        print(f"{a.host}: " + ", ".join(sorted(d.get("tags") or [])))
        return
    d = c.get("api/v1/tags/hosts").get("tags") or {}
    keys: dict[str, int] = {}
    for tag, hosts in d.items():
        k = tag.split(":", 1)[0]
        keys[k] = keys.get(k, 0) + len(hosts)
    print(f"{len(d)} distinct tags on hosts; by key (tag-host pairs):")
    for k, n in sorted(keys.items(), key=lambda kv: -kv[1])[:40]:
        print(f"  {k}: {n}")


def _slug_host(h: str) -> str:
    h = str(h).strip()
    if not h or any(ch in h for ch in "/?#\\ "):
        raise ApiError(f"bad host name {h!r}")
    return h


def cmd_events(c, a):
    d = c.get("api/v1/events", **{"start": parse_time(a.from_), "end": int(time.time()), "tags": a.tags, "priority": a.priority})
    ev = d.get("events") or []
    print(f"{len(ev)} events (showing {min(len(ev), _lim(a, 30))})")
    for e in ev[: _lim(a, 30)]:
        print(f"{_ts(e.get('date_happened'))} [{e.get('alert_type', '')}] {clip(e.get('title'), 140)}  host={e.get('host') or '-'}  tags={clip(','.join(e.get('tags') or []), 80)}")


def cmd_dashboards(c, a):
    rows = (c.get("api/v1/dashboard").get("dashboards") or [])
    if a.query:
        rows = [r for r in rows if a.query.lower() in str(r.get("title", "")).lower()]
    print(f"{len(rows)} dashboards" + (f" (showing {_lim(a, 50)})" if len(rows) > _lim(a, 50) else ""))
    for r in rows[: _lim(a, 50)]:
        print(f"{r.get('id')}  {clip(r.get('title'), 90)}  by={r.get('author_handle', '')}  modified={str(r.get('modified_at', ''))[:10]}")


def cmd_dashboard(c, a):
    d = c.get(f"api/v1/dashboard/{_slug_id(a.id)}")
    widgets = d.get("widgets") or []
    print(f"{d.get('id')}  {d.get('title')}  layout={d.get('layout_type')}  by={d.get('author_handle', '')}  modified={str(d.get('modified_at', ''))[:10]}")
    print("variables: " + ", ".join(f"${v.get('name')}={v.get('default', '*')}" for v in d.get("template_variables") or []))
    print(f"{len(widgets)} widgets:")
    for w in widgets[:40]:
        df = w.get("definition") or {}
        reqs = df.get("requests") or []
        q = ""
        if isinstance(reqs, list) and reqs and isinstance(reqs[0], dict):
            q = reqs[0].get("q") or ((reqs[0].get("queries") or [{}])[0].get("query", ""))
        print(f"  - {df.get('type')}: {clip(df.get('title'), 60)}  {clip(q, 110)}")


def cmd_downtimes(c, a):
    d = c.get("api/v2/downtime", current_only="false" if a.all else "true")
    rows = d.get("data") or []
    print(f"{len(rows)} downtimes")
    for r in rows[: _lim(a, 50)]:
        at = r.get("attributes") or {}
        mid = (at.get("monitor_identifier") or {})
        print(f"{r.get('id')}  {at.get('status')}  scope={clip(at.get('scope'), 60)}  monitor={mid.get('monitor_id') or mid.get('monitor_tags') or '-'}  "
              f"until={(at.get('schedule') or {}).get('current_downtime', {}).get('end') or (at.get('schedule') or {}).get('end') or 'open'}  msg={clip(at.get('message'), 60)}")


def cmd_slos(c, a):
    d = c.get("api/v1/slo", query=a.query, tags_query=a.tags, limit=_lim(a, 30))
    rows = d.get("data") or []
    print(f"{len(rows)} SLOs")
    for s in rows:
        th = (s.get("thresholds") or [{}])[0]
        print(f"{s.get('id')}  {s.get('type')}  {clip(s.get('name'), 80)}  target={th.get('target')}% ({th.get('timeframe')})  tags={clip(','.join(s.get('tags') or []), 60)}")


def cmd_slo(c, a):
    sid = _slug_id(a.id)
    s = (c.get(f"api/v1/slo/{sid}").get("data") or {})
    th = (s.get("thresholds") or [{}])[0]
    print(f"{s.get('id')}  {s.get('name')}  type={s.get('type')}  target={th.get('target')}% ({th.get('timeframe')})")
    to = int(time.time())
    h = (c.get(f"api/v1/slo/{sid}/history", from_ts=parse_time(a.window), to_ts=to, target=th.get("target")).get("data") or {})
    ov = h.get("overall") or {}
    print(f"history {a.window}: sli={ov.get('sli_value')}  error_budget_remaining={(ov.get('error_budget_remaining') or {}).get(str(th.get('timeframe')))}")


def _monitor_downtimes(c, mid: int) -> list[dict]:
    rows = c.get("api/v2/downtime", current_only="true", **{"page[limit]": 100}).get("data") or []
    return [r for r in rows if str(((r.get("attributes") or {}).get("monitor_identifier") or {}).get("monitor_id")) == str(mid)]


def cmd_mute(c, a):
    mid = _int_id(a.monitor_id)
    m = c.get(f"api/v1/monitor/{mid}")
    now = time.time()
    if not 0 < now - parse_time(a.duration, now) <= 30 * 86400:
        raise ApiError("--duration must be between 1s and 30d, e.g. 1h")
    end = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + now - parse_time(a.duration, now)))
    print(f"PLAN mute monitor {mid} '{clip(m.get('name'), 80)}' (state {m.get('overall_state')}) for {a.duration} until {end}")
    if not a.yes:
        print("dry run: nothing changed; confirm with the user, then re-run with --yes")
        return
    body = {"data": {"type": "downtime", "attributes": {
        "scope": "*", "monitor_identifier": {"monitor_id": mid}, "message": clip(a.message or "muted via dop-agent", 300),
        "schedule": {"start": None, "end": end}}}}
    d = c.request("POST", "api/v2/downtime", body=body)
    print(f"muted: downtime {((d.get('data') or {}).get('id'))} until {end}")


def cmd_unmute(c, a):
    mid = _int_id(a.monitor_id)
    dts = _monitor_downtimes(c, mid)
    print(f"PLAN cancel {len(dts)} active downtime(s) of monitor {mid}: {', '.join(str(d.get('id')) for d in dts) or 'none'}")
    if not dts:
        return
    if not a.yes:
        print("dry run: nothing changed; confirm with the user, then re-run with --yes")
        return
    for d in dts:
        c.request("DELETE", f"api/v2/downtime/{_slug_id(d.get('id'))}")
    print("unmuted")


def fetch_card(c, kind: str, ident: dict) -> dict:
    if kind == "monitor":
        m = c.get(f"api/v1/monitor/{_int_id(ident.get('id'))}")
        st = m.get("overall_state", "")
        return {"title": m.get("name", ""), "status": st, "status_emoji": _EMOJI.get(st, ""),
                "url": f"https://app.{c.inst.site}/monitors/{m.get('id')}",
                "fields": {"type": m.get("type", ""), "tags": ",".join(m.get("tags") or [])}}
    if kind == "dashboard":
        d = c.get(f"api/v1/dashboard/{_slug_id(ident.get('id'))}")
        return {"title": d.get("title", ""), "url": f"https://app.{c.inst.site}/dashboard/{d.get('id')}",
                "fields": {"author": d.get("author_handle", ""), "modified": str(d.get("modified_at", ""))[:19]}}
    if kind == "slo":
        s = (c.get(f"api/v1/slo/{_slug_id(ident.get('id'))}").get("data") or {})
        return {"title": s.get("name", ""), "url": f"https://app.{c.inst.site}/slo?slo_id={s.get('id')}",
                "fields": {"type": s.get("type", ""), "tags": ",".join(s.get("tags") or [])}}
    raise ApiError(f"unknown kind {kind!r}")


def run_test(name: str | None, client_factory=Client) -> dict:
    steps = []

    def step(n, ok, detail=""):
        steps.append({"step": n, "ok": ok, "detail": detail})
        return ok

    def done(msg):
        return {"ok": all(s["ok"] for s in steps), "message": msg, "steps": steps}

    try:
        inst = ddconfig.load_instance(name)
        step("config", True, f"instance {inst.name} -> {inst.api}")
    except ddconfig.InstanceError as exc:
        step("config", False, str(exc))
        return done(str(exc))
    c = client_factory(inst)
    try:
        c.get("api/v1/validate")
        step("api_key", True, "API key valid")
    except ApiError as exc:
        step("api_key", False, redact(str(exc), inst.api_key, inst.app_key)[:200])
        return done("API key rejected or site wrong: check auth.api_key and options.site (datadoghq.eu vs datadoghq.com)")
    try:
        mons = c.get("api/v1/monitor", page_size=1)
        step("app_key", True, "application key accepted (monitors readable)")
    except ApiError as exc:
        step("app_key", False, redact(str(exc), inst.api_key, inst.app_key)[:200])
        return done("application key rejected: check auth.app_key and its scopes (monitors_read, ...)")
    try:
        c.get("api/v1/dashboard")
        step("dashboards", True, "dashboards readable")
    except ApiError as exc:
        step("dashboards", False, redact(str(exc), inst.api_key, inst.app_key)[:160])
    return done(f"Datadog {inst.site} reachable; keys valid" + ("" if all(s["ok"] for s in steps) else " (some scopes missing)"))


def build():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--instance", help="datadog integration instance (env DATADOG_INSTANCE)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(n):
        return sub.add_parser(n)

    def lim(s, d):
        s.add_argument("--limit", type=int, default=d)
        return s

    add("test")
    s = add("fetch"); s.add_argument("--kind", required=True); s.add_argument("--ident", required=True)
    s = lim(add("monitors"), 50); s.add_argument("--tags"); s.add_argument("--name"); s.add_argument("--state", choices=sorted(STATE_ALIASES))
    s = lim(add("monitor-search"), 30); s.add_argument("query")
    s = add("monitor"); s.add_argument("id")
    s = add("metrics"); s.add_argument("query"); s.add_argument("--from", dest="from_", default="1h")
    s.add_argument("--to", default="now"); s.add_argument("--points", type=int, default=8)
    s = lim(add("logs"), 30); s.add_argument("query"); s.add_argument("--from", dest="from_", default="15m")
    s.add_argument("--to", default="now"); s.add_argument("--full", action="store_true")
    s = lim(add("hosts"), 50); s.add_argument("--filter")
    s = add("host-tags"); s.add_argument("host", nargs="?")
    s = lim(add("events"), 30); s.add_argument("--from", dest="from_", default="1h"); s.add_argument("--tags"); s.add_argument("--priority", choices=["normal", "low"])
    s = lim(add("dashboards"), 50); s.add_argument("--query")
    s = add("dashboard"); s.add_argument("id")
    s = lim(add("downtimes"), 50); s.add_argument("--all", action="store_true")
    s = lim(add("slos"), 30); s.add_argument("--query"); s.add_argument("--tags")
    s = add("slo"); s.add_argument("id"); s.add_argument("--window", default="7d")
    s = add("mute"); s.add_argument("monitor_id"); s.add_argument("--duration", default="1h")
    s.add_argument("--message"); s.add_argument("--yes", action="store_true")
    s = add("unmute"); s.add_argument("monitor_id"); s.add_argument("--yes", action="store_true")
    return p


COMMANDS = {"monitors": cmd_monitors, "monitor-search": cmd_monitor_search, "monitor": cmd_monitor, "metrics": cmd_metrics,
            "logs": cmd_logs, "hosts": cmd_hosts, "host-tags": cmd_host_tags, "events": cmd_events,
            "dashboards": cmd_dashboards, "dashboard": cmd_dashboard, "downtimes": cmd_downtimes, "slos": cmd_slos,
            "slo": cmd_slo, "mute": cmd_mute, "unmute": cmd_unmute}


def main(argv=None) -> int:
    a = build().parse_args(argv)
    inst = None
    try:
        if a.cmd == "test":
            print(json.dumps(run_test(a.instance)))
            return 0
        inst = ddconfig.load_instance(a.instance)
        c = Client(inst)
        if a.cmd == "fetch":
            print(json.dumps(fetch_card(c, a.kind, json.loads(a.ident))))
            return 0
        COMMANDS[a.cmd](c, a)
        return 0
    except (ApiError, ddconfig.InstanceError, ValueError) as exc:
        print("ERROR: " + redact(str(exc), getattr(inst, "api_key", ""), getattr(inst, "app_key", ""))[:400], file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
