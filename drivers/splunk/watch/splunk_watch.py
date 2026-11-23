#!/usr/bin/env python3
"""Splunk watcher (read-only). Script-watch exit codes: 0 = nothing new (silent), 200 = changed (notice printed),
1 = error (instance/auth/API failure).

Modes (first arg):
  fired    saved-search alerts that fired since the last run (Splunk fired-alerts list). --alert SUBSTR (repeatable,
           name filter, default all), --limit N=50 fired instances scanned per alert, --max-alerts N=100.
  results  rows of a bounded SPL query not seen before. --spl QUERY (required, read-only SPL only), --earliest
           (default -15m), --latest (default now), --count N=20 rows per run, --timeout S=60, --key FIELD
           (repeatable; fields identifying a row, default: the whole row). Each new row is reported once.

Instance: --instance or the SPLUNK_INSTANCE env (set by the driver). State: $WATCH_STATE_DIR/splunk-<instance>-<mode>-<hash>.json
(ids / row hashes only, never row content). The first run only records a baseline (exit 0). 429/5xx/network errors
are transient (silent exit 0, warning on stderr).
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
MAX_SEEN = 2000


class Transient(Exception):
    pass


def _api():
    sys.path.insert(0, str(CLI_DIR))
    import splunk_api as api
    return api


def state_path(instance, mode, key):
    d = Path(os.environ.get("WATCH_STATE_DIR") or Path.home() / ".dop-agent" / "watch-state" / "splunk-default")
    d.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha1(json.dumps(key, sort_keys=True).encode()).hexdigest()[:8]
    return d / f"splunk-{re.sub(r'[^A-Za-z0-9_.-]+', '_', instance)}-{mode}-{h}.json"


def load_state(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def save_state(path, st):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".sp.", suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        json.dump(st, fh)
    os.replace(tmp, path)


def run_fired(api, c, args, st, first):
    seen = st.setdefault("sids", {})
    lines = []
    alerts = c.entries(c.path("/alerts/fired_alerts"), {"count": api.lim(args.max_alerts)})
    for e in alerts:
        name = e.get("name") or ""
        if args.alert and not any(s.lower() in name.lower() for s in args.alert):
            continue
        if not (e.get("content") or {}).get("triggered_alert_count"):
            continue
        try:
            inst = c.entries(c.path(f"/alerts/fired_alerts/{api.q(name)}"), {"count": api.lim(args.limit)})
        except api.ApiError as exc:
            if exc.status == 404:
                continue
            raise
        known = set(seen.get(name) or [])
        cur = [i.get("name") for i in inst if i.get("name")]
        if not first:
            for i in inst:
                if i.get("name") and i["name"] not in known:
                    ct = i.get("content") or {}
                    lines.append(f"[fired] {name} at {ct.get('trigger_time_rendered') or ct.get('trigger_time') or '?'} "
                                 f"(severity {ct.get('severity', '?')}, sid {i['name']})")
        seen[name] = sorted(known | set(cur))[-MAX_SEEN:]
    return lines


def _row_id(row, keys):
    basis = {k: row.get(k) for k in keys} if keys else {k: v for k, v in row.items() if k not in ("_serial", "_si", "_sourcetype")}
    return hashlib.sha1(json.dumps(basis, sort_keys=True, default=str).encode()).hexdigest()[:16]


def run_results(api, c, args, st, first):
    r = api.run_search(c, args.spl, args.earliest, args.latest, args.count, args.timeout)
    if not r["done"]:
        raise Transient(r["note"])
    seen = st.setdefault("rows", [])
    seen_set = set(seen)
    new = []
    for row in r["rows"]:
        rid = _row_id(row, args.key)
        if rid not in seen_set:
            seen_set.add(rid)
            seen.append(rid)
            new.append(row)
    st["rows"] = seen[-MAX_SEEN:]
    if first:
        return []
    out = []
    for row in api.compact_rows(new, None, 200):
        out.append("[new] " + " | ".join(f"{k}={v}" for k, v in row.items())[:600])
    if out and r.get("total", 0) > len(r["rows"]):
        out.append(f"(+ more results than --count {args.count}; refine the query)")
    return out


def main(argv=None):
    api = _api()
    p = argparse.ArgumentParser(description="Splunk watcher")
    p.add_argument("mode", choices=["fired", "results"])
    p.add_argument("--instance", default=os.environ.get("SPLUNK_INSTANCE"))
    p.add_argument("--alert", action="append", default=[])
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--max-alerts", type=int, default=100)
    p.add_argument("--spl")
    p.add_argument("--earliest", default="-15m")
    p.add_argument("--latest", default="now")
    p.add_argument("--count", type=int, default=20)
    p.add_argument("--timeout", type=int, default=60)
    p.add_argument("--key", action="append", default=[])
    args = p.parse_args(api.join_time_args(list(sys.argv[1:] if argv is None else argv)))
    try:
        if args.mode == "results" and not args.spl:
            raise ValueError("results mode needs --spl")
        c = api.client(args.instance)
        key = {"alert": sorted(args.alert)} if args.mode == "fired" else {
            "spl": args.spl, "e": args.earliest, "l": args.latest, "k": sorted(args.key)}
        path = state_path(c.inst.name, args.mode, key)
        st = load_state(path)
        first = st is None
        st = st or {}
        try:
            lines = (run_fired if args.mode == "fired" else run_results)(api, c, args, st, first)
        except api.ApiError as exc:
            if exc.transient:
                raise Transient(str(exc)) from exc
            raise
        save_state(path, st)
    except Transient as exc:
        print(f"warning: transient error, skipping this run: {api.redact(exc)}", file=sys.stderr)
        return 0
    except Exception as exc:
        print(f"error: {api.redact(exc)}", file=sys.stderr)
        return 1
    if first:
        print("baseline recorded", file=sys.stderr)
        return 0
    if not lines:
        return 0
    print(f"Splunk {c.inst.name}: {len(lines)} new")
    print("\n".join(lines[:50]))
    return RC_CHANGED


if __name__ == "__main__":
    sys.exit(main())
