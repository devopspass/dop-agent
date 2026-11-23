#!/usr/bin/env python3
"""Bitbucket watcher (Cloud and Server/DC, read-only). Script-watch exit codes: 0 = nothing new (silent),
200 = changed (notice printed), 1 = error (instance/token/API failure).

Modes (first arg). "me" is the user of the instance's token.
  review-requested   open PRs where I am a reviewer; reports PRs not seen before.
  my-pr-notes        new human comments on my open PRs (own comments and bots skipped; --include-bots keeps bots).
  my-pr-checks       my open PRs whose head-commit build status turned terminal (success/failed).
  pr                 ONE pull request (--ref '<repo>#<n>'): new comments, build status turned success/failed,
                     merged/declined. Register it with until_changed or remove it after merged/declined.
  pipeline           Cloud: ONE pipeline (--ref '<repo>#<build_number>', reports once finished) or the newest pipeline
                     of a branch (--ref '<repo>@<branch>', reports when a new one finishes).
                     Server/DC: build statuses of a branch/commit (--ref '<repo>@<branch|sha>'); no pipelines exist.
<repo> = workspace/slug (Cloud) or PROJECT_KEY/slug (Server).
Common flags: --repo R (limit PR scans to one repo; Cloud scans without it cover the newest --repo-limit=20 repos of
options.workspace; Server uses the dashboard), --limit N=30 (PRs scanned), --max-chars N=200 (comment text cut).

Instance: --instance or the BITBUCKET_INSTANCE env (set by the driver). State:
$WATCH_STATE_DIR/bitbucket-<instance>-<mode>[-<target>].json (default ~/.dop-agent/watch-state/bitbucket-default).
The first run only records a baseline (exit 0). Scans are bounded; 429/5xx/network errors are transient (silent
exit 0, warning on stderr).
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
BOT_RE = re.compile(r"\[bot\]$|[-_ ]bot$|^bot[-_ ]", re.I)
REPO = r"[A-Za-z0-9_.~-]+/[A-Za-z0-9_.-]+"
REF_PR_RE = re.compile(rf"^(?P<repo>{REPO})#(?P<n>\d+)$")
REF_BR_RE = re.compile(rf"^(?P<repo>{REPO})@(?P<ref>[A-Za-z0-9_./+-]{{1,200}})$")
REPO_RE = re.compile(rf"^{REPO}$")
SECRET = re.compile(r"(token|password|secret|authorization)([=:]\s*)\S+", re.I)


class Transient(Exception):
    pass


def _client(instance):
    sys.path.insert(0, str(CLI_DIR))
    import bbapi
    import bbconfig
    name, base, token, username, opts = bbconfig.load_instance(instance)
    c = bbapi.Client(base, bbconfig.api_url(base, opts), bbconfig.flavor(base, opts), token, username)
    c.workspace = str(opts.get("workspace") or "")
    return name, c, bbapi


def _whoami(c, mode):
    """'Me' is needed by every mode but pipeline; `pr` tolerates tokens without a user (repository access tokens)."""
    if mode == "pipeline":
        return None
    try:
        return _call(c.whoami)
    except Transient:
        raise
    except Exception:
        if mode != "pr":
            raise
        return {"name": "", "display": "", "id": ""}


def _call(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception as exc:
        status = getattr(exc, "status", 0)
        if status in (429, 500, 502, 503, 504) or type(exc).__name__ in ("ConnectionError", "Timeout", "ReadTimeout", "ConnectTimeout"):
            raise Transient(str(exc)) from exc
        raise


def state_path(instance, mode, target=""):
    d = Path(os.environ.get("WATCH_STATE_DIR") or Path.home() / ".dop-agent" / "watch-state" / "bitbucket-default")
    d.mkdir(parents=True, exist_ok=True)
    safe = lambda s: re.sub(r"[^A-Za-z0-9_.-]+", "_", s)  # noqa: E731
    return d / f"bitbucket-{safe(instance)}-{mode}{'-' + safe(target) if target else ''}.json"


def load_state(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def save_state(path, st):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".bb.", suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        json.dump(st, fh)
    os.replace(tmp, path)


def _line(p):
    return f"{p['repo']}#{p['id']} {p['title'][:100]} {p['url']}"


def _repos(args):
    return [args.repo] if args.repo else None


def _scan(c, args, role):
    return _call(c.pr_scan, role, _repos(args), args.limit, c.workspace, args.repo_limit)


def run_review_requested(c, me, args, st, first, bb):
    prs = _scan(c, args, "reviewer")
    key = lambda p: f"{p['repo']}#{p['id']}"  # noqa: E731
    seen = set(st.get("seen", []))
    out = [] if first else [f"review requested: {_line(p)}" for p in prs if key(p) not in seen]
    st["seen"] = sorted(seen | {key(p) for p in prs})[-KEEP_IDS:]
    return out


def _new_notes(c, repo, n, me, args, cursors, first, url=""):
    rows = _call(c.pr_comments, repo, n, 100)
    ckey = f"{repo}#{n}"
    old = cursors.get(ckey)
    cursors[ckey] = max([r["id"] for r in rows] + [old or 0])
    if first or old is None:
        return []
    out = []
    for r in sorted(rows, key=lambda r: r["id"]):
        if r["id"] <= old or r["deleted"] or r["author"] in (me["display"], me["name"]):
            continue
        if not args.include_bots and BOT_RE.search(r["author"]):
            continue
        where = f" ({r['path']}:{r['line']})" if r["path"] else ""
        text = SECRET.sub(r"\1\2***", re.sub(r"\s+", " ", r["text"]).strip()[:args.max_chars])
        out.append(f"comment on {repo}#{n}{where} by {r['author']}: {text} {url}")
    return out


def run_my_pr_notes(c, me, args, st, first, bb):
    cursors, out = st.get("cursors", {}), []
    for p in _scan(c, args, "author"):
        out += _new_notes(c, p["repo"], p["id"], me, args, cursors, first, p["url"])
    st["cursors"] = dict(list(cursors.items())[-KEEP_IDS:])
    return out


def run_my_pr_checks(c, me, args, st, first, bb):
    prev, cur, out = st.get("checks", {}), {}, []
    for p in _scan(c, args, "author"):
        checks = _call(c.pr_checks, p["repo"], p["id"], p["head"])
        summary = bb.checks_summary(checks)
        key = f"{p['repo']}#{p['id']}"
        cur[key] = f"{p['head']}:{summary}"
        if not first and cur[key] != prev.get(key) and summary in ("success", "failed"):
            bad = ", ".join(x["name"] for x in checks if x["state"] == "failed")[:150]
            out.append(f"checks {summary}: {_line(p)}" + (f" [failed: {bad}]" if bad else ""))
    st["checks"] = cur
    return out


def _pr_target(args):
    m = REF_PR_RE.match(args.ref or "")
    if not m:
        raise ValueError("--ref must be <repo>#<number> (workspace/slug#12 or PROJECT/slug#12)")
    return m["repo"], m["n"]


def run_pr(c, me, args, st, first, bb):
    repo, n = _pr_target(args)
    p = _call(c.pr_get, repo, n)
    checks = _call(c.pr_checks, repo, n, p["head"])
    summary = bb.checks_summary(checks)
    key = f"{p['head']}:{summary}"
    cursors = st.get("cursors", {})
    notes = _new_notes(c, repo, n, me, args, cursors, first, p["url"])
    out = []
    if not first:
        if p["state"] != st.get("state"):
            out.append(f"{repo}#{n} is {p['state']}: {p['title'][:100]} {p['url']}")
        if key != st.get("checks") and summary in ("success", "failed"):
            bad = ", ".join(x["name"] for x in checks if x["state"] == "failed")[:150]
            out.append(f"{repo}#{n} checks {summary}" + (f" [failed: {bad}]" if bad else "") + f" {p['url']}")
        out += notes
        if out and p["state"] != "open":
            out.append("(PR is no longer open: this watch can be removed)")
    st.update(state=p["state"], checks=key, cursors=cursors)
    return out


def run_pipeline(c, me, args, st, first, bb):
    m = REF_PR_RE.match(args.ref or "")
    b = REF_BR_RE.match(args.ref or "")
    if not (m or b):
        raise ValueError("--ref must be <repo>#<build_number> (Cloud) or <repo>@<branch|sha>")
    if m:
        if not c.cloud:
            raise ValueError("Server/DC has no pipelines: use --ref <repo>@<branch|sha> (build statuses)")
        p = _call(c.pipeline, m["repo"], m["n"])
        if p["status"] not in bb.TERMINAL:
            return []
        key = f"{p['uuid']}:{p['status']}"
        if key == st.get("done"):
            return []
        st["done"] = key
        return [f"pipeline #{p['id']} {p['status']}: {m['repo']} {p['ref']} {p['sha'][:8]} "
                f"{c.base_url}/{m['repo']}/pipelines/results/{p['id']}", "(pipeline finished: this watch can be removed)"]
    repo, ref = b["repo"], b["ref"]
    if c.cloud:
        rows = _call(c.pipelines, repo, ref, 1)
        if not rows:
            return []
        p = rows[0]
        state, key, label = p["status"], f"{p['uuid']}:{p['status']}", f"pipeline #{p['id']} on {ref} @{p['sha'][:8]}"
        failed = ""
    else:
        state, checks = _call(c.ref_checks, repo, ref)
        sha = (_call(c.commits, repo, ref, None, 1) or [{"sha": ref}])[0]["sha"]
        key, label = f"{sha}:{state}", f"build on {ref} @{sha[:8]}"
        failed = ", ".join(x["name"] for x in checks if x["state"] == "failed")[:150]
    if state not in bb.TERMINAL:
        st.setdefault("done", st.get("done"))
        return []
    if first or key == st.get("done"):
        st["done"] = key
        return []
    st["done"] = key
    return [f"{label} {state}: {repo}" + (f" [failed: {failed}]" if failed else "")]


MODES = {"review-requested": run_review_requested, "my-pr-notes": run_my_pr_notes, "my-pr-checks": run_my_pr_checks,
         "pr": run_pr, "pipeline": run_pipeline}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("mode", choices=sorted(MODES))
    ap.add_argument("--repo")
    ap.add_argument("--ref", help="<repo>#N for pr / pipeline, <repo>@<branch> for pipeline")
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--repo-limit", type=int, default=20)
    ap.add_argument("--max-chars", type=int, default=200)
    ap.add_argument("--include-bots", action="store_true")
    ap.add_argument("--instance", default=os.environ.get("BITBUCKET_INSTANCE"))
    args = ap.parse_args(argv)
    args.limit = max(1, min(args.limit, 100))
    args.repo_limit = max(1, min(args.repo_limit, 50))
    if args.repo and not REPO_RE.match(args.repo):
        print("bitbucket_watch: --repo must be workspace/slug or PROJECT_KEY/slug", file=sys.stderr)
        return 1
    try:
        name, c, bb = _client(args.instance)
        me = _whoami(c, args.mode)
        path = state_path(name, args.mode, args.ref or args.repo or "")
        st = load_state(path)
        first = st is None
        st = st or {}
        lines = MODES[args.mode](c, me, args, st, first, bb)
    except Transient as exc:
        print(f"transient Bitbucket API error, skipping this tick: {SECRET.sub('', str(exc))[:200]}", file=sys.stderr)
        return 0
    except Exception as exc:
        print(f"bitbucket_watch {args.mode} failed: {SECRET.sub(r'', str(exc))[:300]}", file=sys.stderr)
        return 1
    save_state(path, st)
    if lines:
        print("\n".join(lines))
        return RC_CHANGED
    return 0


if __name__ == "__main__":
    sys.exit(main())
