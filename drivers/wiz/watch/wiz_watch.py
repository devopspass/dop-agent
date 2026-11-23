#!/usr/bin/env python3
"""Wiz watcher (read-only). Script-watch exit codes: 0 = nothing new (silent), 200 = changed (notice printed),
1 = error (auth/config/bad args; the watch driver wakes after `retries` and stops).

Modes (first arg):
  findings     new OPEN vulnerability findings since the previous run: --env V[,V] (value of the env tag), --tag k=v
               (repeatable), --severity CRITICAL,HIGH (default), --host SUBSTR, --limit N=10, --max-pages N=6
  counts       open-finding counts per severity changed vs the previous run (cheap first:1 queries):
               --env, --tag, --severity (default all), --only-increase
  rescan-done  scan activities of --asset-id UUID (repeatable) all finished; fires once per asset, then forgets it.
               --scan-id ID limits it to one scan group (needs a logged-in app.wiz.io tab, --tab-id optional)

Instance: injected as env WIZ_DIR / WIZ_DATA_CENTER / WIZ_SCOPE_TAGS / WIZ_ENV_TAG_KEY / WIZ_CDP_URL by the driver
(`watch_script(..., integration=<instance>)`).
State: $WATCH_STATE_DIR/wiz-<mode>.json (ids and counts only). The first run only records a baseline (exit 0); the id
list is capped, so nothing is reported twice. Wake text: severity, host, package, CVE, fixed version, link; at most
--limit lines, the rest as "+N more". Network errors are a silent skip (retried next tick).
"""
import argparse
import json
import os
import sys
import tempfile
import urllib.error
from pathlib import Path

CLI_DIR = Path(__file__).resolve().parents[1] / "cli"
sys.path.insert(0, str(CLI_DIR))

import wiz_api as api  # noqa: E402
import wiz_auth as auth  # noqa: E402
import wiz_rescan as rescan  # noqa: E402

RC_CHANGED = 200
KEEP_IDS = 5000
MODES = ["findings", "counts", "rescan-done"]


def state_path(mode):
    d = Path(os.environ.get("WATCH_STATE_DIR") or Path.home() / ".dop-agent/watch-state/wiz-default")
    d.mkdir(parents=True, exist_ok=True)
    return d / f"wiz-{mode}.json"


def load_state(mode):
    p = state_path(mode)
    return json.loads(p.read_text()) if p.exists() else None


def save_state(mode, st):
    p = state_path(mode)
    fd, tmp = tempfile.mkstemp(dir=p.parent)
    with os.fdopen(fd, "w") as f:
        json.dump(st, f)
    os.replace(tmp, p)


def _line(r):
    fix = f" fix {r['fixed_version']}" if r.get("fixed_version") else ""
    return f"{r['severity']} {r['host']}: {r['package']} {r['cve']}{fix} {r['url'] or ''}".strip()


def _more(lines, limit):
    out = lines[:limit]
    if len(lines) > limit:
        out.append(f"+{len(lines) - limit} more")
    return out


def run_findings(a):
    rows = api.find_rows(api._split(a.env) or [None], a.host, api._split(a.severity, upper=True), "OPEN",
                         api.parse_tags(",".join(a.tag or [])), max_pages=a.max_pages)
    ids = [r["id"] for r in rows if r["id"]]
    prev = load_state("findings")
    old = (prev or {}).get("ids", [])
    cur = set(ids)
    save_state("findings", {"ids": (ids + [i for i in old if i not in cur])[:KEEP_IDS]})
    if prev is None:
        return []
    known = set(old)
    new = [r for r in rows if r["id"] and r["id"] not in known]
    return _more([_line(r) for r in new], a.limit)


def run_counts(a):
    sev = api._split(a.severity, upper=True) or api.SEVERITY_ORDER
    tags = api.parse_tags(",".join(a.tag or []))
    cur = {s: sum(api.count_findings(e, s, tags) for e in (api._split(a.env) or [None])) for s in sev}
    prev = load_state("counts")
    save_state("counts", {"counts": cur})
    if prev is None:
        return []
    d = {s: cur[s] - prev["counts"].get(s, 0) for s in sev}
    d = {s: v for s, v in d.items() if v > 0 or (v < 0 and not a.only_increase)}
    if not d:
        return []
    return ["open findings changed: " + ", ".join(f"{s} {prev['counts'].get(s, 0)}->{cur[s]} ({v:+d})"
                                                   for s, v in d.items())]


def run_rescan_done(a):
    if not a.asset_id:
        raise ValueError("rescan-done needs --asset-id")
    st = load_state("rescan-done") or {"reported": []}
    pending = [i for i in a.asset_id if i not in st["reported"]]
    if not pending:
        return []
    session = rescan.open_session(a.tab_id)
    lines = []
    try:
        for aid in pending:
            r = rescan.scan_activities(session, aid, a.scan_id)
            if r["done"] is None:
                raise auth.WizAuthError(f"status query failed for {aid}: {str(r.get('error'))[:200]}")
            if r["done"]:
                names = ", ".join(f"{x['name']}={x['status']}" for x in r["activities"]) or r.get("note", "")
                lines.append(f"rescan finished for {aid} ({names})")
                st["reported"].append(aid)
    finally:
        session.close()
    save_state("rescan-done", st)
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=MODES)
    ap.add_argument("--env")
    ap.add_argument("--tag", action="append")
    ap.add_argument("--severity", help="comma-separated; default CRITICAL,HIGH (findings) / all (counts)")
    ap.add_argument("--host")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--max-pages", type=int, default=6)
    ap.add_argument("--only-increase", action="store_true")
    ap.add_argument("--asset-id", action="append")
    ap.add_argument("--scan-id")
    ap.add_argument("--tab-id")
    a = ap.parse_args(argv)
    if a.mode == "findings" and not a.severity:
        a.severity = "CRITICAL,HIGH"
    try:
        auth.configure(os.environ.get("WIZ_DIR"), os.environ.get("WIZ_DATA_CENTER"))
        auth.configure_instance(os.environ.get("WIZ_INSTANCE"))
        api.configure(os.environ.get("WIZ_SCOPE_TAGS"), os.environ.get("WIZ_ENV_TAG_KEY"))
        rescan.configure(os.environ.get("WIZ_CDP_URL"))
        if a.mode != "rescan-done":
            auth.refresh_token()
        lines = {"findings": run_findings, "counts": run_counts, "rescan-done": run_rescan_done}[a.mode](a)
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"transient network error, skipping this tick: {exc}", file=sys.stderr)
        return 0
    except (RuntimeError, ValueError, ImportError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if lines:
        print(f"Wiz {a.mode}:\n" + "\n".join(lines))
        return RC_CHANGED
    return 0


if __name__ == "__main__":
    sys.exit(main())
