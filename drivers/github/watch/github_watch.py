#!/usr/bin/env python3
"""GitHub watcher (read-only). Script-watch exit codes: 0 = nothing new (silent), 200 = changed (notice
printed), 1 = error (instance/token/API failure).

Modes (first arg). "me" is the user of the instance's token.
  review-requested   open PRs where my review is requested; reports PRs not seen before.
  assigned           open PRs assigned to me; reports PRs not seen before.
  my-pr-checks       my open PRs whose head commit's checks turned terminal (success/failure).
  my-pr-notes        new comments, review comments and submitted reviews by others on my open PRs
                     (bots skipped unless --include-bots).
  pr                 ONE pull request (--ref owner/name#N): new comments/reviews, checks turned
                     success/failure, merged/closed. Remove the watch once it reports merged/closed.
  run                ONE workflow run (--ref owner/name#RUN_ID): fires once when it completes.
  branch-runs        workflow runs of --repo owner/name [--branch B] [--workflow W]:
                     fires for each newly completed run (success/failure/cancelled...).
Common flags: --repo owner/name (limit PR modes to one repository), --limit N=30 (PRs/runs scanned),
--max-chars N=200 (comment text cut).

Instance: --instance or the GITHUB_INSTANCE env (set by the driver). State:
$WATCH_STATE_DIR/github-<instance>-<mode>[-<target>].json (default ~/.dop-agent/watch-state/github-default).
The first run only records a baseline (exit 0; `run` reports a target that is already finished). Scans are
bounded: at most --limit PRs and 5 items per stream per PR per run; 429/5xx/rate-limit responses are
transient (silent exit 0, warning on stderr).
"""
import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

CLI_DIR = Path(__file__).resolve().parents[1] / "cli"
RC_CHANGED = 200
KEEP_IDS = 500
BOT_RE = re.compile(r"\[bot\]$|[-_]bot$", re.I)
REF_RE = re.compile(r"^(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(?P<n>\d+)$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class Transient(Exception):
    pass


def _client(instance):
    sys.path.insert(0, str(CLI_DIR))
    import ghapi
    import ghconfig
    name, base, token, opts = ghconfig.load_instance(instance)
    rest, gql = ghconfig.api_urls(base, opts)
    return name, ghapi.Client(rest, token, gql), ghapi


def _call(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception as exc:
        text = str(exc)
        if re.search(r"\b(429|500|502|503|504)\b|rate limit", text, re.I):
            raise Transient(text) from exc
        raise


def state_path(instance, mode, target=""):
    d = Path(os.environ.get("WATCH_STATE_DIR") or Path.home() / ".dop-agent" / "watch-state" / "github-default")
    d.mkdir(parents=True, exist_ok=True)
    safe = lambda s: re.sub(r"[^A-Za-z0-9_.-]+", "_", s)  # noqa: E731
    return d / f"github-{safe(instance)}-{mode}{'-' + safe(target) if target else ''}.json"


def load_state(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def save_state(path, st):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".gh.", suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        json.dump(st, fh)
    os.replace(tmp, path)


def _search_prs(c, args, qualifier):
    q = f"is:pr is:open {qualifier}" + (f" repo:{args.repo}" if args.repo else "")
    r = _call(c.json, "/search/issues", {"q": q, "sort": "updated", "order": "desc", "per_page": args.limit})
    return r.get("items", [])


def _item_ref(it):
    return f"{it['repository_url'].split('/repos/')[-1]}#{it['number']}"


def _line(it):
    return f"{_item_ref(it)} {it.get('title', '')[:100]} {it.get('html_url', '')}"


def _bad_checks(items):
    return ", ".join(name for name, s, _ in items if s == "failure")[:150]


def run_new_prs(c, me, args, st, first, gh):
    qual = "review-requested:@me" if args.mode == "review-requested" else "assignee:@me"
    label = "review requested" if args.mode == "review-requested" else "assigned"
    items = _search_prs(c, args, qual)
    seen = set(st.get("seen", []))
    out = [] if first else [f"{label}: {_line(i)}" for i in items if i["id"] not in seen]
    st["seen"] = sorted(seen | {i["id"] for i in items})[-KEEP_IDS:]
    return out


def run_pr_checks(c, me, args, st, first, gh):
    prev, out, cur = st.get("checks", {}), [], {}
    for it in _search_prs(c, args, "author:@me"):
        repo, n = _item_ref(it).split("#")
        sha = _call(c.json, f"/repos/{repo}/pulls/{n}")["head"]["sha"]
        state, items = _call(gh.checks_state, c, repo, sha)
        key = f"{repo}#{n}"
        cur[key] = f"{sha}:{state}"
        if not first and cur[key] != prev.get(key) and state in ("success", "failure"):
            bad = _bad_checks(items)
            out.append(f"checks {state}: {_line(it)}" + (f" [failed: {bad}]" if bad else ""))
    st["checks"] = cur
    return out


def _new_items(c, repo, n, me, args, cursors, first):
    """New comments / review comments / reviews on one PR (newest 5 of each stream), cursor per stream."""
    streams = {
        "issue": (f"/repos/{repo}/issues/{n}/comments", {"per_page": 100}, "comment"),
        "review_comment": (f"/repos/{repo}/pulls/{n}/comments", {"sort": "created", "direction": "desc", "per_page": 5}, "review comment"),
        "review": (f"/repos/{repo}/pulls/{n}/reviews", {"per_page": 100}, "review"),
    }
    out = []
    for sname, (path, params, label) in streams.items():
        rows = _call(c.json, path, params)
        if sname != "review_comment":
            rows = rows[-5:]
        ckey = f"{repo}#{n}:{sname}"
        old = cursors.get(ckey)
        cursors[ckey] = max([r["id"] for r in rows] + [old or 0])
        if first or old is None:
            continue
        for r in sorted(rows, key=lambda r: r["id"]):
            who = (r.get("user") or {}).get("login", "")
            if r["id"] <= old or who == me or (not args.include_bots and BOT_RE.search(who)):
                continue
            if sname == "review" and (r.get("state") == "PENDING" or (r.get("state") == "COMMENTED" and not r.get("body"))):
                continue
            body = re.sub(r"\s+", " ", r.get("body") or "").strip()[:args.max_chars]
            tag = f"{label} {r['state'].lower()}" if sname == "review" else label
            out.append(f"{tag} on {repo}#{n} by {who}: {body} {r.get('html_url', '')}")
    return out


def run_pr_notes(c, me, args, st, first, gh):
    cursors, out = st.get("cursors", {}), []
    for it in _search_prs(c, args, "author:@me"):
        repo, n = _item_ref(it).split("#")
        out += _new_items(c, repo, n, me, args, cursors, first)
    st["cursors"] = dict(list(cursors.items())[-KEEP_IDS:])
    return out


def _target(args):
    m = REF_RE.match(args.ref or "")
    if not m:
        raise ValueError("--ref must be owner/name#<number>")
    return m["repo"], m["n"]


def run_pr(c, me, args, st, first, gh):
    repo, n = _target(args)
    pr = _call(c.json, f"/repos/{repo}/pulls/{n}")
    state = "merged" if pr.get("merged") else pr["state"]
    cs, items = _call(gh.checks_state, c, repo, pr["head"]["sha"])
    key = f"{pr['head']['sha']}:{cs}"
    cursors = st.get("cursors", {})
    notes = _new_items(c, repo, n, me, args, cursors, first)
    out = []
    if not first:
        link = pr["html_url"]
        if state != st.get("state"):
            out.append(f"{repo}#{n} is {state}: {pr['title'][:100]} {link}")
        if key != st.get("checks") and cs in ("success", "failure"):
            bad = _bad_checks(items)
            out.append(f"{repo}#{n} checks {cs}" + (f" [failed: {bad}]" if bad else "") + f" {link}")
        out += notes
        if out and state != "open":
            out.append("(PR is no longer open: this watch can be removed)")
    st.update(state=state, checks=key, cursors=cursors)
    return out


def _run_line(r):
    return (f"run {r['id']} {r.get('conclusion') or r.get('status')}: {r.get('name') or ''} "
            f"{r.get('head_branch', '')} {r.get('head_sha', '')[:7]} {r.get('html_url', '')}")


def run_run(c, me, args, st, first, gh):
    repo, rid = _target(args)
    r = _call(c.json, f"/repos/{repo}/actions/runs/{rid}")
    if r.get("status") != "completed":
        return []
    key = f"{r.get('run_attempt')}:{r.get('conclusion')}"
    if key == st.get("done"):
        return []
    st["done"] = key
    return [_run_line(r), "(run finished: this watch can be removed)"]


def run_branch_runs(c, me, args, st, first, gh):
    if not args.repo:
        raise ValueError("branch-runs needs --repo owner/name")
    path = f"/repos/{args.repo}/actions/workflows/{args.workflow}/runs" if args.workflow else f"/repos/{args.repo}/actions/runs"
    params = {"per_page": min(args.limit, 100)}
    if args.branch:
        params["branch"] = args.branch
    done = [r for r in _call(c.json, path, params).get("workflow_runs", []) if r.get("status") == "completed"]
    key = lambda r: f"{r['id']}:{r.get('run_attempt')}:{r.get('conclusion')}"  # noqa: E731
    seen = set(st.get("seen", []))
    out = [] if first else [_run_line(r) for r in reversed(done) if key(r) not in seen]
    st["seen"] = sorted(seen | {key(r) for r in done})[-KEEP_IDS:]
    return out


MODES = {"review-requested": run_new_prs, "assigned": run_new_prs, "my-pr-checks": run_pr_checks,
         "my-pr-notes": run_pr_notes, "pr": run_pr, "run": run_run, "branch-runs": run_branch_runs}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("mode", choices=sorted(MODES))
    ap.add_argument("--repo")
    ap.add_argument("--ref", help="owner/name#N for modes pr / run")
    ap.add_argument("--branch")
    ap.add_argument("--workflow", help="workflow file name or id (branch-runs)")
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--max-chars", type=int, default=200)
    ap.add_argument("--include-bots", action="store_true")
    ap.add_argument("--instance", default=os.environ.get("GITHUB_INSTANCE"))
    args = ap.parse_args(argv)
    args.limit = max(1, min(args.limit, 100))
    if args.workflow and not re.fullmatch(r"[A-Za-z0-9_\-][A-Za-z0-9_.\-]*", args.workflow):
        print("github_watch: --workflow must be a workflow file name or id", file=sys.stderr)
        return 1
    if args.repo and not REPO_RE.match(args.repo):
        print("github_watch: --repo must be owner/name", file=sys.stderr)
        return 1
    try:
        name, c, gh = _client(args.instance)
        me = _call(c.json, "/user")["login"]
        target = args.ref or "-".join(filter(None, (args.repo, args.branch, args.workflow)))
        path = state_path(name, args.mode, target)
        st = load_state(path)
        first = st is None
        st = st or {}
        lines = MODES[args.mode](c, me, args, st, first, gh)
    except Transient as exc:
        print(f"transient GitHub API error, skipping this tick: {str(exc)[:200]}", file=sys.stderr)
        return 0
    except Exception as exc:
        print(f"github_watch {args.mode} failed: {str(exc)[:300]}", file=sys.stderr)
        return 1
    save_state(path, st)
    if lines:
        print("\n".join(lines))
        return RC_CHANGED
    return 0


if __name__ == "__main__":
    sys.exit(main())
