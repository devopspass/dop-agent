#!/usr/bin/env python3
"""Compact, LLM-friendly GitHub CLI (github.com and GitHub Enterprise; complements the native `gh` CLI).

Global: --instance NAME (github integration in config.yaml; optional when exactly one is enabled).
Checks: test   (config, token, reachability, auth; JSON {ok, message, steps})
        fetch --kind pull_request|issue|run --ident '{"repo":"o/r","id":"1"}'   (furl card, used by dop-agent)

Reads:
  whoami
  pr-status <repo>#<n>            pr-threads <repo>#<n> [--unresolved]    pr-comments <repo>#<n> [--limit 20]
  pr-files <repo>#<n>             pr-checks <repo>#<n>
  pr-list [--repo R] [--mine] [--author U] [--reviewer U] [--state open|closed|merged|all] [--since D] [--limit 15]
  runs <repo> [--branch B] [--status S] [--event E] [--workflow W] [--limit 15]   workflows <repo>
  run-artifacts <repo> [--run ID] [--limit 20]   run-logs <repo> <run_id> [--out F]   artifact-download <repo> <id> [--out F]
  caches <repo> [--limit 20]   variables <repo> (variable values + secret NAMES only)
  run-info <repo> <run_id> [--failed]   job-log-tail <repo> <job_id> [--lines 60] [--grep RE]
  issue-status <repo>#<n>         issue-list <repo> [--state S] [--label L] [--assignee U] [--author U] [--limit 15]
  repo-info <repo>   search-repos <query>   org-repos <org> [--search S]   users <name...>
  branches <repo> [--search S]    compare <repo> <base> <head>    commits <repo> [--ref R] [--path F]
  commit <repo> <sha>             tags <repo> [--search S]
  api GET|POST|PUT|PATCH|DELETE <path> [--fields a,b.c] [--max-chars 3000] [--data J|--data-file F] [--yes]
Writes (explicit ids, one-line result):
  pr-comment <repo>#<n> (--body T | --body-file F) [--reply REVIEW_COMMENT_ID]
  thread-resolve <repo>#<n> <thread_id> [--unresolve]
  pr-review <repo>#<n> --event approve|request-changes|comment [--body T | --body-file F]
  pr-merge <repo>#<n> [--method merge|squash|rebase] [--delete-branch]
  pr-create <repo> --head B [--base <default>] --title T [--body-file F] [--draft]
  run-rerun <repo> <run_id> [--failed]   run-cancel <repo> <run_id>
  workflow-dispatch <repo> <workflow> --ref R [--input K=V ...]
  workflow-state <repo> <workflow> enable|disable   run-delete <repo> <run_id>   run-approve <repo> <run_id>
  cache-delete <repo> <cache_id>
  branch-create <repo> <name> --ref R    branch-delete <repo> <name>
  tag-create <repo> <name> --ref R       tag-delete <repo> <name>
  issue-create <repo> --title T [--body-file F] [--label L ...]   issue-comment <repo>#<n> (--body T | --body-file F)
  issue-close <repo>#<n> [--reason completed|not_planned]

<repo> is always the full `owner/name`; a PR/issue ref is `owner/name#<number>` (a github URL also works).
"""
import argparse
import json
import re
import sys
import tempfile
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ghapi  # noqa: E402
import ghconfig  # noqa: E402
from ghapi import Client, GhError  # noqa: E402

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REF_RE = re.compile(r"^(?:https?://[^/]+/)?(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?:#|/pull/|/issues/)(?P<n>\d+)")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\(B")
TS_PREFIX = re.compile(r"^\ufeff?\d{4}-\d\d-\d\dT[\d:.]+Z ?")
SECRET = re.compile(r"(token|password|secret|authorization)([=:]\s*)\S+", re.I)
ERR_RE = re.compile(r"error|fail|fatal|exception|traceback", re.I)
THREADS_Q = """query($o:String!,$n:String!,$pr:Int!,$after:String){repository(owner:$o,name:$n){pullRequest(number:$pr){
reviewThreads(first:50,after:$after){pageInfo{hasNextPage endCursor}nodes{id isResolved isOutdated path line
comments(first:2){totalCount nodes{author{login}body databaseId}}}}}}}"""


def one_line(text, n=200):
    return " ".join((text or "").split())[:n]


def parse_repo(repo):
    if not REPO_RE.match(repo or ""):
        raise GhError(f"bad repository {repo!r}: use the full owner/name")
    return repo


def parse_ref(ref):
    """'owner/name#12' (or a PR/issue URL) -> ('owner/name', '12')."""
    m = REF_RE.match(ref or "")
    if not m:
        raise GhError(f"bad reference {ref!r}: use owner/name#<number>")
    return m["repo"], m["n"]


def clean_log(text):
    lines = []
    for ln in ANSI.sub("", text).replace("\r", "\n").split("\n"):
        ln = SECRET.sub(r"\1\2***", TS_PREFIX.sub("", ln)).strip()[:240]
        if ln:
            lines.append(ln)
    return lines


def log_tail(lines, n=60, grep=None, ctx=3, max_matches=3):
    if grep:
        rx = re.compile(grep, re.I)
        idx = [i for i, ln in enumerate(lines) if rx.search(ln)][-max_matches:]
        keep = sorted({j for i in idx for j in range(max(0, i - ctx), min(len(lines), i + ctx + 1))})
        out, prev = [], None
        for j in keep:
            if prev is not None and j != prev + 1:
                out.append("--")
            out.append(f"{j + 1}: {lines[j]}")
            prev = j
        return out or [f"(no match for {grep!r})"]
    errs = [i + 1 for i, ln in enumerate(lines) if ERR_RE.search(ln)]
    return lines[-n:] + ([f"(last error-like line: {errs[-1]} of {len(lines)})"] if errs else [])


def since_date(value):
    """'7d' | '12h' | ISO date -> 'YYYY-MM-DD' (GitHub search granularity)."""
    m = re.fullmatch(r"(\d+)([dh])", value or "")
    if not m:
        return (value or "")[:10]
    delta = timedelta(days=int(m[1])) if m[2] == "d" else timedelta(hours=int(m[1]))
    return (datetime.now(timezone.utc) - delta).strftime("%Y-%m-%d")


def user_arg(u):
    return "@me" if u in ("me", "@me") else u


def read_body(a):
    if getattr(a, "body_file", None):
        return Path(a.body_file).read_text()
    if getattr(a, "body", None) is not None:
        return a.body
    raise GhError("pass --body or --body-file")


# ---------------------------------------------------------------- PRs
def _reviews(c, repo, n):
    latest = {}
    for r in c.get_all(f"/repos/{repo}/pulls/{n}/reviews", max_pages=3):
        st, who = r.get("state"), (r.get("user") or {}).get("login", "?")
        if st in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED") or who not in latest:
            latest[who] = st
    return latest


def _threads(c, repo, n):
    owner, name = repo.split("/")
    out, after = [], None
    for _ in range(4):
        d = c.graphql(THREADS_Q, {"o": owner, "n": name, "pr": int(n), "after": after})
        rt = d["repository"]["pullRequest"]["reviewThreads"]
        out += rt["nodes"]
        if not rt["pageInfo"]["hasNextPage"]:
            break
        after = rt["pageInfo"]["endCursor"]
    return out


def cmd_pr_status(c, a):
    repo, n = parse_ref(a.ref)
    pr = c.json(f"/repos/{repo}/pulls/{n}")
    state = "merged" if pr.get("merged") else pr["state"]
    out = [f"{repo}#{n} {state}{' (draft)' if pr.get('draft') else ''} @{pr['user']['login']} {pr['head']['ref']} -> {pr['base']['ref']}",
           f"title: {pr['title']}", pr["html_url"],
           f"mergeable: {pr.get('mergeable')} / {pr.get('mergeable_state')}  +{pr.get('additions')}/-{pr.get('deletions')} in {pr.get('changed_files')} files"]
    reviews = _reviews(c, repo, n)
    requested = [u["login"] for u in pr.get("requested_reviewers") or []] + [t["slug"] for t in pr.get("requested_teams") or []]
    out.append("reviews: " + (", ".join(f"{u}={s}" for u, s in reviews.items()) or "none")
               + (f" | requested: {', '.join(requested)}" if requested else ""))
    try:
        th = _threads(c, repo, n)
        out.append(f"threads: {sum(not t['isResolved'] for t in th)} unresolved of {len(th)}")
    except (GhError, KeyError, TypeError) as exc:
        out.append(f"threads: n/a ({one_line(str(exc), 80)})")
    sha = pr["head"]["sha"]
    cs, items = ghapi.checks_state(c, repo, sha)
    n_by = {s: sum(1 for i in items if i[1] == s) for s in ("success", "pending", "failure")}
    out.append(f"checks: {cs} ({n_by['success']} ok, {n_by['pending']} pending, {n_by['failure']} failed) @ {sha[:7]}")
    out += [f"  FAILED {name} {url}" for name, st, url in items if st == "failure"][:15]
    return "\n".join(out)


def cmd_pr_threads(c, a):
    repo, n = parse_ref(a.ref)
    rows = []
    for t in _threads(c, repo, n):
        if a.unresolved and t["isResolved"]:
            continue
        first = (t["comments"]["nodes"] or [{}])[0]
        who = (first.get("author") or {}).get("login", "?")
        flags = ("ok" if t["isResolved"] else "OPEN") + (",outdated" if t["isOutdated"] else "")
        extra = f" (+{t['comments']['totalCount'] - 1} replies)" if t["comments"]["totalCount"] > 1 else ""
        rows.append(f"{t['id']} {flags} @{who} {t.get('path')}:{t.get('line')} c={first.get('databaseId')} | {one_line(first.get('body'))}{extra}")
    return "\n".join(rows) or "no threads"


def cmd_pr_comments(c, a):
    repo, n = parse_ref(a.ref)
    rows = c.get_all(f"/repos/{repo}/issues/{n}/comments", max_pages=3)[-a.limit:]
    return "\n".join(f"{x['id']} @{x['user']['login']} {x['created_at'][:16]} | {one_line(x['body'], 300)}" for x in rows) or "no comments"


def cmd_pr_files(c, a):
    repo, n = parse_ref(a.ref)
    rows = c.get_all(f"/repos/{repo}/pulls/{n}/files", max_pages=3)
    return "\n".join(f"{f['status'][:3]} +{f['additions']}/-{f['deletions']} {f['filename']}" for f in rows) or "no files"


def cmd_pr_checks(c, a):
    repo, n = parse_ref(a.ref)
    sha = c.json(f"/repos/{repo}/pulls/{n}")["head"]["sha"]
    state, items = ghapi.checks_state(c, repo, sha)
    return "\n".join([f"{state} @ {sha[:7]}"] + [f"{st:8} {name} {url}" for name, st, url in sorted(items, key=lambda i: i[1])]) 


def cmd_pr_list(c, a):
    q = ["is:pr"]
    if a.repo:
        q.append(f"repo:{parse_repo(a.repo)}")
    if a.author:
        q.append(f"author:{user_arg(a.author)}")
    if a.reviewer:
        q.append(f"review-requested:{user_arg(a.reviewer)}")
    if a.mine or not (a.repo or a.author or a.reviewer):
        q.append("author:@me")
    if a.state != "all":
        q.append("is:merged" if a.state == "merged" else f"is:{a.state}")
    if a.since:
        q.append(f"updated:>={since_date(a.since)}")
    r = c.json("/search/issues", {"q": " ".join(q), "sort": "updated", "order": "desc", "per_page": min(a.limit, 100)})
    rows = []
    for it in r.get("items", []):
        repo = it["repository_url"].split("/repos/")[-1]
        st = "merged" if (it.get("pull_request") or {}).get("merged_at") else it["state"]
        rows.append(f"{repo}#{it['number']} {st} @{it['user']['login']} {it['updated_at'][:10]} | {one_line(it['title'], 100)}")
    return "\n".join(rows) or "no pull requests"


def cmd_pr_comment(c, a):
    repo, n = parse_ref(a.ref)
    body = read_body(a)
    if a.reply:
        r = c.request("POST", f"/repos/{repo}/pulls/{n}/comments/{int(a.reply)}/replies", body={"body": body})
    else:
        r = c.request("POST", f"/repos/{repo}/issues/{n}/comments", body={"body": body})
    return f"commented {repo}#{n}: {r.get('html_url')}"


def cmd_thread_resolve(c, a):
    repo, n = parse_ref(a.ref)
    if not re.fullmatch(r"[A-Za-z0-9_=-]{8,100}", a.thread):
        raise GhError("thread id must be the id printed by pr-threads")
    field = "unresolveReviewThread" if a.unresolve else "resolveReviewThread"
    c.graphql("mutation($id:ID!){%s(input:{threadId:$id}){thread{isResolved}}}" % field, {"id": a.thread})
    return f"thread {a.thread} {'reopened' if a.unresolve else 'resolved'} on {repo}#{n}"


def cmd_pr_review(c, a):
    repo, n = parse_ref(a.ref)
    event = {"approve": "APPROVE", "request-changes": "REQUEST_CHANGES", "comment": "COMMENT"}[a.event]
    body = a.body if a.body is not None else (Path(a.body_file).read_text() if a.body_file else "")
    if event != "APPROVE" and not body:
        raise GhError(f"--event {a.event} needs --body or --body-file")
    c.request("POST", f"/repos/{repo}/pulls/{n}/reviews", body={"event": event, "body": body})
    return f"review {a.event} submitted on {repo}#{n}"


def cmd_pr_merge(c, a):
    repo, n = parse_ref(a.ref)
    r = c.request("PUT", f"/repos/{repo}/pulls/{n}/merge", body={"merge_method": a.method})
    msg = f"merged {repo}#{n} ({a.method}): {r.get('message', '')}"
    if a.delete_branch:
        pr = c.json(f"/repos/{repo}/pulls/{n}")
        if pr["head"]["repo"] and pr["head"]["repo"]["full_name"] == repo:
            c.request("DELETE", f"/repos/{repo}/git/refs/heads/{urllib.parse.quote(pr['head']['ref'], safe='/')}")
            msg += f"; deleted branch {pr['head']['ref']}"
    return msg


def cmd_pr_create(c, a):
    repo = parse_repo(a.repo)
    base = a.base or c.json(f"/repos/{repo}")["default_branch"]
    body = Path(a.body_file).read_text() if a.body_file else (a.body or "")
    r = c.request("POST", f"/repos/{repo}/pulls", body={"title": a.title, "head": a.head, "base": base, "body": body, "draft": a.draft})
    return f"created {repo}#{r['number']}: {r['html_url']}"


# ---------------------------------------------------------------- Actions
def _run_state(r):
    return r.get("conclusion") or r.get("status") or "?"


def cmd_runs(c, a):
    repo = parse_repo(a.repo)
    path = f"/repos/{repo}/actions/workflows/{urllib.parse.quote(a.workflow, safe='')}/runs" if a.workflow else f"/repos/{repo}/actions/runs"
    params = {k: v for k, v in {"branch": a.branch, "status": a.status, "event": a.event, "per_page": min(a.limit, 100)}.items() if v}
    rows = c.json(path, params).get("workflow_runs", [])
    return "\n".join(f"{r['id']} {_run_state(r):12} {r['head_branch']} {r['head_sha'][:7]} {r.get('name')} ({r['event']}) {r['created_at'][:16]}"
                     for r in rows) or "no runs"


def cmd_workflows(c, a):
    rows = c.json(f"/repos/{parse_repo(a.repo)}/actions/workflows", {"per_page": 100}).get("workflows", [])
    return "\n".join(f"{w['id']} {w['state']} {w['path']} | {w['name']}" for w in rows) or "no workflows"


def cmd_run_info(c, a):
    repo = parse_repo(a.repo)
    r = c.json(f"/repos/{repo}/actions/runs/{int(a.run)}")
    out = [f"run {r['id']} {_run_state(r)} {r.get('name')} attempt {r.get('run_attempt')} {r['head_branch']} {r['head_sha'][:7]} {r['html_url']}"]
    for j in c.json(f"/repos/{repo}/actions/runs/{int(a.run)}/jobs", {"per_page": 100, "filter": "latest"}).get("jobs", []):
        st = j.get("conclusion") or j.get("status")
        if a.failed and st not in ("failure", "timed_out", "cancelled"):
            continue
        bad = [s["name"] for s in j.get("steps") or [] if s.get("conclusion") == "failure"]
        out.append(f"job {j['id']} {st} {j['name']}" + (f" | failed step: {', '.join(bad)}" if bad else ""))
    return "\n".join(out)


def cmd_job_log_tail(c, a):
    repo = parse_repo(a.repo)
    resp = c.get(f"/repos/{repo}/actions/jobs/{int(a.job)}/logs")
    text = resp.content[-a.max_bytes:].decode("utf-8", "replace")
    return "\n".join(log_tail(clean_log(text), a.lines, a.grep))


def cmd_run_rerun(c, a):
    repo = parse_repo(a.repo)
    c.request("POST", f"/repos/{repo}/actions/runs/{int(a.run)}/{'rerun-failed-jobs' if a.failed else 'rerun'}")
    return f"rerun requested for run {a.run} in {repo}" + (" (failed jobs only)" if a.failed else "")


def cmd_run_cancel(c, a):
    c.request("POST", f"/repos/{parse_repo(a.repo)}/actions/runs/{int(a.run)}/cancel")
    return f"cancel requested for run {a.run}"


def cmd_run_artifacts(c, a):
    repo = parse_repo(a.repo)
    rows = c.json(f"/repos/{repo}/actions/runs/{int(a.run)}/artifacts", {"per_page": 100}).get("artifacts", []) if a.run else \
        c.json(f"/repos/{repo}/actions/artifacts", {"per_page": min(a.limit, 100)}).get("artifacts", [])
    return "\n".join(f"{r['id']} {r['name']} {r['size_in_bytes']}B {'expired' if r.get('expired') else r['created_at'][:16]}" for r in rows) or "no artifacts"


def cmd_artifact_download(c, a):
    repo = parse_repo(a.repo)
    resp = c.get(f"/repos/{repo}/actions/artifacts/{int(a.artifact)}/zip")
    return _save(resp.content, a.out, f"artifact-{int(a.artifact)}.zip")


def cmd_run_logs(c, a):
    repo = parse_repo(a.repo)
    resp = c.get(f"/repos/{repo}/actions/runs/{int(a.run)}/logs")
    return _save(resp.content, a.out, f"run-{int(a.run)}-logs.zip")


def _save(data, out, default_name):
    path = Path(out or Path(tempfile.gettempdir()) / default_name).expanduser()
    path.write_bytes(data)
    return f"saved {len(data)} bytes to {path}"


def cmd_caches(c, a):
    rows = c.json(f"/repos/{parse_repo(a.repo)}/actions/caches", {"per_page": min(a.limit, 100), "sort": "size_in_bytes", "direction": "desc"}).get("actions_caches", [])
    return "\n".join(f"{r['id']} {r['size_in_bytes']}B {r['ref']} {r['key']}" for r in rows) or "no caches"


def cmd_cache_delete(c, a):
    c.request("DELETE", f"/repos/{parse_repo(a.repo)}/actions/caches/{int(a.cache)}")
    return f"deleted cache {a.cache}"


def cmd_variables(c, a):
    # names/values of variables are not secret; secrets are listed by name only (the API never returns values)
    repo = parse_repo(a.repo)
    out = [f"var {v['name']}={one_line(v.get('value'), 80)}" for v in c.json(f"/repos/{repo}/actions/variables", {"per_page": 100}).get("variables", [])]
    out += [f"secret {v['name']}" for v in c.json(f"/repos/{repo}/actions/secrets", {"per_page": 100}).get("secrets", [])]
    return "\n".join(out) or "no variables/secrets"


def cmd_workflow_state(c, a):
    if a.state not in ("enable", "disable"):
        raise GhError("state must be enable or disable")
    wf = urllib.parse.quote(a.workflow, safe="")
    c.request("PUT", f"/repos/{parse_repo(a.repo)}/actions/workflows/{wf}/{'enable' if a.state == 'enable' else 'disable'}")
    return f"{a.state}d workflow {a.workflow}"


def cmd_run_delete(c, a):
    c.request("DELETE", f"/repos/{parse_repo(a.repo)}/actions/runs/{int(a.run)}")
    return f"deleted run {a.run}"


def cmd_run_approve(c, a):
    c.request("POST", f"/repos/{parse_repo(a.repo)}/actions/runs/{int(a.run)}/approve")
    return f"approved run {a.run} (fork PR workflow)"


def cmd_workflow_dispatch(c, a):
    inputs = {}
    for kv in a.input or []:
        k, sep, v = kv.partition("=")
        if not sep or not k:
            raise GhError(f"bad --input {kv!r}, expected K=V")
        inputs[k] = v
    wf = urllib.parse.quote(a.workflow, safe="")
    c.request("POST", f"/repos/{parse_repo(a.repo)}/actions/workflows/{wf}/dispatches", body={"ref": a.ref, "inputs": inputs})
    return f"dispatched {a.workflow} on {a.ref} in {a.repo}"


# ---------------------------------------------------------------- issues
def cmd_issue_status(c, a):
    repo, n = parse_ref(a.ref)
    i = c.json(f"/repos/{repo}/issues/{n}")
    labels = ",".join(lb["name"] for lb in i.get("labels") or [])
    assignees = ",".join(u["login"] for u in i.get("assignees") or [])
    return (f"{repo}#{n} {i['state']} @{i['user']['login']} assignees={assignees or '-'} labels={labels or '-'} comments={i['comments']}\n"
            f"title: {i['title']}\n{i['html_url']}\n{one_line(i.get('body'), 600)}")


def cmd_issue_list(c, a):
    repo = parse_repo(a.repo)
    params = {k: v for k, v in {"state": a.state, "labels": a.label, "assignee": a.assignee, "creator": a.author,
                                "per_page": min(a.limit, 100)}.items() if v}
    rows = [i for i in c.json(f"/repos/{repo}/issues", params) if "pull_request" not in i]
    return "\n".join(f"#{i['number']} {i['state']} @{i['user']['login']} {i['updated_at'][:10]} | {one_line(i['title'], 100)}" for i in rows) or "no issues"


def cmd_issue_create(c, a):
    repo = parse_repo(a.repo)
    body = Path(a.body_file).read_text() if a.body_file else (a.body or "")
    r = c.request("POST", f"/repos/{repo}/issues", body={"title": a.title, "body": body, "labels": a.label or []})
    return f"created {repo}#{r['number']}: {r['html_url']}"


def cmd_issue_comment(c, a):
    repo, n = parse_ref(a.ref)
    r = c.request("POST", f"/repos/{repo}/issues/{n}/comments", body={"body": read_body(a)})
    return f"commented {repo}#{n}: {r.get('html_url')}"


def cmd_issue_close(c, a):
    repo, n = parse_ref(a.ref)
    c.request("PATCH", f"/repos/{repo}/issues/{n}", body={"state": "closed", "state_reason": a.reason})
    return f"closed {repo}#{n} ({a.reason})"


# ---------------------------------------------------------------- repos / users / git
def cmd_whoami(c, a):
    u = c.json("/user")
    return f"{u['login']} ({u.get('name') or '-'}) {u['html_url']}"


def cmd_repo_info(c, a):
    r = c.json(f"/repos/{parse_repo(a.repo)}")
    return (f"{r['full_name']} {'private' if r['private'] else 'public'}{' archived' if r['archived'] else ''} default={r['default_branch']} "
            f"open_issues={r['open_issues_count']} pushed={r['pushed_at'][:10]}\n{r['html_url']}\n{one_line(r.get('description'), 200)}")


def cmd_search_repos(c, a):
    rows = c.json("/search/repositories", {"q": a.query, "per_page": 15}).get("items", [])
    return "\n".join(f"{r['full_name']} {r['pushed_at'][:10]} | {one_line(r.get('description'), 100)}" for r in rows) or "no repositories"


def cmd_org_repos(c, a):
    org = urllib.parse.quote(a.org, safe="")
    rows = c.get_all(f"/orgs/{org}/repos", {"type": "all"}, max_pages=3)
    if a.search:
        rows = [r for r in rows if a.search.lower() in r["name"].lower()]
    return "\n".join(f"{r['full_name']} {r['pushed_at'][:10]}" for r in rows[:a.limit]) or "no repositories"


def cmd_users(c, a):
    out = []
    for name in a.names:
        rows = c.json("/search/users", {"q": name, "per_page": 5}).get("items", [])
        out += [f"{u['login']} {u['type']} {u['html_url']}" for u in rows] or [f"{name}: not found"]
    return "\n".join(out)


def cmd_branches(c, a):
    rows = c.get_all(f"/repos/{parse_repo(a.repo)}/branches", max_pages=5)
    if a.search:
        rows = [b for b in rows if a.search.lower() in b["name"].lower()]
    return "\n".join(f"{b['name']} {b['commit']['sha'][:7]}{' protected' if b.get('protected') else ''}" for b in rows[:a.limit]) or "no branches"


def cmd_compare(c, a):
    repo = parse_repo(a.repo)
    r = c.json(f"/repos/{repo}/compare/{urllib.parse.quote(a.base, safe='')}...{urllib.parse.quote(a.head, safe='')}")
    out = [f"{r['status']} ahead={r['ahead_by']} behind={r['behind_by']} commits={r['total_commits']} files={len(r.get('files') or [])}"]
    out += [f"{f['status'][:3]} +{f['additions']}/-{f['deletions']} {f['filename']}" for f in (r.get("files") or [])[:a.limit]]
    return "\n".join(out)


def cmd_commits(c, a):
    params = {k: v for k, v in {"sha": a.ref, "path": a.path, "per_page": min(a.limit, 100)}.items() if v}
    rows = c.json(f"/repos/{parse_repo(a.repo)}/commits", params)
    return "\n".join(f"{x['sha'][:7]} {x['commit']['author']['date'][:10]} {x['commit']['author']['name']} | {one_line(x['commit']['message'], 100)}"
                     for x in rows) or "no commits"


def cmd_commit(c, a):
    x = c.json(f"/repos/{parse_repo(a.repo)}/commits/{urllib.parse.quote(a.sha, safe='')}")
    out = [f"{x['sha']} {x['commit']['author']['name']} {x['commit']['author']['date'][:16]}", one_line(x["commit"]["message"], 400)]
    out += [f"{f['status'][:3]} +{f['additions']}/-{f['deletions']} {f['filename']}" for f in (x.get("files") or [])[:60]]
    return "\n".join(out)


def cmd_tags(c, a):
    rows = c.get_all(f"/repos/{parse_repo(a.repo)}/tags", max_pages=3)
    if a.search:
        rows = [t for t in rows if a.search.lower() in t["name"].lower()]
    return "\n".join(f"{t['name']} {t['commit']['sha'][:7]}" for t in rows[:a.limit]) or "no tags"


def _ref_sha(c, repo, ref):
    return c.json(f"/repos/{repo}/commits/{urllib.parse.quote(ref, safe='')}")["sha"]


def cmd_branch_create(c, a):
    repo = parse_repo(a.repo)
    sha = _ref_sha(c, repo, a.ref or c.json(f"/repos/{repo}")["default_branch"])
    c.request("POST", f"/repos/{repo}/git/refs", body={"ref": f"refs/heads/{a.name}", "sha": sha})
    return f"created branch {a.name} at {sha[:7]} in {repo}"


def cmd_branch_delete(c, a):
    c.request("DELETE", f"/repos/{parse_repo(a.repo)}/git/refs/heads/{urllib.parse.quote(a.name, safe='/')}")
    return f"deleted branch {a.name}"


def cmd_tag_create(c, a):
    repo = parse_repo(a.repo)
    sha = _ref_sha(c, repo, a.ref)
    if a.message:
        tag = c.request("POST", f"/repos/{repo}/git/tags", body={"tag": a.name, "message": a.message, "object": sha, "type": "commit"})
        sha = tag["sha"]
    c.request("POST", f"/repos/{repo}/git/refs", body={"ref": f"refs/tags/{a.name}", "sha": sha})
    return f"created tag {a.name} in {repo}"


def cmd_tag_delete(c, a):
    c.request("DELETE", f"/repos/{parse_repo(a.repo)}/git/refs/tags/{urllib.parse.quote(a.name, safe='/')}")
    return f"deleted tag {a.name}"


def pick(obj, fields):
    if isinstance(obj, list):
        return [pick(o, fields) for o in obj]
    if not isinstance(obj, dict):
        return obj
    out = {}
    for f in fields:
        cur = obj
        for part in f.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        out[f] = cur
    return out


def cmd_api(c, a):
    method = a.method.upper()
    if method != "GET" and not a.yes:
        raise GhError(f"{method} needs --yes")
    path = a.path if a.path.startswith("/") else "/" + a.path
    body = json.loads(Path(a.data_file).read_text() if a.data_file else a.data) if (a.data or a.data_file) else None
    res = c.request(method, path, body=body)
    if a.fields:
        res = pick(res, a.fields.split(","))
    text = res if isinstance(res, str) else json.dumps(res, separators=(",", ":"), default=str)
    if len(text) > a.max_chars:
        text = text[:a.max_chars] + f"...[truncated {len(text) - a.max_chars} chars]"
    return text


# ---------------------------------------------------------------- furl + test
def _state_emoji(kind, state):
    table = {"pull_request": {"open": "🟢", "draft": "⚪", "merged": "🟣", "closed": "🔴"},
             "issue": {"open": "🟢", "closed": "🟣"},
             "run": {"success": "✅", "failure": "❌", "cancelled": "🚫", "in_progress": "🔄", "queued": "⏳",
                     "skipped": "⏭️", "timed_out": "⌛"}}
    return table.get(kind, {}).get(state, "")


def cmd_fetch(c, a):
    """One JSON object for the link card: {title, status, status_emoji, url, fields}."""
    ident = json.loads(a.ident)
    repo, n = parse_repo(ident.get("repo", "")), str(ident.get("id", ""))
    if not n.isdigit():
        raise GhError("ident needs a numeric id")
    if a.kind == "pull_request":
        pr = c.json(f"/repos/{repo}/pulls/{n}")
        state = "merged" if pr.get("merged") else "draft" if pr.get("draft") else pr["state"]
        cs = ghapi.checks_state(c, repo, pr["head"]["sha"])[0]
        fields = {"title": pr["title"], "state": state, "author": pr["user"]["login"], "checks": cs,
                  "branch": f"{pr['head']['ref']} -> {pr['base']['ref']}"}
        card = {"title": pr["title"], "url": pr["html_url"]}
    elif a.kind == "issue":
        i = c.json(f"/repos/{repo}/issues/{n}")
        state = i["state"]
        fields = {"title": i["title"], "state": state, "author": i["user"]["login"], "comments": i["comments"]}
        card = {"title": i["title"], "url": i["html_url"]}
    elif a.kind == "run":
        r = c.json(f"/repos/{repo}/actions/runs/{n}")
        state = _run_state(r)
        fields = {"name": r.get("name") or "", "status": state, "branch": r["head_branch"], "event": r["event"], "repo": repo}
        card = {"title": r.get("display_title") or r.get("name") or f"Run #{n}", "url": r["html_url"]}
    else:
        raise GhError(f"unknown kind {a.kind!r}")
    return json.dumps({**card, "status": state, "status_emoji": _state_emoji(a.kind, state), "fields": fields})


def cmd_test(_c, a):
    """Per-step connection test for Settings -> Test; never raises, never prints the token."""
    steps = []
    out = {"ok": False, "message": "", "steps": steps}

    def step(name, ok, detail=""):
        steps.append({"step": name, "ok": ok, "detail": detail})

    try:
        import requests
        step("requests", True, requests.__version__)
    except ImportError:
        step("requests", False, "missing")
        out["message"] = "python package 'requests' is missing under the bot's interpreter: pip install requests"
        return json.dumps(out)
    try:
        name, base, token, opts = ghconfig.load_instance(a.instance)
        rest, gql = ghconfig.api_urls(base, opts)
        step("config", True, f"instance {name} -> {base} (api {rest})")
    except ghconfig.InstanceError as exc:
        step("config", False, str(exc))
        out["message"] = str(exc)
        return json.dumps(out)
    client = Client(rest, token, gql)
    try:
        r = client.session.get(f"{rest}/user", timeout=20)
    except requests.RequestException as exc:
        step("reach", False, type(exc).__name__)
        out["message"] = f"{rest} unreachable ({type(exc).__name__}): check base_url / api_url / VPN"
        return json.dumps(out)
    step("reach", True, f"HTTP {r.status_code}")
    if not r.ok:
        step("auth", False, f"HTTP {r.status_code}")
        out["message"] = f"token rejected by {rest} (HTTP {r.status_code}): check auth.token scope/expiry/SSO authorization"
        return json.dumps(out)
    login = r.json().get("login", "")
    step("auth", True, f"user {login}")
    scopes = r.headers.get("X-OAuth-Scopes")
    if scopes is not None:
        step("scopes", True, scopes or "(none)")
    try:
        client.graphql("{viewer{login}}")
        step("graphql", True, "ok (review threads available)")
    except (GhError, requests.RequestException) as exc:
        step("graphql", False, one_line(str(exc), 120))
    out.update(ok=True, message=f"authenticated as {login}")
    return json.dumps(out)


# ---------------------------------------------------------------- wiring
def build_parser():
    p = argparse.ArgumentParser(prog="github-cli", description=__doc__.split("\n")[0])
    p.add_argument("--instance", help="github integration instance name (config.yaml)")
    s = p.add_subparsers(dest="cmd", required=True)
    s.add_parser("test")

    def add(name, *pos, **opts):
        x = s.add_parser(name)
        for q in pos:
            x.add_argument(q)
        for k, v in opts.items():
            flag = "--" + k.replace("_", "-")
            if v is True:
                x.add_argument(flag, action="store_true")
            elif isinstance(v, int):
                x.add_argument(flag, type=int, default=v)
            elif v == "+":
                x.add_argument(flag, action="append")
            else:
                x.add_argument(flag, default=v)
        return x

    add("fetch", kind=None, ident=None)
    add("whoami")
    add("pr-status", "ref"); add("pr-threads", "ref", unresolved=True); add("pr-comments", "ref", limit=20)
    add("pr-files", "ref"); add("pr-checks", "ref")
    x = add("pr-list", repo=None, mine=True, author=None, reviewer=None, since=None, limit=15)
    x.add_argument("--state", default="open", choices=["open", "closed", "merged", "all"])
    add("run-artifacts", "repo", run=None, limit=20); add("artifact-download", "repo", "artifact", out=None)
    add("run-logs", "repo", "run", out=None); add("caches", "repo", limit=20); add("variables", "repo")
    add("workflow-state", "repo", "workflow", "state"); add("run-delete", "repo", "run"); add("run-approve", "repo", "run")
    add("cache-delete", "repo", "cache")
    add("runs", "repo", branch=None, status=None, event=None, workflow=None, limit=15); add("workflows", "repo")
    add("run-info", "repo", "run", failed=True)
    add("job-log-tail", "repo", "job", lines=60, grep=None, max_bytes=262144)
    add("issue-status", "ref"); add("issue-list", "repo", state="open", label=None, assignee=None, author=None, limit=15)
    add("repo-info", "repo"); add("search-repos", "query"); add("org-repos", "org", search=None, limit=50)
    x = s.add_parser("users"); x.add_argument("names", nargs="+")
    add("branches", "repo", search=None, limit=30); add("compare", "repo", "base", "head", limit=60)
    add("commits", "repo", ref=None, path=None, limit=15); add("commit", "repo", "sha")
    add("tags", "repo", search=None, limit=15)
    x = add("api", "method", "path", fields=None, max_chars=3000, data=None, data_file=None, yes=True)
    add("pr-comment", "ref", body=None, body_file=None, reply=None)
    add("thread-resolve", "ref", "thread", unresolve=True)
    x = add("pr-review", "ref", body=None, body_file=None)
    x.add_argument("--event", required=True, choices=["approve", "request-changes", "comment"])
    x = add("pr-merge", "ref", delete_branch=True)
    x.add_argument("--method", default="merge", choices=["merge", "squash", "rebase"])
    add("pr-create", "repo", head=None, base=None, title=None, body=None, body_file=None, draft=True)
    add("run-rerun", "repo", "run", failed=True); add("run-cancel", "repo", "run")
    add("workflow-dispatch", "repo", "workflow", ref=None, input="+")
    add("branch-create", "repo", "name", ref=None); add("branch-delete", "repo", "name")
    add("tag-create", "repo", "name", ref=None, message=None); add("tag-delete", "repo", "name")
    add("issue-create", "repo", title=None, body=None, body_file=None, label="+")
    add("issue-comment", "ref", body=None, body_file=None)
    x = add("issue-close", "ref")
    x.add_argument("--reason", default="completed", choices=["completed", "not_planned"])
    return p


REQUIRED = {"fetch": ("kind", "ident"), "pr-create": ("head", "title"), "issue-create": ("title",),
            "workflow-dispatch": ("ref",), "tag-create": ("ref",)}

CMDS = {"fetch": cmd_fetch, "whoami": cmd_whoami, "pr-status": cmd_pr_status, "pr-threads": cmd_pr_threads,
        "pr-comments": cmd_pr_comments, "pr-files": cmd_pr_files, "pr-checks": cmd_pr_checks, "pr-list": cmd_pr_list,
        "run-artifacts": cmd_run_artifacts, "artifact-download": cmd_artifact_download, "run-logs": cmd_run_logs,
        "caches": cmd_caches, "variables": cmd_variables, "workflow-state": cmd_workflow_state, "run-delete": cmd_run_delete,
        "run-approve": cmd_run_approve, "cache-delete": cmd_cache_delete, "runs": cmd_runs, "workflows": cmd_workflows, "run-info": cmd_run_info, "job-log-tail": cmd_job_log_tail,
        "issue-status": cmd_issue_status, "issue-list": cmd_issue_list, "repo-info": cmd_repo_info,
        "search-repos": cmd_search_repos, "org-repos": cmd_org_repos, "users": cmd_users, "branches": cmd_branches,
        "compare": cmd_compare, "commits": cmd_commits, "commit": cmd_commit, "tags": cmd_tags, "api": cmd_api,
        "pr-comment": cmd_pr_comment, "thread-resolve": cmd_thread_resolve, "pr-review": cmd_pr_review,
        "pr-merge": cmd_pr_merge, "pr-create": cmd_pr_create, "run-rerun": cmd_run_rerun, "run-cancel": cmd_run_cancel,
        "workflow-dispatch": cmd_workflow_dispatch, "branch-create": cmd_branch_create,
        "branch-delete": cmd_branch_delete, "tag-create": cmd_tag_create, "tag-delete": cmd_tag_delete,
        "issue-create": cmd_issue_create, "issue-comment": cmd_issue_comment, "issue-close": cmd_issue_close}


def _use_instance(name):
    name, base, token, opts = ghconfig.load_instance(name)
    rest, gql = ghconfig.api_urls(base, opts)
    return Client(rest, token, gql)


def main(argv=None, client=None):
    a = build_parser().parse_args(argv)
    try:
        if a.cmd == "test":
            print(cmd_test(None, a))
            return 0
        for req in REQUIRED.get(a.cmd, ()):
            if not getattr(a, req, None):
                raise GhError(f"{a.cmd} needs --{req.replace('_', '-')}")
        print(CMDS[a.cmd](client or _use_instance(a.instance), a))
    except (GhError, ghconfig.InstanceError, ValueError, OSError) as e:
        print(f"ERROR: {str(e)[:300]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
