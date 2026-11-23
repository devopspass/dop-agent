#!/usr/bin/env python3
"""Prometheus / Alertmanager CLI for one configured integration instance.

Global: --instance NAME (prometheus integration in config.yaml; env PROMETHEUS_INSTANCE; optional when exactly one
is enabled). Output is compact JSON; --max-chars N cuts long output. Range queries are bounded (<=7d, <=500 points).
Creating/expiring silences does nothing without --yes (dry run). See skill for usage.
"""
import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import prom_api as api  # noqa: E402
import prom_config as pconfig  # noqa: E402
import prom_furl as furl  # noqa: E402

DEFAULT_MAX = 6000
MATCHER_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(=~|!~|!=|=)(.+)$", re.S)
ID_RE = re.compile(r"^[A-Za-z0-9\-]{8,64}$")


def _lim(n, hi=500):
    return max(1, min(int(n), hi))


def cmd_query(c, a):
    return api.summarize_result(c.prom("/api/v1/query", {"query": a.expr, **({"time": a.time} if a.time else {})}), _lim(a.series, 100))


def cmd_range(c, a):
    secs, step = api.plan_range(api.parse_duration(a.range), a.step)
    end = api.now()
    data = c.prom("/api/v1/query_range", {"query": a.expr, "start": end - secs, "end": end, "step": step})
    out = api.summarize_result(data, _lim(a.series, 100))
    out.update(range_s=secs, step_s=step)
    return out


def cmd_targets(c, a):
    rows = api.targets(c, a.health, a.job or "", _lim(a.limit))
    return {"count": len(rows), "targets": rows}


def _rule_row(g, r):
    row = {"group": g.get("name"), "type": r.get("type"), "name": r.get("name"), "health": r.get("health"),
           "query": " ".join(str(r.get("query", "")).split())[:240]}
    if r.get("type") == "alerting":
        row.update(state=r.get("state"), duration=r.get("duration"), labels=r.get("labels") or {},
                   active=len(r.get("alerts") or []))
    if r.get("lastError"):
        row["last_error"] = str(r["lastError"])[:200]
    return row


def cmd_rules(c, a):
    params = {"type": a.type} if a.type else {}
    out = []
    for g in c.prom("/api/v1/rules", params).get("groups") or []:
        for r in g.get("rules") or []:
            if a.state and r.get("state") != a.state:
                continue
            if a.name and a.name.lower() not in str(r.get("name", "")).lower():
                continue
            out.append(_rule_row(g, r))
    return {"count": len(out), "rules": out[:_lim(a.limit)]}


def _pairs(items):
    for kv in items or []:
        if "=" not in kv:
            raise ValueError(f"--label needs k=v, got {kv!r}")
    return items or []


def cmd_alerts(c, a):
    rows = api.label_filter(api.alerts_prom(c, [a.state], 1000), _pairs(a.label))
    return {"count": len(rows), "alerts": [{k: r[k] for k in ("alert", "state", "labels", "since", "value", "summary")}
                                          for r in rows[:_lim(a.limit)]]}


def cmd_labels(c, a):
    params = {"match[]": a.match} if a.match else {}
    names = c.prom("/api/v1/labels", params)
    return {"count": len(names), "labels": names[:_lim(a.limit, 1000)]}


def cmd_label_values(c, a):
    params = {"match[]": a.match} if a.match else {}
    vals = c.prom(f"/api/v1/label/{api.quote(a.name)}/values", params)
    if a.filter:
        vals = [v for v in vals if a.filter.lower() in v.lower()]
    return {"count": len(vals), "values": vals[:_lim(a.limit, 1000)]}


def cmd_metrics(c, a):
    a.name = "__name__"
    return cmd_label_values(c, a)


def cmd_series(c, a):
    rows = c.prom("/api/v1/series", {"match[]": a.match})
    return {"count": len(rows), "series": rows[:_lim(a.limit)]}


def cmd_metadata(c, a):
    params = {"limit": _lim(a.limit, 200)}
    if a.metric:
        params["metric"] = a.metric
    return c.prom("/api/v1/metadata", params)


def cmd_am_alerts(c, a):
    rows = api.label_filter(api.alerts_am(c, True, a.silenced, a.inhibited, 1000), _pairs(a.label))
    return {"count": len(rows), "alerts": [{k: r[k] for k in ("alert", "state", "labels", "since", "silenced_by", "summary")}
                                          for r in rows[:_lim(a.limit)]]}


def _silence_row(s):
    return {"id": s.get("id"), "state": (s.get("status") or {}).get("state"),
            "matchers": [f"{m['name']}{'=~' if m.get('isRegex') and m.get('isEqual', True) else '!~' if m.get('isRegex') else '=' if m.get('isEqual', True) else '!='}{m['value']}"
                         for m in s.get("matchers") or []],
            "starts": s.get("startsAt"), "ends": s.get("endsAt"), "by": s.get("createdBy"),
            "comment": " ".join(str(s.get("comment", "")).split())[:200]}


def cmd_silences(c, a):
    rows = [_silence_row(s) for s in c.am("GET", "/api/v2/silences") or []]
    if a.state != "all":
        rows = [r for r in rows if r["state"] == a.state]
    return {"count": len(rows), "silences": rows[:_lim(a.limit)]}


def parse_matcher(text):
    m = MATCHER_RE.match(text)
    if not m:
        raise ValueError(f"bad matcher {text!r} (use name=value, name!=value, name=~regex, name!~regex)")
    name, op, value = m.groups()
    return {"name": name, "value": value, "isRegex": op in ("=~", "!~"), "isEqual": op in ("=", "=~")}


def cmd_silence_create(c, a):
    matchers = [parse_matcher(m) for m in a.matcher]
    if not matchers:
        raise ValueError("at least one --matcher is required")
    if all(m["isRegex"] and m["value"] in (".*", ".+") for m in matchers):
        raise ValueError("refusing a silence that matches every alert; narrow the matchers")
    secs = api.parse_duration(a.duration)
    if secs > 30 * 86400:
        raise ValueError("silence duration is limited to 30d")
    start = datetime.now(timezone.utc)
    body = {"matchers": matchers, "startsAt": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endsAt": (start + timedelta(seconds=secs)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "createdBy": a.created_by, "comment": a.comment}
    if not a.yes:
        return {"dry_run": True, "would": "create silence", "instance": c.inst.name, "silence": body,
                "hint": "re-run with --yes after the user confirmed matchers, duration and comment"}
    return {"created": True, **c.am("POST", "/api/v2/silences", None, body)}


def cmd_silence_expire(c, a):
    if not ID_RE.match(a.id):
        raise ValueError(f"bad silence id {a.id!r}")
    cur = c.am("GET", f"/api/v2/silence/{api.quote(a.id)}")
    if not a.yes:
        return {"dry_run": True, "would": "expire silence", "instance": c.inst.name, "silence": _silence_row(cur),
                "hint": "re-run with --yes after the user confirmed this is the silence to expire"}
    c.am("DELETE", f"/api/v2/silence/{api.quote(a.id)}")
    return {"expired": True, "id": a.id}


def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--instance", help="prometheus integration instance name (config.yaml)")
    p.add_argument("--max-chars", type=int, default=DEFAULT_MAX, help="cut output after N chars (0 = no limit)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, help_):
        s = sub.add_parser(name, help=help_)
        s.add_argument("--max-chars", type=int, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        return s

    add("test", "connection check (JSON; exit 0 even when a step fails)")
    s = add("fetch", "link card (used by dop-agent; this driver has no link kinds)")
    s.add_argument("--kind", required=True)
    s.add_argument("--ident", required=True)
    s = add("query", "instant PromQL query, compact per-series summary")
    s.add_argument("expr")
    s.add_argument("--time", help="evaluation time (RFC3339 or unix seconds); default now")
    s.add_argument("--series", type=int, default=20, help="max series shown")
    s = add("range", "range PromQL query summarized (last/min/max/avg per series); <=7d, <=500 points")
    s.add_argument("expr")
    s.add_argument("--range", default="1h", help="window ending now: 30m, 6h, 2d (max 7d)")
    s.add_argument("--step", default="auto", help="e.g. 60s; auto = smallest allowed")
    s.add_argument("--series", type=int, default=20)
    s = add("targets", "scrape targets and their health")
    s.add_argument("--health", choices=["up", "down", "unknown", "all"], default="all")
    s.add_argument("--job")
    s.add_argument("--limit", type=int, default=100)
    s = add("rules", "alerting/recording rules")
    s.add_argument("--type", choices=["alert", "record"])
    s.add_argument("--state", choices=["firing", "pending", "inactive"])
    s.add_argument("--name", help="substring of the rule name")
    s.add_argument("--limit", type=int, default=50)
    s = add("alerts", "active alerts known to Prometheus")
    s.add_argument("--state", choices=["firing", "pending", "all"], default="firing")
    s.add_argument("--label", action="append", metavar="K=V")
    s.add_argument("--limit", type=int, default=50)
    s = add("labels", "label names")
    s.add_argument("--match", help="series selector to restrict, e.g. up{job=\"x\"}")
    s.add_argument("--limit", type=int, default=200)
    s = add("label-values", "values of one label")
    s.add_argument("name")
    s.add_argument("--match")
    s.add_argument("--filter", help="substring filter")
    s.add_argument("--limit", type=int, default=100)
    s = add("metrics", "metric names")
    s.add_argument("--match")
    s.add_argument("--filter", help="substring filter")
    s.add_argument("--limit", type=int, default=100)
    s = add("series", "series matching a selector (bounded)")
    s.add_argument("match")
    s.add_argument("--limit", type=int, default=50)
    s = add("metadata", "metric metadata (type/help)")
    s.add_argument("--metric")
    s.add_argument("--limit", type=int, default=50)
    s = add("am-alerts", "Alertmanager alerts (needs alertmanager_url)")
    s.add_argument("--silenced", action="store_true")
    s.add_argument("--inhibited", action="store_true")
    s.add_argument("--label", action="append", metavar="K=V")
    s.add_argument("--limit", type=int, default=50)
    s = add("silences", "Alertmanager silences")
    s.add_argument("--state", choices=["active", "pending", "expired", "all"], default="active")
    s.add_argument("--limit", type=int, default=50)
    s = add("silence-create", "create a silence (dry run without --yes)")
    s.add_argument("--matcher", action="append", default=[], metavar="NAME(=|!=|=~|!~)VALUE")
    s.add_argument("--duration", required=True, help="e.g. 2h, 1d (max 30d)")
    s.add_argument("--comment", required=True)
    s.add_argument("--created-by", default="dop-agent")
    s.add_argument("--yes", action="store_true", help="really create (otherwise a dry run is printed)")
    s = add("silence-expire", "expire a silence by id (dry run without --yes)")
    s.add_argument("id")
    s.add_argument("--yes", action="store_true")
    return p


CMDS = {"query": cmd_query, "range": cmd_range, "targets": cmd_targets, "rules": cmd_rules, "alerts": cmd_alerts,
        "labels": cmd_labels, "label-values": cmd_label_values, "metrics": cmd_metrics, "series": cmd_series,
        "metadata": cmd_metadata, "am-alerts": cmd_am_alerts, "silences": cmd_silences,
        "silence-create": cmd_silence_create, "silence-expire": cmd_silence_expire}


def main(argv=None):
    a = build_parser().parse_args(argv)
    try:
        if a.cmd == "test":
            print(json.dumps(furl.run_test(a.instance)))
            return 0
        if a.cmd == "fetch":
            print(json.dumps(furl.fetch(a.instance, a.kind, json.loads(a.ident))))
            return 0
        print(api.dumps(CMDS[a.cmd](api.client(a.instance), a), a.max_chars))
        return 0
    except (api.ApiError, pconfig.InstanceError, ValueError, OSError, RuntimeError) as exc:
        print(f"error: {api.redact(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
