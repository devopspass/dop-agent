#!/usr/bin/env python3
"""Compact, LLM-friendly Bitbucket CLI (Cloud and Server/Data Center behind the same commands).

Global: --instance NAME (bitbucket integration in config.yaml; optional when exactly one is enabled).
Checks: test   (config, reach, auth; JSON {ok, message, steps})
        fetch --kind pull_request|pipeline --ident '{"repo":"ws/slug","id":"1"}'   (link card, used by dop-agent)

<repo> = `workspace/slug` (Cloud) or `PROJECT_KEY/slug` (Server/DC); a PR ref is `<repo>#<n>` (a PR URL also works).
Reads:
  whoami   repo-info <repo>   repos <workspace|PROJECT> [--search S] [--limit 30]
  branches <repo> [--search S]   commits <repo> [--ref R] [--path F] [--limit 15]   commit <repo> <sha>
  compare <repo> <base> <head>   (commits + files in head not in base)
  pr-list [--repo R] [--mine|--reviewer-me] [--state open|merged|declined|closed|all] [--limit 15]
          (--mine/--reviewer-me without --repo: Server = all repos; Cloud = newest repos of options.workspace)
  pr-status <repo>#<n>   pr-threads <repo>#<n> [--unresolved]   pr-comments <repo>#<n> [--limit 20]
  pr-files <repo>#<n>    pr-checks <repo>#<n>
  pipelines <repo> [--branch B] [--limit 15]            (Cloud)   pipeline-info <repo> <build_no|uuid> [--failed]
  step-log-tail <repo> <build_no|uuid> <step_uuid|name> [--lines 60] [--grep RE]   (Cloud)
  build-status <repo> <branch|sha>   (Cloud: newest pipeline; Server: commit build statuses)
  api GET <path> [--fields a,b.c] [--max-chars 3000]     (non-GET needs --yes and --data/--data-file)
Writes - WITHOUT --yes they only print a DRAFT of what would be sent; show it to the user, then re-run with --yes:
  pr-comment <repo>#<n> (--body T | --body-file F) [--reply COMMENT_ID] --yes
  pr-approve <repo>#<n> [--undo] --yes
  pr-merge <repo>#<n> [--strategy merge_commit|squash|fast_forward] [--close-branch] --yes   (strategy/close: Cloud only)
  pr-create <repo> --source B [--dest <default>] --title T [--body-file F] [--draft] --yes
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bbapi  # noqa: E402
import bbconfig  # noqa: E402
from bbapi import BbError, Client  # noqa: E402

REPO_RE = re.compile(r"^[A-Za-z0-9_.~-]+/[A-Za-z0-9_.-]+$")
REF_RE = re.compile(r"^(?:https?://[^/]+(?:/[^/#?]+)*?/)?(?:projects/(?P<p>[A-Za-z0-9_.~-]+)/repos/(?P<s>[A-Za-z0-9_.-]+)"
                    r"|(?P<repo>[A-Za-z0-9_.~-]+/[A-Za-z0-9_.-]+))(?:#|/pull-requests/)(?P<n>\d+)")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\(B")
SECRET = re.compile(r"(token|password|secret|authorization)([=:]\s*)\S+", re.I)
ERR_RE = re.compile(r"error|fail|fatal|exception|traceback", re.I)
SAFE_ID = re.compile(r"^[A-Za-z0-9_.{}/@~+][A-Za-z0-9_.{}/@~+-]{0,199}$")
EMOJI = {"open": "🟢", "merged": "🟣", "declined": "🔴", "success": "✅", "failed": "❌", "running": "🔄", "canceled": "🚫", "none": ""}


def one_line(text, n=200):
    return " ".join((text or "").split())[:n]


def redact(text):
    return SECRET.sub(r"\1\2***", str(text))


def parse_repo(repo):
    if not REPO_RE.match(repo or ""):
        raise BbError(f"bad repository {repo!r}: use workspace/slug (Cloud) or PROJECT_KEY/slug (Server)")
    return repo


def parse_ref(ref):
    """'ws/slug#12' or a PR URL -> ('ws/slug', '12')."""
    m = REF_RE.match(ref or "")
    if not m:
        raise BbError(f"bad reference {ref!r}: use <repo>#<number>")
    return (f"{m['p']}/{m['s']}" if m["p"] else m["repo"]), m["n"]


def safe_id(value, what):
    if not SAFE_ID.match(value or "") or ".." in value:
        raise BbError(f"bad {what} {value!r}")
    return value


def read_body(a):
    if getattr(a, "body_file", None):
        return Path(a.body_file).read_text()
    if getattr(a, "body", None) is not None:
        return a.body
    raise BbError("pass --body or --body-file")


def clean_log(text):
    lines = []
    for ln in ANSI.sub("", text).replace("\r", "\n").split("\n"):
        ln = redact(ln).strip()[:240]
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


def pr_line(p):
    return (f"{p['repo']}#{p['id']} {EMOJI.get(p['state'], '')}{p['state']}{' (draft)' if p['draft'] else ''} "
            f"@{p['author']} {p['source']} -> {p['dest']} | {one_line(p['title'], 100)}")


def draft(a, what, payload=None):
    """Writes need --yes; otherwise print the DRAFT so the user can confirm first. Returns True when it must run."""
    if getattr(a, "yes", False):
        return True
    print(f"DRAFT (nothing sent; re-run with --yes after the user confirms): {what}")
    if payload:
        print(payload[:2000])
    return False


# ---------------------------------------------------------------- reads
def cmd_whoami(c, a):
    me = c.whoami()
    return f"{me['display']} ({me['name']}) [{c.flavor}]"


def cmd_repo_info(c, a):
    r = c.repo_info(parse_repo(a.repo))
    return "\n".join([f"{r['repo']} {'private' if r['private'] else 'public'} default={r['default']} {r['language']}",
                      one_line(r["description"]), r["url"]])


def cmd_repos(c, a):
    safe_id(a.scope, "workspace/project")
    return "\n".join(f"{r['repo']} {r['updated']}" for r in c.repos(a.scope, a.search or "", a.limit)) or "(none)"


def cmd_branches(c, a):
    return "\n".join(f"{b['name']} {b['sha'][:8]} {b['date']}" for b in c.branches(parse_repo(a.repo), a.search or "", a.limit)) or "(none)"


def _commit_line(x):
    return f"{x['sha'][:8]} {x['date'][:10]} {x['author']}: {one_line(x['message'], 100)}"


def cmd_commits(c, a):
    ref = safe_id(a.ref, "ref") if a.ref else None
    return "\n".join(_commit_line(x) for x in c.commits(parse_repo(a.repo), ref, a.path, a.limit)) or "(none)"


def cmd_commit(c, a):
    x = c.commit(parse_repo(a.repo), safe_id(a.sha, "sha"))
    return "\n".join([_commit_line(x), one_line(x["message"], 400), f"files ({len(x['files'])}): " + ", ".join(x["files"][:30])])


def cmd_compare(c, a):
    commits, files = c.compare(parse_repo(a.repo), safe_id(a.base, "base"), safe_id(a.head, "head"))
    out = [f"{len(commits)} commit(s) in {a.head} not in {a.base}"] + [_commit_line(x) for x in commits[:30]]
    return "\n".join(out + [f"files ({len(files)}):"] + [f"  {f['status']} {f['path']}" for f in files[:60]])


def cmd_pr_list(c, a):
    if a.mine or a.reviewer_me:
        repos = [parse_repo(a.repo)] if a.repo else None
        prs = c.pr_scan("author" if a.mine else "reviewer", repos, a.limit, getattr(c, "workspace", ""))
    elif a.repo:
        prs = c.pr_list(parse_repo(a.repo), a.state, a.limit)
    else:
        raise BbError("pr-list needs --repo, --mine or --reviewer-me")
    return "\n".join(pr_line(p) + f" {p['url']}" for p in prs[:a.limit]) or "(none)"


def cmd_pr_status(c, a):
    repo, n = parse_ref(a.ref)
    p = c.pr_get(repo, n)
    out = [pr_line(p), p["url"]]
    out.append("reviewers: " + (", ".join(f"{r['name']}={r['state']}" for r in p["reviewers"]) or "none"))
    cm = [x for x in c.pr_comments(repo, n) if not x["deleted"]]
    open_threads = [x for x in cm if x["parent"] is None and x["path"] and x["resolved"] is not True]
    out.append(f"comments: {len(cm)} total, {len(open_threads)} inline thread(s) not marked resolved"
               + ("" if c.flavor == "server" else " (Cloud resolution state may be unavailable)"))
    try:
        checks = c.pr_checks(repo, n, p["head"])
        out.append(f"checks: {bbapi.checks_summary(checks)} ({len(checks)} status(es)) @ {p['head'][:8]}")
        out += [f"  FAILED {x['name']} {x['url']}" for x in checks if x["state"] == "failed"][:15]
    except BbError as exc:
        out.append(f"checks: n/a ({one_line(str(exc), 80)})")
    return "\n".join(out)


def cmd_pr_threads(c, a):
    repo, n = parse_ref(a.ref)
    cm = [x for x in c.pr_comments(repo, n) if not x["deleted"]]
    kids = {}
    for x in cm:
        if x["parent"] is not None:
            kids.setdefault(x["parent"], []).append(x)
    out = []
    for x in cm:
        if x["parent"] is not None or (a.unresolved and x["resolved"] is True):
            continue
        where = f"{x['path']}:{x['line']}" if x["path"] else "general"
        state = {True: "resolved", False: "open", None: ""}[x["resolved"]]
        out.append(f"[{x['id']}] {where} {state} @{x['author']}: {one_line(x['text'], 200)}")
        out += [f"    ↳ [{r['id']}] @{r['author']}: {one_line(r['text'], 200)}" for r in kids.get(x["id"], [])[:5]]
    return "\n".join(out) or "(no threads)"


def cmd_pr_comments(c, a):
    repo, n = parse_ref(a.ref)
    cm = [x for x in c.pr_comments(repo, n) if not x["deleted"]][-a.limit:]
    return "\n".join(f"[{x['id']}] {x['created'][:16]} @{x['author']}: {one_line(x['text'], 300)}" for x in cm) or "(none)"


def cmd_pr_files(c, a):
    repo, n = parse_ref(a.ref)
    files = c.pr_files(repo, n)
    return "\n".join(f"{f['status']} {f['path']}" + (f" +{f['add']}/-{f['del']}" if f["add"] is not None else "") for f in files) or "(none)"


def cmd_pr_checks(c, a):
    repo, n = parse_ref(a.ref)
    checks = c.pr_checks(repo, n)
    return "\n".join([f"checks: {bbapi.checks_summary(checks)}"] + [f"{EMOJI.get(x['state'], '')} {x['name']} {x['state']} {x['url']}" for x in checks[:30]])


def _pipe_line(p):
    return (f"#{p['id']} {EMOJI.get(p['status'], '')}{p['status']} {p['ref']} {p['sha'][:8]} {p['created'][:16]} "
            f"{p['seconds'] or ''}s @{p['author']}")


def cmd_pipelines(c, a):
    ref = safe_id(a.branch, "branch") if a.branch else None
    return "\n".join(_pipe_line(p) for p in c.pipelines(parse_repo(a.repo), ref, a.limit)) or "(none)"


def cmd_pipeline_info(c, a):
    repo = parse_repo(a.repo)
    p = c.pipeline(repo, safe_id(a.pipeline, "pipeline"))
    steps = c.pipeline_steps(repo, p["uuid"])
    if a.failed:
        steps = [s for s in steps if s["status"] == "failed"]
    return "\n".join([_pipe_line(p)] + [f"  {EMOJI.get(s['status'], '')} {s['name']} {s['status']} {s['seconds'] or ''}s step={s['uuid']}" for s in steps])


def cmd_step_log_tail(c, a):
    repo = parse_repo(a.repo)
    p = c.pipeline(repo, safe_id(a.pipeline, "pipeline"))
    step = next((s for s in c.pipeline_steps(repo, p["uuid"]) if a.step in (s["uuid"], s["name"])), None)
    if not step:
        raise BbError(f"step {a.step!r} not found: list steps with pipeline-info")
    lines = clean_log(c.step_log(repo, p["uuid"], step["uuid"]))
    return "\n".join(log_tail(lines, a.lines, a.grep))


def cmd_build_status(c, a):
    state, checks = c.ref_checks(parse_repo(a.repo), safe_id(a.ref, "ref"))
    return "\n".join([f"build: {EMOJI.get(state, '')}{state}"] + [f"  {x['state']} {x['name']} {x['url']}" for x in checks[:20]])


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
    if "://" in a.path or method not in ("GET", "POST", "PUT", "DELETE"):
        raise BbError("api takes a relative REST path (e.g. /repositories/ws/slug) and GET|POST|PUT|DELETE")
    path = a.path if a.path.startswith("/") else "/" + a.path
    body = json.loads(Path(a.data_file).read_text() if a.data_file else a.data) if (a.data or a.data_file) else None
    if method != "GET" and not draft(a, f"{method} {path}", json.dumps(body)):
        return ""
    res = c.request(method, path, body=body)
    if a.fields:
        res = pick(res, a.fields.split(","))
    text = res if isinstance(res, str) else json.dumps(res, separators=(",", ":"), default=str)
    if len(text) > a.max_chars:
        text = text[:a.max_chars] + f"...[truncated {len(text) - a.max_chars} chars]"
    return text


# ---------------------------------------------------------------- writes (draft-first)
def cmd_pr_comment(c, a):
    repo, n = parse_ref(a.ref)
    text = read_body(a)
    if not draft(a, f"comment on {repo}#{n}" + (f" (reply to {a.reply})" if a.reply else ""), text):
        return ""
    r = c.pr_comment(repo, n, text, a.reply)
    return f"commented on {repo}#{n} (comment id {r.get('id')})"


def cmd_pr_approve(c, a):
    repo, n = parse_ref(a.ref)
    if not draft(a, f"{'remove approval from' if a.undo else 'approve'} {repo}#{n}"):
        return ""
    c.pr_approve(repo, n, a.undo)
    return f"{'approval removed from' if a.undo else 'approved'} {repo}#{n}"


def cmd_pr_merge(c, a):
    repo, n = parse_ref(a.ref)
    p = c.pr_get(repo, n)
    if not draft(a, f"MERGE {repo}#{n} '{p['title'][:80]}' {p['source']} -> {p['dest']} strategy={a.strategy or 'default'}"):
        return ""
    r = c.pr_merge(repo, n, a.strategy, a.close_branch)
    return f"merged {repo}#{n}: {r.get('state', 'ok')}"


def cmd_pr_create(c, a):
    repo = parse_repo(a.repo)
    dest = a.dest or c.default_branch(repo)
    body = Path(a.body_file).read_text() if a.body_file else ""
    if not draft(a, f"create PR in {repo}: {a.source} -> {dest} title={a.title!r}{' (draft)' if a.draft else ''}", body):
        return ""
    p = c.pr_create(repo, a.source, dest, a.title, body, a.draft)
    return f"created {p['repo']}#{p['id']} {p['url']}"


# ---------------------------------------------------------------- furl + test
def cmd_fetch(c, a):
    """One JSON object for the link card: {title, status, status_emoji, url, fields}."""
    ident = json.loads(a.ident)
    repo = ident.get("repo") or f"{ident.get('project', '')}/{ident.get('slug', '')}"
    repo, n = parse_repo(repo), str(ident.get("id", ""))
    if not n.isdigit():
        raise BbError("ident needs a numeric id")
    if a.kind in ("pull_request", "pull_request_server"):
        p = c.pr_get(repo, n)
        state = "draft" if p["draft"] and p["state"] == "open" else p["state"]
        try:
            checks = bbapi.checks_summary(c.pr_checks(repo, n, p["head"]))
        except BbError:
            checks = "n/a"
        approvals = sum(r["state"] == "approved" for r in p["reviewers"])
        fields = {"title": p["title"], "state": state, "author": p["author"], "checks": checks,
                  "branch": f"{p['source']} -> {p['dest']}", "approvals": f"{approvals}/{len(p['reviewers'])}"}
        return json.dumps({"title": p["title"], "url": p["url"], "status": state,
                           "status_emoji": "⚪" if state == "draft" else EMOJI.get(state, ""), "fields": fields})
    if a.kind == "pipeline":
        p = c.pipeline(repo, n)
        fields = {"status": p["status"], "ref": p["ref"], "repo": repo, "duration": f"{p['seconds']}s" if p["seconds"] else ""}
        url = f"{c.base_url}/{repo}/pipelines/results/{n}"
        return json.dumps({"title": f"Pipeline #{n} {p['ref']}", "url": url, "status": p["status"],
                           "status_emoji": EMOJI.get(p["status"], ""), "fields": fields})
    raise BbError(f"unknown kind {a.kind!r}")


def cmd_test(_c, a):
    """Per-step connection test for Settings -> Test; never raises, never prints the token."""
    steps = []
    out = {"ok": False, "message": "", "steps": steps}

    def step(name, ok, detail=""):
        steps.append({"step": name, "ok": ok, "detail": redact(detail)})

    try:
        import requests
        step("requests", True, requests.__version__)
    except ImportError:
        step("requests", False, "missing")
        out["message"] = "python package 'requests' is missing under the bot's interpreter: pip install requests"
        return json.dumps(out)
    try:
        name, base, token, username, opts = bbconfig.load_instance(a.instance)
        fl, api = bbconfig.flavor(base, opts), bbconfig.api_url(base, opts)
        step("config", True, f"instance {name} -> {base} ({fl}, api {api}, auth {'basic' if username else 'bearer'})")
    except bbconfig.InstanceError as exc:
        step("config", False, str(exc))
        out["message"] = str(exc)
        return json.dumps(out)
    client = Client(base, api, fl, token, username)
    probe = "/user" if fl == "cloud" else "/application-properties"
    try:
        r = client.session.get(f"{api}{probe}", timeout=20)
    except requests.RequestException as exc:
        step("reach", False, type(exc).__name__)
        out["message"] = f"{api} unreachable ({type(exc).__name__}): check base_url / api_url / VPN"
        return json.dumps(out)
    step("reach", True, f"HTTP {r.status_code}")
    if r.status_code in (401, 403) or (not r.ok and fl == "cloud"):
        step("auth", False, f"HTTP {r.status_code}")
        hint = ("Cloud: an Atlassian API token/app password needs auth.username (e-mail/username); a workspace/repository "
                "access token (Bearer) has no /user and needs scopes: read repositories + pull requests"
                if fl == "cloud" else "check auth.token (HTTP access token) scope/expiry and flavor/api_url")
        if fl == "cloud" and r.status_code in (401, 403) and not username:
            hint += " [no auth.username set: Bearer used]"
        out["message"] = f"token rejected by {api} (HTTP {r.status_code}): {hint}"
        return json.dumps(out)
    if not r.ok:
        step("auth", False, f"HTTP {r.status_code}")
        out["message"] = f"{api} answered HTTP {r.status_code}: check base_url / flavor / api_url"
        return json.dumps(out)
    try:
        who = client.whoami()
        step("auth", True, f"user {who['name']}")
    except (BbError, requests.RequestException) as exc:
        step("auth", False, one_line(str(exc), 160))
        out["message"] = "authenticated but the current user could not be resolved: " + one_line(str(exc), 160)
        return json.dumps(out)
    out.update(ok=True, message=f"authenticated as {who['display']} ({fl})")
    return json.dumps(out)


# ---------------------------------------------------------------- wiring
CMDS = {"whoami": cmd_whoami, "repo-info": cmd_repo_info, "repos": cmd_repos, "branches": cmd_branches, "commits": cmd_commits,
        "commit": cmd_commit, "compare": cmd_compare, "pr-list": cmd_pr_list, "pr-status": cmd_pr_status,
        "pr-threads": cmd_pr_threads, "pr-comments": cmd_pr_comments, "pr-files": cmd_pr_files, "pr-checks": cmd_pr_checks,
        "pipelines": cmd_pipelines, "pipeline-info": cmd_pipeline_info, "step-log-tail": cmd_step_log_tail,
        "build-status": cmd_build_status, "api": cmd_api, "pr-comment": cmd_pr_comment, "pr-approve": cmd_pr_approve,
        "pr-merge": cmd_pr_merge, "pr-create": cmd_pr_create, "fetch": cmd_fetch}


def build_parser():
    p = argparse.ArgumentParser(prog="bitbucket-cli", description=__doc__.split("\n")[0])
    p.add_argument("--instance", help="bitbucket integration instance name (config.yaml)")
    s = p.add_subparsers(dest="cmd", required=True)
    s.add_parser("test")

    def add(name, *pos, **flags):
        x = s.add_parser(name)
        for q in pos:
            x.add_argument(q)
        for f, kw in flags.items():
            x.add_argument("--" + f.replace("_", "-"), **kw)
        return x

    num = lambda d: {"type": int, "default": d}  # noqa: E731
    flag = {"action": "store_true"}
    add("whoami")
    add("repo-info", "repo")
    add("repos", "scope", search={}, limit=num(30))
    add("branches", "repo", search={}, limit=num(30))
    add("commits", "repo", ref={}, path={}, limit=num(15))
    add("commit", "repo", "sha")
    add("compare", "repo", "base", "head")
    add("pr-list", repo={}, mine=flag, reviewer_me=flag, state={"default": "open", "choices": ["open", "merged", "declined", "closed", "all"]},
        limit=num(15))
    add("pr-status", "ref")
    add("pr-threads", "ref", unresolved=flag)
    add("pr-comments", "ref", limit=num(20))
    add("pr-files", "ref")
    add("pr-checks", "ref")
    add("pipelines", "repo", branch={}, limit=num(15))
    add("pipeline-info", "repo", "pipeline", failed=flag)
    add("step-log-tail", "repo", "pipeline", "step", lines=num(60), grep={})
    add("build-status", "repo", "ref")
    x = add("api", "method", "path", fields={}, max_chars=num(3000), data={}, data_file={}, yes=flag)
    add("pr-comment", "ref", body={}, body_file={}, reply={}, yes=flag)
    add("pr-approve", "ref", undo=flag, yes=flag)
    add("pr-merge", "ref", strategy={"choices": ["merge_commit", "squash", "fast_forward"]}, close_branch=flag, yes=flag)
    add("pr-create", "repo", source={"required": True}, dest={}, title={"required": True}, body_file={}, draft=flag, yes=flag)
    x = s.add_parser("fetch")
    x.add_argument("--kind", required=True)
    x.add_argument("--ident", required=True)
    return p


def _use_instance(name):
    name, base, token, username, opts = bbconfig.load_instance(name)
    c = Client(base, bbconfig.api_url(base, opts), bbconfig.flavor(base, opts), token, username)
    c.workspace = str(opts.get("workspace") or "")
    return c


def main(argv=None, client=None):
    a = build_parser().parse_args(argv)
    try:
        if a.cmd == "test":
            print(cmd_test(None, a))
            return 0
        print(CMDS[a.cmd](client or _use_instance(a.instance), a))
    except (BbError, bbconfig.InstanceError, ValueError, OSError) as e:
        print(f"ERROR: {redact(str(e))[:300]}", file=sys.stderr)
        return 1
    except Exception as e:  # network errors etc. must not leak a traceback with headers
        print(f"ERROR: {type(e).__name__}: {redact(str(e))[:200]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
