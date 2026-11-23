#!/usr/bin/env python3
"""GitLab watcher (read-only). Script-watch exit codes: 0 = nothing new (silent), 200 = changed (notice
printed), 1 = error (instance/token/API failure).

Modes (first arg). "me" is the user of the instance's token.
  review-requested   open MRs where I am a reviewer; reports MRs not seen before.
  assigned           open MRs assigned to me; reports MRs not seen before.
  my-mr-pipelines    my open MRs whose head pipeline changed to a terminal status (success/failed/canceled).
  my-mr-notes        new human review comments on my open MRs (system notes and bots are skipped;
                     --include-bots keeps bot notes).
  mr                 ONE merge request (--target 'group/project#<iid>'): new human notes and its head pipeline
                     reaching a terminal status; also reports merged/closed.
  pipeline           ONE pipeline (--target 'group/project#<id>') or the latest pipeline of a branch
                     (--target 'group/project@<ref>'): reports when it reaches a terminal status.
  job                ONE job (--target 'group/project#<job_id>'): reports when it reaches a terminal status.
                     (Register mr/pipeline/job watches with until_changed so they stop after the final report.)
Common flags: --project PATH (limit to one project, full path), --limit N=30 (MRs scanned),
--max-chars N=200 (comment text cut).

Instance: --instance or the GITLAB_INSTANCE env (set by the driver). State:
$WATCH_STATE_DIR/gitlab-<instance>-<mode>.json (default ~/.dop-agent/watch-state/gitlab-default).
The first run only records a baseline (exit 0). Scans are bounded: at most --limit MRs and 5 notes per MR per
run; 429/5xx are transient (silent exit 0, warning on stderr).
"""
import argparse
import json
import os
import re
import sys
import tempfile
import urllib.parse
from pathlib import Path

CLI_DIR = Path(__file__).resolve().parents[1] / "cli"
RC_CHANGED = 200
TERMINAL = {"success", "failed", "canceled"}
BOT_RE = re.compile(r"^(project|group)_\d+_bot|[-_]bot$", re.I)
KEEP_IDS = 500


class Transient(Exception):
    pass


def _client(instance):
    sys.path.insert(0, str(CLI_DIR))
    import glconfig
    from glclient import GitLabClient
    name, base, token, _ = glconfig.load_instance(instance)
    return name, GitLabClient(base_url=base, token=token)


def _get(c, path, params=None):
    try:
        return c._get(path, params).json()
    except Exception as exc:
        if re.search(r"\b(429|500|502|503|504)\b", str(exc)):
            raise Transient(str(exc)) from exc
        raise


def state_path(instance, mode):
    d = Path(os.environ.get("WATCH_STATE_DIR") or Path.home() / ".dop-agent" / "watch-state" / "gitlab-default")
    d.mkdir(parents=True, exist_ok=True)
    return d / f"gitlab-{re.sub(r'[^A-Za-z0-9_.-]+', '_', instance)}-{mode}.json"


def load_state(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def save_state(path, st):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".gl.", suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        json.dump(st, fh)
    os.replace(tmp, path)


def _mrs(c, args, **filters):
    params = {"state": "opened", "scope": "all", "order_by": "updated_at", "per_page": args.limit, **filters}
    path = "/merge_requests"
    if args.project:
        path = f"/projects/{urllib.parse.quote(args.project, safe='')}/merge_requests"
    return _get(c, path, params)


def _line(m):
    return f"{m['references']['full']} {m.get('title', '')[:100]} {m.get('web_url', '')}"


def run_new_mrs(c, me, args, st, first):
    flt = {"review-requested": {"reviewer_username": me}, "assigned": {"assignee_username": me}}[args.mode]
    label = "review requested" if args.mode == "review-requested" else "assigned"
    mrs = _mrs(c, args, **flt)
    seen = set(st.get("seen", []))
    out = [] if first else [f"{label}: {_line(m)}" for m in mrs if m["id"] not in seen]
    st["seen"] = sorted(seen | {m["id"] for m in mrs})[-KEEP_IDS:]
    return out


def run_pipelines(c, me, args, st, first):
    prev, out, cur = st.get("pipelines", {}), [], {}
    for m in _mrs(c, args, author_username=me):
        p = m.get("head_pipeline") or {}
        if not p:
            continue
        key, val = str(m["id"]), f"{p.get('id')}:{p.get('status')}"
        cur[key] = val
        if not first and val != prev.get(key) and p.get("status") in TERMINAL:
            out.append(f"pipeline {p.get('status')}: {_line(m)} {p.get('web_url', '')}")
    st["pipelines"] = cur
    return out


def run_notes(c, me, args, st, first):
    cursors, out = st.get("cursors", {}), []
    for m in _mrs(c, args, author_username=me):
        key = str(m["id"])
        notes = _get(c, f"/projects/{m['project_id']}/merge_requests/{m['iid']}/notes",
                     {"sort": "desc", "order_by": "created_at", "per_page": 5})
        old = cursors.get(key)
        cursors[key] = max([n["id"] for n in notes] + [old or 0])
        if first or old is None:
            continue
        for n in sorted(notes, key=lambda n: n["id"]):
            who = (n.get("author") or {}).get("username", "")
            if n["id"] <= old or n.get("system") or who == me or (not args.include_bots and BOT_RE.search(who)):
                continue
            body = re.sub(r"\s+", " ", n.get("body", "")).strip()[:args.max_chars]
            out.append(f"note on {m['references']['full']} by {who}: {body} {m.get('web_url', '')}#note_{n['id']}")
    st["cursors"] = dict(list(cursors.items())[-KEEP_IDS:])
    return out


def _split(target, seps):
    m = re.match(r"^(?P<p>.+?)(?P<s>[%s])(?P<i>[^#@]+)$" % seps, target or "")
    if not m:
        raise ValueError(f"--target must look like group/project{'#id' if seps == '#' else '#id or group/project@ref'}")
    return urllib.parse.quote(m["p"], safe=""), m["s"], m["i"]


def _ci_line(kind, ident, obj):
    return f"{kind} {ident} {obj.get('status')}: {obj.get('web_url', '')}"


def run_mr(c, me, args, st, first):
    proj, _, iid = _split(args.target, "#")
    mr = _get(c, f"/projects/{proj}/merge_requests/{iid}")
    ref = f"{args.target}"
    out, pipe = [], mr.get("head_pipeline") or {}
    notes = _get(c, f"/projects/{proj}/merge_requests/{iid}/notes",
                 {"sort": "desc", "order_by": "created_at", "per_page": 20})
    old, st["cursor"] = st.get("cursor"), max([n["id"] for n in notes] + [st.get("cursor") or 0])
    if old is not None:
        for n in sorted(notes, key=lambda n: n["id"]):
            who = (n.get("author") or {}).get("username", "")
            if n["id"] <= old or n.get("system") or who == me or (not args.include_bots and BOT_RE.search(who)):
                continue
            body = re.sub(r"\s+", " ", n.get("body", "")).strip()[:args.max_chars]
            out.append(f"note on {ref} by {who}: {body} {mr.get('web_url', '')}#note_{n['id']}")
    val = f"{pipe.get('id')}:{pipe.get('status')}"
    if not first and pipe and val != st.get("pipeline") and pipe.get("status") in TERMINAL:
        out.append(f"pipeline {pipe.get('status')} on {ref}: {pipe.get('web_url', '')}")
    st["pipeline"] = val
    if not first and mr.get("state") in ("merged", "closed") and st.get("state") != mr["state"]:
        out.append(f"{ref} is {mr['state']}: {mr.get('web_url', '')}")
    st["state"] = mr.get("state")
    return out


def run_pipeline(c, me, args, st, first):
    proj, sep, ident = _split(args.target, "#@")
    if sep == "@":
        found = _get(c, f"/projects/{proj}/pipelines", {"ref": ident, "per_page": 1, "order_by": "id", "sort": "desc"})
        if not found:
            return []
        ident = str(found[0]["id"])
    p = _get(c, f"/projects/{proj}/pipelines/{ident}")
    val = f"{p.get('id')}:{p.get('status')}"
    out = [] if first or val == st.get("last") or p.get("status") not in TERMINAL else [
        _ci_line("pipeline", f"{args.target} (#{p.get('id')})", p)]
    st["last"] = val
    return out


def run_job(c, me, args, st, first):
    proj, _, ident = _split(args.target, "#")
    j = _get(c, f"/projects/{proj}/jobs/{ident}")
    val = f"{j.get('status')}"
    out = [] if first or val == st.get("last") or j.get("status") not in TERMINAL else [
        _ci_line("job", f"{args.target} ({j.get('name', '')})", j)]
    st["last"] = val
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("mode", choices=["review-requested", "assigned", "my-mr-pipelines", "my-mr-notes", "mr", "pipeline", "job"])
    ap.add_argument("--target", help="mr/pipeline/job modes: group/project#id (pipeline also group/project@ref)")
    ap.add_argument("--project")
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--max-chars", type=int, default=200)
    ap.add_argument("--include-bots", action="store_true")
    ap.add_argument("--instance", default=os.environ.get("GITLAB_INSTANCE"))
    args = ap.parse_args(argv)
    args.limit = max(1, min(args.limit, 100))
    try:
        name, c = _client(args.instance)
        me = _get(c, "/user")["username"]
        suffix = "-" + re.sub(r"[^A-Za-z0-9_.-]+", "_", args.target) if args.target else ""
        path = state_path(name, args.mode + suffix)
        st = load_state(path)
        first = st is None
        st = st or {}
        if args.mode in ("review-requested", "assigned"):
            lines = run_new_mrs(c, me, args, st, first)
        elif args.mode == "my-mr-pipelines":
            lines = run_pipelines(c, me, args, st, first)
        elif args.mode == "my-mr-notes":
            lines = run_notes(c, me, args, st, first)
        else:
            lines = {"mr": run_mr, "pipeline": run_pipeline, "job": run_job}[args.mode](c, me, args, st, first)
    except ValueError as exc:
        print(f"gitlab_watch {args.mode}: {exc}", file=sys.stderr)
        return 1
    except Transient as exc:
        print(f"transient GitLab API error, skipping this tick: {exc}", file=sys.stderr)
        return 0
    except Exception as exc:
        print(f"gitlab_watch {args.mode} failed: {str(exc)[:300]}", file=sys.stderr)
        return 1
    save_state(path, st)
    if lines:
        print("\n".join(lines))
        return RC_CHANGED
    return 0


if __name__ == "__main__":
    sys.exit(main())
