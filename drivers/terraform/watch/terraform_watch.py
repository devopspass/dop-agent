#!/usr/bin/env python3
"""Terraform (HCP Terraform / Enterprise) watcher (read-only). Exit codes: 0 = nothing new (silent), 200 = changed
(notice printed), 1 = error (bad args / unknown instance / auth rejected). Network errors, 429 and 5xx = silent skip.

Modes (first arg); all scan the instance's organization, bounded by --max-pages (100 workspaces each, default 3):
  runs      a workspace's current run reached a terminal state (applied / planned_and_finished / errored / canceled /
            discarded ...): --search S (workspace-name substring), --tag T, --status errored,applied (filter),
            --only-failed (= errored)
  approval  a run is waiting for confirmation (confirmable plan or policy override): --search, --tag
  drift     health assessment found drift (newly drifted or the drifted-resource count changed): --search, --tag,
            --max-workspaces N=50 (assessment calls are one per workspace; only workspaces with assessments enabled)

Instance: TERRAFORM_INSTANCE (injected from the pinned integration). State: $WATCH_STATE_DIR/terraform-<mode>.json
(ids/status only). The first run records a baseline (exit 0). Backend: the driver's cli/tfclient.py (GET only).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

CLI_DIR = Path(__file__).resolve().parents[1] / "cli"
sys.path.insert(0, str(CLI_DIR))

import tfconfig  # noqa: E402
from tfclient import TERMINAL, ApiError, Client, attr, workspaces_with_runs  # noqa: E402

RC_CHANGED = 200
KEEP = 5000


def state_path(mode: str) -> Path:
    d = Path(os.environ.get("WATCH_STATE_DIR") or Path.home() / ".dop-agent" / "watch-state" / "terraform")
    d.mkdir(parents=True, exist_ok=True)
    return d / f"terraform-{mode}.json"


def load_state(mode: str):
    try:
        return json.loads(state_path(mode).read_text())
    except (OSError, ValueError):
        return None


def save_state(mode: str, st) -> None:
    p = state_path(mode)
    fd, tmp = tempfile.mkstemp(dir=p.parent)
    with os.fdopen(fd, "w") as f:
        json.dump(st, f)
    os.replace(tmp, p)


def _url(c, w, run):
    return c.run_url(attr(w, "name"), run["id"])


def check_runs(c, a):
    seen = {}
    lines = []
    prev = load_state("runs")
    want = {s for s in (a.status or "").split(",") if s} | ({"errored"} if a.only_failed else set())
    for w, run in workspaces_with_runs(c, a.search, a.tag, a.max_pages):
        if not run:
            continue
        st = attr(run, "status")
        key = f"{run['id']}:{st}"
        seen[w["id"]] = key
        if prev is not None and st in TERMINAL and prev.get(w["id"]) != key and (not want or st in want):
            lines.append(f"{attr(w, 'name')}: run {st} — {attr(run, 'message') or ''} {_url(c, w, run)}".strip())
    save_state("runs", {**(prev or {}), **seen})
    return lines


def check_approval(c, a):
    prev = load_state("approval")
    notified = set((prev or {}).get("notified", []))
    cur, lines = [], []
    for w, run in workspaces_with_runs(c, a.search, a.tag, a.max_pages):
        if not run:
            continue
        st = attr(run, "status")
        if (attr(run, "actions", {}) or {}).get("is-confirmable") or st == "policy_override":
            key = f"{run['id']}:{st}"
            cur.append(key)
            if prev is not None and key not in notified:
                lines.append(f"{attr(w, 'name')}: run needs approval ({st}) {_url(c, w, run)}")
    save_state("approval", {"notified": (cur + [k for k in notified if k not in cur])[:KEEP]})
    return lines


def check_drift(c, a):
    prev = load_state("drift")
    cur, lines = {}, []
    ws = [w for w, _ in workspaces_with_runs(c, a.search, a.tag, a.max_pages) if attr(w, "assessments-enabled")]
    for w in ws[: a.max_workspaces]:
        r = ((c.get_or_none(f"workspaces/{w['id']}/current-assessment-result") or {}).get("data"))
        if not r:
            continue
        drifted, n = bool(attr(r, "drifted")), attr(r, "resources-drifted") or 0
        cur[w["id"]] = [drifted, n]
        old = (prev or {}).get(w["id"])
        if prev is not None and drifted and (not old or not old[0] or old[1] != n):
            lines.append(f"{attr(w, 'name')}: drift detected, {n} resource(s) drifted "
                         f"{c.inst.base_url}/app/{c.inst.organization}/workspaces/{attr(w, 'name')}/health")
    save_state("drift", {**(prev or {}), **cur})
    return lines


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=["runs", "approval", "drift"])
    p.add_argument("--search", default="")
    p.add_argument("--tag", default="")
    p.add_argument("--status", default="")
    p.add_argument("--only-failed", action="store_true")
    p.add_argument("--max-pages", type=int, default=3)
    p.add_argument("--max-workspaces", type=int, default=50)
    a = p.parse_args(argv)
    a.max_pages = max(1, min(a.max_pages, 10))
    a.max_workspaces = max(1, min(a.max_workspaces, 200))
    for v in (a.search, a.tag, a.status):
        if not re.fullmatch(r"[A-Za-z0-9_.,:\-]{0,90}", v):
            print("ERROR: invalid --search/--tag/--status value", file=sys.stderr)
            return 1
    try:
        inst = tfconfig.load_instance(None)
    except tfconfig.InstanceError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    try:
        lines = {"runs": check_runs, "approval": check_approval, "drift": check_drift}[a.mode](Client(inst), a)
    except ApiError as exc:
        msg = str(exc)
        if re.search(r"HTTP (401|403|404)\b", msg):
            print(f"ERROR: {tfconfig.redact(msg, inst.token)}", file=sys.stderr)
            return 1
        return 0
    if not lines:
        return 0
    print(f"Terraform {inst.organization} ({a.mode}):")
    print("\n".join(lines[:20]) + (f"\n+{len(lines) - 20} more" if len(lines) > 20 else ""))
    return RC_CHANGED


if __name__ == "__main__":
    sys.exit(main())
