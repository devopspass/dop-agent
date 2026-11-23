#!/usr/bin/env python3
"""Prometheus / Alertmanager watcher (read-only). Script-watch exit codes: 0 = nothing new (silent), 200 = changed
(notice printed), 1 = error (instance/auth/API failure).

Modes (first arg):
  alerts       Alert instances newly in --state (default firing), once each. --source prometheus (default,
               /api/v1/alerts) or alertmanager (needs alertmanager_url; silenced/inhibited alerts are skipped).
               --label k=v (repeatable, all must match), --name SUBSTR (alertname), --resolved also reports
               instances that stopped matching, --limit N=200 (instances scanned).
  target-down  Scrape targets whose health turned `down`. --job J, --label k=v (repeatable), --resolved also reports
               targets that are up again, --limit N=500 (targets scanned).
  threshold    PromQL --query EXPR crossing a threshold: series where `value --op --value` newly holds
               (--op gt|ge|lt|le|eq|ne, default gt), --resolved reports series that stopped matching,
               --limit N=100 (series considered).

Instance: --instance or the PROMETHEUS_INSTANCE env (set by the driver). State:
$WATCH_STATE_DIR/prometheus-<instance>-<mode>-<hash of filters>.json (series keys only, never sample data).
The first run only records a baseline (exit 0). 429/5xx/network errors are transient (silent exit 0, warning on stderr).
"""
import argparse
import hashlib
import json
import operator
import os
import re
import sys
import tempfile
from pathlib import Path

CLI_DIR = Path(__file__).resolve().parents[1] / "cli"
RC_CHANGED = 200
MODES = ["alerts", "target-down", "threshold"]
OPS = {"gt": operator.gt, "ge": operator.ge, "lt": operator.lt, "le": operator.le, "eq": operator.eq, "ne": operator.ne}
SYMBOL = {"gt": ">", "ge": ">=", "lt": "<", "le": "<=", "eq": "==", "ne": "!="}


class Transient(Exception):
    pass


def _client(instance):
    sys.path.insert(0, str(CLI_DIR))
    import prom_api as api
    return api, api.client(instance)


def _call(api, fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except api.ApiError as exc:
        if exc.transient:
            raise Transient(str(exc)) from exc
        raise


def state_path(instance, mode, key):
    d = Path(os.environ.get("WATCH_STATE_DIR") or Path.home() / ".dop-agent" / "watch-state" / "prometheus-default")
    d.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha1(json.dumps(key, sort_keys=True).encode()).hexdigest()[:8]
    return d / f"prometheus-{re.sub(r'[^A-Za-z0-9_.-]+', '_', instance)}-{mode}-{h}.json"


def load_state(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def save_state(path, st):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".pm.", suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        json.dump(st, fh)
    os.replace(tmp, path)


def _labels(d):
    return " ".join(f"{k}={v}" for k, v in sorted(d.items()))[:160]


def _diff(cur, st, first, resolved, fmt_new, fmt_gone):
    prev = set(st.get("keys") or [])
    st["keys"] = sorted(cur)[:1000]
    if first:
        return []
    lines = [fmt_new(cur[k]) for k in sorted(cur) if k not in prev]
    if resolved:
        lines += [fmt_gone(k) for k in sorted(prev - set(cur))]
    return lines


def run_alerts(api, c, args, st, first):
    if args.source == "alertmanager":
        rows = _call(api, api.alerts_am, c, True, False, False, 1000)
        rows = [r for r in rows if r["state"] == "active"]
    else:
        rows = _call(api, api.alerts_prom, c, ("firing",), 1000)
    rows = api.label_filter(rows, args.label)
    if args.name:
        rows = [r for r in rows if args.name.lower() in r["alert"].lower()]
    cur = {r["fp"]: r for r in rows[: args.limit]}
    return _diff(cur, st, first, args.resolved,
                 lambda r: f"[firing] {r['alert']} {_labels({k: v for k, v in r['labels'].items() if k != 'alertname'})} "
                           f"since {r['since']} {r['summary']}".rstrip(),
                 lambda k: f"[resolved] {k[:160]}")


def run_targets(api, c, args, st, first):
    rows = _call(api, api.targets, c, "all", args.job or "", 2000)
    rows = api.label_filter([dict(r, labels={**r["labels"], "job": r["job"], "instance": r["instance"]}) for r in rows], args.label)[: args.limit]
    keys = {f"{r['job']}|{r['instance']}": r for r in rows}
    down = {k: r for k, r in keys.items() if r["health"] == "down"}
    seen_up = {k for k, r in keys.items() if r["health"] == "up"}
    prev = set(st.get("keys") or [])
    st["keys"] = sorted(down)[:1000]
    if first:
        return []
    lines = [f"[down] target {r['job']} {r['instance']}: {r['last_error'] or 'no error text'}"
             for k, r in sorted(down.items()) if k not in prev]
    if args.resolved:
        lines += [f"[up again] target {k.replace('|', ' ')}" for k in sorted(prev & seen_up)]
    return lines


def run_threshold(api, c, args, st, first):
    series = _call(api, api.instant_series, c, args.query)[: args.limit]
    cmp = OPS[args.op]
    cur = {k: (lab, v) for k, lab, v in series if cmp(v, args.value)}
    return _diff(cur, st, first, args.resolved,
                 lambda lv: f"[threshold] {args.query[:100]} {SYMBOL[args.op]} {args.value:g}: {_labels(lv[0]) or 'scalar'} = {lv[1]:.6g}",
                 lambda k: f"[back within threshold] {k[:160]}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=MODES)
    ap.add_argument("--instance", default=os.environ.get("PROMETHEUS_INSTANCE"))
    ap.add_argument("--source", choices=["prometheus", "alertmanager"], default="prometheus")
    ap.add_argument("--label", action="append", default=[], metavar="K=V")
    ap.add_argument("--name")
    ap.add_argument("--job")
    ap.add_argument("--resolved", action="store_true")
    ap.add_argument("--query")
    ap.add_argument("--op", choices=sorted(OPS), default="gt")
    ap.add_argument("--value", type=float)
    ap.add_argument("--limit", type=int)
    args = ap.parse_args(argv)
    args.limit = max(1, min(args.limit or {"alerts": 200, "target-down": 500, "threshold": 100}[args.mode], 1000))
    if any("=" not in x for x in args.label):
        print("--label needs k=v", file=sys.stderr)
        return 1
    if args.mode == "threshold" and (not args.query or args.value is None):
        print("threshold needs --query and --value", file=sys.stderr)
        return 1
    try:
        api, c = _client(args.instance)
        key = {k: v for k, v in vars(args).items() if k not in ("instance", "mode", "limit")}
        path = state_path(c.inst.name, args.mode, key)
        st = load_state(path)
        first = st is None
        st = st or {}
        run = {"alerts": run_alerts, "target-down": run_targets, "threshold": run_threshold}[args.mode]
        lines = run(api, c, args, st, first)
    except Transient as exc:
        print(f"transient Prometheus API error, skipping this tick: {exc}", file=sys.stderr)
        return 0
    except Exception as exc:
        print(f"prometheus_watch {args.mode} failed: {api_redact(str(exc))[:300]}", file=sys.stderr)
        return 1
    save_state(path, st)
    if lines:
        print("\n".join(lines[:20]) + (f"\n+{len(lines) - 20} more" if len(lines) > 20 else ""))
        return RC_CHANGED
    return 0


def api_redact(text):
    return re.sub(r"(?i)(token|password|secret|authorization)([=:]\s*)\S+", r"\1\2***", text)


if __name__ == "__main__":
    sys.exit(main())
