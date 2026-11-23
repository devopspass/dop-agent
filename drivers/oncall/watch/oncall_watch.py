#!/usr/bin/env python3
"""PagerDuty / Opsgenie watcher (read-only). Script-watch exit codes: 0 = nothing new (silent), 200 = changed
(notice printed), 1 = error (bad config / auth / scope).

Modes (first arg); every mode's first run only records a baseline (exit 0):
  incidents  [--service ID] [--team X] [--query Q] [--limit 50] [--report-initial] [--report-resolved]
             NEW open (triggered/acknowledged) incidents / alerts matching the filter. PagerDuty: --service/--team are ids;
             Opsgenie: --team is a team name, --query is Opsgenie search syntax (--service unsupported).
             --report-resolved also reports items that left the open set.
  oncall     [--schedule ID | --name TEXT]    (no flag = up to 20 schedules)
             The on-call people of a schedule changed.

Instance: --instance or the ONCALL_INSTANCE env (set by the driver); base_url, backend and the token are resolved from
config.yaml by the CLI (never passed through argv/env, never printed). State: $WATCH_STATE_DIR/oncall-<mode>-<hash of args>.json
(ids/names only, no incident text). 429/5xx/network errors are skipped silently (exit 0) and retried on the next run.
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
    spec = importlib.util.spec_from_file_location("oncall_cli", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def state_path(mode, ident):
    d = Path(os.environ.get("WATCH_STATE_DIR") or tempfile.gettempdir())
    d.mkdir(parents=True, exist_ok=True)
    return d / f"oncall-{mode}-{hashlib.sha1(ident.encode()).hexdigest()[:12]}.json"


def load(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def save(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def mode_incidents(a, cli):
    rows = cli.fetch_incidents("open", a.service, a.team, a.query, 0, max(1, min(a.limit, 100)))
    cur = {r["id"]: r for r in rows}
    path = state_path("incidents", f"{a.service}/{a.team}/{a.query}")
    prev = load(path)
    # remember what was already reported so an item that stays open is not reported twice
    seen = list(dict.fromkeys((prev or {}).get("seen", []) + list(cur)))[-KEEP:]
    save(path, {"open": sorted(cur), "seen": seen})
    if prev is None and not a.report_initial:
        return []
    old, was_seen = set((prev or {}).get("open", [])), set((prev or {}).get("seen", []))
    out = [f"NEW   {cli.line(cur[i])}" + (f" — {cur[i]['url']}" if cur[i]["url"] else "") for i in cur if i not in was_seen and i not in old]
    if a.report_resolved:
        out += [f"GONE  id={i} (resolved/closed or no longer matching)" for i in sorted(old - set(cur))]
    if out:
        out = [f"{cli.CONF['backend']}: {len(cur)} open"] + out
    return out


def mode_oncall(a, cli):
    if a.schedule or a.name:
        targets = [cli.resolve_schedule(a.schedule, a.name)]
    else:
        targets = [(s["id"], s["name"]) for s in cli.list_schedules(None, 20)]
    cur = {sid: {"name": n, "who": sorted(r["who"] for r in cli.on_call(sid, schedule_name=n))} for sid, n in targets}
    path = state_path("oncall", f"{a.schedule}/{a.name}")
    prev = load(path)
    save(path, cur)
    if prev is None:
        return []
    out = []
    for sid, c in cur.items():
        if sid in prev and prev[sid]["who"] != c["who"]:
            out.append(f"on-call changed in {c['name']}: {', '.join(prev[sid]['who']) or 'nobody'} -> {', '.join(c['who']) or 'nobody'}")
    return out


MODES = {"incidents": mode_incidents, "oncall": mode_oncall}


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
    p.add_argument("--instance", default=os.environ.get("ONCALL_INSTANCE"))
    p.add_argument("--service")
    p.add_argument("--team")
    p.add_argument("--query")
    p.add_argument("--schedule")
    p.add_argument("--name")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--report-initial", action="store_true")
    p.add_argument("--report-resolved", action="store_true")
    return p


def main(argv=None):
    a = build().parse_args(argv)
    try:
        return run(a, load_cli())
    except SystemExit as e:
        msg = str(e.code) if e.code not in (None, 0) else ""
        if TRANSIENT.match(msg):
            return 0
        print(msg or "error", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
