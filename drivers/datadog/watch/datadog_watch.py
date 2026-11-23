#!/usr/bin/env python3
"""Datadog watcher (read-only). Script-watch exit codes: 0 = nothing new (silent), 200 = changed (notice printed),
1 = error (bad args / auth rejected / unknown instance).

Modes (first arg):
  monitors  state transitions of monitors selected by --tags a:b,c:d and/or --name TEXT and/or --query '<monitor search>'
            (at least one filter is required). --notify alert,warn,recovered (default) picks which transitions report:
            alert/warn = changed TO that state, recovered = back to OK from Alert/Warn/No Data. --include-new also
            reports monitors first seen already in a notifying state. --limit N bounds the monitors scanned (default 300).
  logs      new log message PATTERNS for --query '<log query>' within the last --window (default 15m): messages are
            normalized (numbers, hex, UUIDs, IPs collapsed), only patterns never seen before are reported. --limit N logs
            are scanned per run (default 300, max 1000). Needs --query.

Instance: DATADOG_INSTANCE (injected from the pinned integration). State: $WATCH_STATE_DIR/datadog-<mode>-<hash>.json
(monitor ids/states, pattern hashes; bounded). The first run records a baseline (exit 0). 5xx/429/network errors =>
silent skip; 401/403/400 => exit 1.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

CLI_DIR = Path(__file__).resolve().parents[1] / "cli"
sys.path.insert(0, str(CLI_DIR))

import ddconfig  # noqa: E402
from client import ApiError, Client, monitor_states, redact, search_logs  # noqa: E402

RC_CHANGED = 200
MAX_PATTERNS = 3000
BAD = ("HTTP 400", "HTTP 401", "HTTP 403", "HTTP 404", "needs", "invalid")


def state_file(mode: str, key: str) -> Path:
    d = Path(os.environ.get("WATCH_STATE_DIR") or Path.home() / ".dop-agent" / "watch-state" / "datadog")
    d.mkdir(parents=True, exist_ok=True)
    return d / f"datadog-{mode}-{hashlib.sha1(key.encode()).hexdigest()[:12]}.json"


def load_state(p: Path):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def save_state(p: Path, data) -> None:
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".st")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, p)


def run_monitors(c: Client, a) -> int:
    if not (a.tags or a.name or a.query):
        raise ApiError("monitors mode needs --tags, --name or --query")
    notify = {x.strip() for x in a.notify.split(",") if x.strip()}
    if not notify <= {"alert", "warn", "recovered"}:
        raise ApiError("invalid --notify: use alert,warn,recovered")
    sf = state_file("monitors", f"{c.inst.name}|{a.tags}|{a.name}|{a.query}")
    rows = monitor_states(c, a.tags or "", a.name or "", a.query or "", a.limit)
    cur = {str(r["id"]): r["state"] for r in rows}
    old = load_state(sf)
    save_state(sf, cur)
    if old is None:
        return 0
    by_id = {str(r["id"]): r for r in rows}
    events = []
    for mid, st in cur.items():
        prev = old.get(mid)
        if prev == st:
            continue
        if st == "OK":
            kind = "recovered" if prev in ("Alert", "Warn", "No Data") else None
        else:
            kind = st.lower() if st.lower() in ("alert", "warn") else None
        if kind and kind in notify and (prev is not None or a.include_new):
            events.append((kind, mid, prev or "new", st))
    if not events:
        return 0
    print(f"Datadog {c.inst.name}: {len(events)} monitor change(s)")
    for kind, mid, prev, st in events[:20]:
        r = by_id[mid]
        print(f"  {kind.upper()}: [{mid}] {r['name'][:100]}  {prev} -> {st}  https://app.{c.inst.site}/monitors/{mid}")
    if len(events) > 20:
        print(f"  ... +{len(events) - 20} more")
    return RC_CHANGED


_NORM = [(re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
         (re.compile(r"\b\d{1,3}(\.\d{1,3}){3}\b"), "<ip>"),
         (re.compile(r"\b0x[0-9a-f]+\b|\b[0-9a-f]{12,}\b", re.I), "<hex>"),
         (re.compile(r"\d+"), "<n>")]


def normalize(msg: str) -> str:
    msg = " ".join(msg.split())[:300]
    for rx, rep in _NORM:
        msg = rx.sub(rep, msg)
    return msg


def run_logs(c: Client, a) -> int:
    if not a.query:
        raise ApiError("logs mode needs --query")
    sf = state_file("logs", f"{c.inst.name}|{a.query}")
    logs = search_logs(c, a.query, a.window, a.limit)
    pats: dict[str, dict] = {}
    for e in logs:
        p = normalize(e["message"])
        pats.setdefault(hashlib.sha1(p.encode()).hexdigest()[:12], {"pattern": p, "e": e, "n": 0})["n"] += 1
    old = load_state(sf)
    known = set(old or [])
    merged = list(old or []) + [h for h in pats if h not in known]
    save_state(sf, merged[-MAX_PATTERNS:])
    if old is None:
        return 0
    new = [v for h, v in pats.items() if h not in known]
    if not new:
        return 0
    print(f"Datadog {c.inst.name}: {len(new)} new log pattern(s) for `{a.query}` in the last {a.window}")
    for v in new[:10]:
        e = v["e"]
        print(f"  x{v['n']} [{e['status']}] {e['service']}@{e['host']}: {v['pattern'][:200]}")
    if len(new) > 10:
        print(f"  ... +{len(new) - 10} more")
    return RC_CHANGED


def main(argv=None, client_factory=Client) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["monitors", "logs"])
    ap.add_argument("--instance", default=os.environ.get("DATADOG_INSTANCE"))
    ap.add_argument("--tags", help="monitors: monitor tags a:b,c:d")
    ap.add_argument("--name", help="monitors: name substring")
    ap.add_argument("--query", help="monitors: monitor search query; logs: log query")
    ap.add_argument("--notify", default="alert,warn,recovered")
    ap.add_argument("--include-new", action="store_true")
    ap.add_argument("--window", default="15m", help="logs: lookback per run")
    ap.add_argument("--limit", type=int, default=300)
    a = ap.parse_args(argv)
    a.limit = max(1, min(a.limit, 1000))
    inst = None
    try:
        inst = ddconfig.load_instance(a.instance)
        c = client_factory(inst)
        return run_monitors(c, a) if a.mode == "monitors" else run_logs(c, a)
    except ddconfig.InstanceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ApiError as exc:
        msg = redact(str(exc), getattr(inst, "api_key", ""), getattr(inst, "app_key", ""))
        if any(b in msg for b in BAD):
            print(f"error: {msg}", file=sys.stderr)
            return 1
        print(f"transient, skipped: {msg}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
