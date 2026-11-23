#!/usr/bin/env python3
"""Jenkins watcher (read-only). Script-watch exit codes: 0 = nothing new (silent), 200 = changed (notice printed),
1 = error (instance/token/API failure).

Modes (first arg)
  build        builds that FINISHED since the last run for a job or a whole folder (--target 'folder/sub/job' or
               'folder/sub'; folders are scanned breadth-first, multibranch branches included). One line per job
               with its latest completed build (success/failed/unstable/aborted). --only-failed keeps just the
               non-successful ones. The first run records a baseline and stays silent.
  queue-stuck  queue items waiting >= --min-minutes (default 30) or flagged stuck by Jenkins; each item id is
               reported once while it stays in the queue (the first run reports items already stuck).
Common flags: --limit N=100 (max jobs scanned for `build`), --max-requests N=25 (folder listing requests).

Instance: --instance or the JENKINS_INSTANCE env (set by the driver). State:
$WATCH_STATE_DIR/jenkins-<instance>-<mode>-<target>.json. 429/5xx are transient (silent exit 0, warning on stderr).
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import tempfile
from pathlib import Path

CLI_DIR = Path(__file__).resolve().parents[1] / "cli"
RC_CHANGED = 200
sys.path.insert(0, str(CLI_DIR))
import jkconfig  # noqa: E402
from jkclient import (Client, JenkinsError, JOB_TREE, is_folder, job_url_path,  # noqa: E402
                      norm_path, redact, walk_jobs)

EMOJI = {"SUCCESS": "✅", "FAILURE": "❌", "UNSTABLE": "⚠️", "ABORTED": "🚫", "NOT_BUILT": "⚪"}


class Transient(Exception):
    pass


def _client(instance):
    name, base, user, token, _ = jkconfig.load_instance(instance)
    return name, Client(base, user, token)


def state_path(instance, mode, target=""):
    d = Path(os.environ.get("WATCH_STATE_DIR") or Path.home() / ".dop-agent" / "watch-state" / "jenkins-default")
    d.mkdir(parents=True, exist_ok=True)
    key = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{instance}-{mode}-{target}")[:120]
    return d / f"jenkins-{key}.json"


def load_state(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def save_state(path, st):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".jk.", suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        json.dump(st, fh)
    os.replace(tmp, path)


def _dur(ms):
    s = int(ms or 0) // 1000
    return f"{s // 60}m{s % 60:02d}s"


def scan_jobs(c, args):
    target = norm_path(args.target)
    if not target:
        raise JenkinsError("--target is required (job or folder path)")
    node = c.json(job_url_path(target), tree=JOB_TREE)
    if is_folder(node):
        jobs, _ = walk_jobs(c, target, max_jobs=args.limit, max_requests=args.max_requests)
        return target, jobs
    node["fullName"] = node.get("fullName") or target
    return target, [node]


def run_build(c, args, st, first):
    seen, cur, out = st.get("builds", {}), {}, []
    _, jobs = scan_jobs(c, args)
    for j in jobs:
        lc = j.get("lastCompletedBuild") or {}
        n = lc.get("number")
        if n is None:
            continue
        cur[j["fullName"]] = n
        if first or n <= seen.get(j["fullName"], 0):
            continue
        res = lc.get("result") or "UNKNOWN"
        if args.only_failed and res == "SUCCESS":
            continue
        out.append(f"{EMOJI.get(res, '❔')} {j['fullName']} #{n} {res} in {_dur(lc.get('duration'))} {lc.get('url', '')}")
    st["builds"] = {**seen, **cur}
    return out


def run_queue(c, args, st, first):
    items = c.json("/queue", tree="items[id,why,inQueueSince,stuck,task[fullDisplayName,name]]").get("items") or []
    now = dt.datetime.now(dt.timezone.utc).timestamp() * 1000
    reported, cur, out = set(st.get("reported", [])), set(), []
    for i in items:
        mins = (now - i.get("inQueueSince", now)) / 60000
        if mins < args.min_minutes and not i.get("stuck"):
            continue
        cur.add(i["id"])
        if i["id"] in reported:
            continue
        name = (i.get("task") or {}).get("fullDisplayName") or (i.get("task") or {}).get("name")
        out.append(f"⏳ queue item #{i['id']} {name} waiting {int(mins)}m{' (stuck)' if i.get('stuck') else ''}: {(i.get('why') or '')[:140]}")
    st["reported"] = sorted(cur)
    return out


def main(argv=None, client=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["build", "queue-stuck"])
    ap.add_argument("--instance", default=os.environ.get("JENKINS_INSTANCE"))
    ap.add_argument("--target", default="")
    ap.add_argument("--only-failed", action="store_true")
    ap.add_argument("--min-minutes", type=int, default=30)
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--max-requests", type=int, default=25)
    args = ap.parse_args(argv)
    try:
        if client is None:
            name, client = _client(args.instance)
        else:
            name = args.instance or "test"
        if args.mode == "build" and not args.target:
            raise JenkinsError("--target is required (job or folder path)")
        path = state_path(name, args.mode, norm_path(args.target) if args.target else "")
        st = load_state(path)
        first = st is None
        st = st or {}
        try:
            out = run_build(client, args, st, first) if args.mode == "build" else run_queue(client, args, st, first)
        except JenkinsError as exc:
            if re.search(r"\b(429|500|502|503|504)\b", str(exc)):
                print(f"jenkins watch: transient error, skipped: {exc}", file=sys.stderr)
                return 0
            raise
        save_state(path, st)
    except (JenkinsError, jkconfig.InstanceError) as exc:
        print(f"ERROR: {redact(exc)[:300]}", file=sys.stderr)
        return 1
    if first and args.mode == "build":
        return 0
    if out:
        print("\n".join(out))
        return RC_CHANGED
    return 0


if __name__ == "__main__":
    sys.exit(main())
