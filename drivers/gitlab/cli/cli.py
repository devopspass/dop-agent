#!/usr/bin/env python3
"""Compact, LLM-friendly GitLab CLI (complements the native `glab` CLI).

Global: --instance NAME (gitlab integration in config.yaml; optional when exactly one is enabled).
Checks: test   (config, token, reachability, auth; JSON {ok, message, steps})

Reads:
  mr-status <proj>!<iid>            threads <proj>!<iid> [--unresolved]
  note-get <proj>!<iid> <note_id>   mr-files <proj>!<iid>
  pipeline-jobs <proj> <id | --mr IID> [--failed]   pipelines <proj> [--ref R] [--status S]
  job-info <proj> <job>             job-log-tail <proj> <job> [--lines 60] [--grep RE]
  mr-list [--project P [--mine]] [--state S] [--author U] [--since D] [--until D] [--all]
  project-info <proj>   group-projects <group>   users <name...>   search-projects <q>
  branches <proj> [--search S]  branch-diffs <proj> <from> <to>  commits <proj> [--ref R] [--path F]
  commit <proj> <sha>   tags <proj> [--search S]   ci-lint <proj> [--ref R]
  api GET|POST|PUT|DELETE <path> [--fields a,b.c] [--max-chars 3000] [--data J|--data-file F] [--yes]
Writes (explicit ids, one-line result):
  mr-comment <proj>!<iid> (--body T | --body-file F) [--reply DISCUSSION_ID]
  thread-resolve <proj>!<iid> <discussion_id> [--unresolve]
  mr-merge <proj>!<iid> [--squash] [--remove-branch] [--auto]   mr-approve <proj>!<iid>
  job-play|job-retry|job-cancel <proj> <job>   pipeline-retry|pipeline-cancel <proj> <id>
  pipeline-create <proj> <ref> [--var K=V ...]
  branch-create <proj> <name> --ref R   branch-delete <proj> <name>
  tag-create <proj> <name> --ref R [--message M]   tag-delete <proj> <name>
  mr-create <proj> --source B --target B --title T [--description-file F] [--draft] [--squash] [--remove-branch]

<proj> is a full path (group/sub/proj) or a unique project-name suffix.
"""
import argparse
import json
import re
import sys
import urllib.parse
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from glclient import GitLabClient, ConfigError  # noqa: E402
import glconfig  # noqa: E402

CACHE = Path.home() / ".cache" / "dop-gitlab" / "projects.json"  # per instance, see _use_instance()
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\(B")
TS_PREFIX = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z \d\d[OE]\+?")
SECTION = re.compile(r"section_(start|end):\d+:\S*")
SECRET = re.compile(r"(token|password|secret|authorization)([=:]\s*)\S+", re.I)
ERR_RE = re.compile(r"error|fail|fatal|exception|traceback", re.I)


class GlError(RuntimeError):
    pass


class Client(GitLabClient):
    def get_all(self, path, params=None, max_pages=5):
        out, page = [], 1
        while page and page <= max_pages:
            p = dict(params or {}, per_page=100, page=page)
            r = self._get(path, p)
            out += r.json()
            nxt = r.headers.get("x-next-page")
            page = int(nxt) if nxt else 0
        return out

    def request(self, method, path, params=None, body=None):
        resp = self.session.request(method, f"{self.api}{path}", params=params, json=body, timeout=30)
        if not resp.ok:
            raise GlError(f"{resp.status_code} {method} {path}: {resp.text[:300]}")
        try:
            return resp.json()
        except ValueError:
            return resp.text


def parse_ref(ref):
    """'proj!12' -> ('proj', '12'); 'proj' -> ('proj', None)."""
    proj, sep, iid = ref.partition("!")
    if sep and not iid.isdigit():
        raise GlError(f"bad merge request reference: {ref}")
    return proj, (iid or None)


def resolve_project(client, proj):
    """Full path as-is; otherwise a unique project-name suffix (cached)."""
    if "/" in proj:
        return proj
    cache = {}
    try:
        cache = json.loads(CACHE.read_text())
    except (OSError, ValueError):
        pass
    if proj in cache:
        return cache[proj]
    r = client._get("/projects", {"search": proj, "simple": "true", "per_page": 50}).json()
    hits = [p["path_with_namespace"] for p in r if p["path_with_namespace"].rsplit("/", 1)[-1] == proj]
    if len(hits) != 1:
        raise GlError(f"project '{proj}': " + ("not found (use the full path or the exact last path segment)" if not hits else "ambiguous: " + ", ".join(hits)))
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        cache[proj] = hits[0]
        CACHE.write_text(json.dumps(cache))
    except OSError:
        pass
    return hits[0]


def clean_log(text):
    lines = []
    for ln in ANSI.sub("", text).replace("\r", "\n").split("\n"):
        ln = SECRET.sub(r"\1\2***", SECTION.sub("", TS_PREFIX.sub("", ln))).strip()[:240]
        if ln.strip():
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
    tail = lines[-n:]
    errs = [i + 1 for i, ln in enumerate(lines) if ERR_RE.search(ln)]
    hint = [f"(last error-like line: {errs[-1]} of {len(lines)})"] if errs else []
    return tail + hint


def _p(client, proj):
    return f"/projects/{client.project_path(resolve_project(client, proj))}"


def thread_rows(discussions, unresolved_only=False):
    rows = []
    for d in discussions:
        notes = [n for n in d["notes"] if not n.get("system")]
        if not notes or not notes[0].get("resolvable"):
            continue
        resolved = all(n.get("resolved") for n in notes if n.get("resolvable"))
        if unresolved_only and resolved:
            continue
        n = notes[0]
        pos = n.get("position") or {}
        loc = f"{pos.get('new_path') or pos.get('old_path')}:{pos.get('new_line') or pos.get('old_line')}" if pos else "-"
        body = " ".join(n["body"].split())[:200]
        rows.append(f"{d['id'][:8]} {'ok' if resolved else 'OPEN'} @{n['author']['username']} {loc} | {body}")
    return rows


def cmd_mr_status(c, a):
    proj, iid = parse_ref(a.ref)
    if not iid:
        raise GlError("need <proj>!<iid>")
    base = _p(c, proj)
    mr = c._get(f"{base}/merge_requests/{iid}").json()
    try:
        ap = c._get(f"{base}/merge_requests/{iid}/approvals").json()
        apr = f"{len(ap.get('approved_by', []))}/{len(ap.get('approved_by', [])) + ap.get('approvals_left', 0)}" \
              f" ({','.join(x['user']['username'] for x in ap.get('approved_by', []))})"
    except ConfigError:
        apr = "n/a"
    disc = c.get_all(f"{base}/merge_requests/{iid}/discussions")
    open_t = len(thread_rows(disc, True))
    out = [f"!{iid} {mr['title'][:80]} [{mr['state']}{' draft' if mr.get('draft') else ''}] "
           f"{mr['source_branch']}->{mr['target_branch']}",
           f"merge:{mr.get('detailed_merge_status')} approvals:{apr} threads_open:{open_t}"]
    pls = c._get(f"{base}/merge_requests/{iid}/pipelines").json()
    if pls:
        pid = pls[0]["id"]
        jobs = c.get_all(f"{base}/pipelines/{pid}/jobs")
        cnt = dict(Counter(j["status"] for j in jobs))
        out.append(f"pipeline #{pid} {pls[0]['status']} jobs:{cnt}")
        for j in [j for j in jobs if j["status"] == "failed"][:8]:
            out.append(f"  FAILED {j['id']} {j['name']}")
    else:
        out.append("pipeline: none")
    out.append(mr["web_url"])
    return "\n".join(out)


def cmd_threads(c, a):
    proj, iid = parse_ref(a.ref)
    rows = thread_rows(c.get_all(f"{_p(c, proj)}/merge_requests/{iid}/discussions"), a.unresolved)
    return "\n".join(rows) or "(no threads)"


def cmd_pipeline_jobs(c, a):
    base = _p(c, a.proj)
    pid = a.pipeline
    if a.mr:
        pls = c._get(f"{base}/merge_requests/{a.mr}/pipelines").json()
        if not pls:
            return "(no pipelines)"
        pid = pls[0]["id"]
    if not pid:
        raise GlError("need a pipeline id or --mr IID")
    jobs = c.get_all(f"{base}/pipelines/{pid}/jobs")
    try:
        jobs += [dict(b, name="[bridge] " + b["name"], duration=None) for b in
                 c.get_all(f"{base}/pipelines/{pid}/bridges")]
    except ConfigError:
        pass
    if a.failed:
        jobs = [j for j in jobs if j["status"] == "failed"]
    rows = [f"{j['id']} {j['name']} {j.get('stage', '')} {j['status']} "
            f"{int(j['duration']) if j.get('duration') else '-'}s" for j in jobs]
    return f"pipeline #{pid}\n" + ("\n".join(rows) or "(none)")


def cmd_mr_list(c, a):
    params = {"state": a.state, "per_page": a.limit, "order_by": "updated_at"}
    if a.author:
        params["author_username"] = a.author
    if a.since:
        params["created_after"] = a.since
    if a.until:
        params["created_before"] = a.until
    if a.project:
        path = f"{_p(c, a.project)}/merge_requests"
        params["scope"] = "all" if not a.mine else "created_by_me"
    elif a.all:
        path, params["scope"] = "/merge_requests", "all"
    else:
        path, params["scope"] = "/merge_requests", "created_by_me"
    rows = c._get(path, params).json()
    return "\n".join(f"{m['references']['full']} [{m['state']}{' draft' if m.get('draft') else ''}] "
                     f"{m['title'][:70]} ({m.get('detailed_merge_status')})" for m in rows) or "(none)"


def cmd_job_log_tail(c, a):
    text = c.get_job_trace_tail(resolve_project(c, a.proj), a.job, max_bytes=a.max_bytes)
    return "\n".join(log_tail(clean_log(text), a.lines, a.grep))


def _mr(c, ref):
    proj, iid = parse_ref(ref)
    if not iid:
        raise GlError("need <proj>!<iid>")
    return f"{_p(c, proj)}/merge_requests/{iid}", iid


def _body(a):
    if getattr(a, "body_file", None):
        return Path(a.body_file).read_text()
    if getattr(a, "body", None) is not None:
        return a.body
    raise GlError("need --body or --body-file")


def cmd_note_get(c, a):
    base, _ = _mr(c, a.ref)
    n = c._get(f"{base}/notes/{a.note}").json()
    return f"#{n['id']} @{n['author']['username']} {n['created_at'][:16]}\n{n['body'][:2000]}"


def cmd_mr_files(c, a):
    base, _ = _mr(c, a.ref)
    ch = c._get(f"{base}/changes").json().get("changes", [])
    return "\n".join(f"{'A' if x['new_file'] else 'D' if x['deleted_file'] else 'R' if x['renamed_file'] else 'M'} {x['new_path']}"
                     for x in ch) or "(no changes)"


def cmd_pipelines(c, a):
    params = {"per_page": a.limit, "order_by": "id", "sort": "desc"}
    for k in ("ref", "status"):
        if getattr(a, k):
            params[k] = getattr(a, k)
    rows = c._get(f"{_p(c, a.proj)}/pipelines", params).json()
    return "\n".join(f"#{r['id']} {r['status']} {r['ref']} {r['created_at'][:16]} {r['sha'][:8]}" for r in rows) or "(none)"


def cmd_job_info(c, a):
    j = c._get(f"{_p(c, a.proj)}/jobs/{a.job}").json()
    return (f"{j['id']} {j['name']} [{j['stage']}] {j['status']} {int(j.get('duration') or 0)}s "
            f"pipeline #{j['pipeline']['id']} ref:{j['ref']}"
            + (f" failure:{j['failure_reason']}" if j.get("failure_reason") else "") + f"\n{j['web_url']}")


def cmd_project_info(c, a):
    p = c.get_project(resolve_project(c, a.proj))
    return (f"{p['id']} {p['path_with_namespace']} default:{p.get('default_branch')} "
            f"{p['visibility']} last:{p['last_activity_at'][:10]}\n{p['web_url']}")


def cmd_group_projects(c, a):
    g = urllib.parse.quote(a.group, safe="")
    rows = c.get_all(f"/groups/{g}/projects", {"include_subgroups": "true", "archived": "false"}, max_pages=3)
    rows = [r for r in rows if not a.search or a.search in r["path_with_namespace"]]
    return "\n".join(r["path_with_namespace"] for r in rows[:a.limit]) or "(none)"


def cmd_search_projects(c, a):
    rows = c._get("/projects", {"search": a.query, "simple": "true", "per_page": 20}).json()
    return "\n".join(f"{r['id']} {r['path_with_namespace']}" for r in rows) or "(none)"


def cmd_users(c, a):
    out = []
    for n in a.names:
        rows = c._get("/users", {"search": n, "per_page": 5}).json()
        out += [f"{u['id']} {u['username']} {u['name']} ({u['state']})" for u in rows] or [f"{n}: not found"]
    return "\n".join(out)


def cmd_branches(c, a):
    rows = c._get(f"{_p(c, a.proj)}/repository/branches", {"search": a.search or "", "per_page": a.limit}).json()
    return "\n".join(f"{b['name']} {b['commit']['short_id']} {b['commit']['committed_date'][:10]}"
                      f"{' protected' if b.get('protected') else ''}" for b in rows) or "(none)"


def cmd_branch_diffs(c, a):
    r = c._get(f"{_p(c, a.proj)}/repository/compare", {"from": a.base, "to": a.head, "straight": "false"}).json()
    rows = [f"{'A' if d['new_file'] else 'D' if d['deleted_file'] else 'M'} {d['new_path']}" for d in r.get("diffs", [])]
    return f"{len(r.get('commits', []))} commits, {len(rows)} files\n" + "\n".join(rows[:a.limit])


def cmd_commits(c, a):
    params = {"per_page": a.limit}
    for k in ("ref", "path"):
        if getattr(a, k):
            params["ref_name" if k == "ref" else k] = getattr(a, k)
    rows = c._get(f"{_p(c, a.proj)}/repository/commits", params).json()
    return "\n".join(f"{r['short_id']} {r['committed_date'][:10]} {r['author_name']}: {r['title'][:80]}" for r in rows) or "(none)"


def cmd_commit(c, a):
    r = c._get(f"{_p(c, a.proj)}/repository/commits/{a.sha}", {"stats": "true"}).json()
    st = r.get("stats") or {}
    return f"{r['short_id']} {r['author_name']} {r['committed_date'][:16]} +{st.get('additions')}/-{st.get('deletions')}\n{r['title']}"


def cmd_tags(c, a):
    rows = c._get(f"{_p(c, a.proj)}/repository/tags", {"search": a.search or "", "per_page": a.limit}).json()
    return "\n".join(f"{t['name']} {t['commit']['short_id']} {t['commit']['created_at'][:10]}" for t in rows) or "(none)"


def cmd_ci_lint(c, a):
    params = {"dry_run": "false"}
    if a.ref:
        params["content_ref"] = a.ref
    r = c._get(f"{_p(c, a.proj)}/ci/lint", params).json()
    out = [f"valid:{r.get('valid')}"] + [f"ERR {e}" for e in r.get("errors", [])[:10]] + \
          [f"WARN {w[:160]}" for w in list(dict.fromkeys(r.get("warnings", [])))[:5]]
    return "\n".join(out)


# ---- writers ----

def _disc_id(c, base, prefix):
    """Expand the 8-char id shown by `threads` into the full discussion id."""
    if len(prefix) >= 40:
        return prefix
    hits = [d["id"] for d in c.get_all(f"{base}/discussions") if d["id"].startswith(prefix)]
    if len(hits) != 1:
        raise GlError(f"discussion '{prefix}': " + ("not found" if not hits else "ambiguous"))
    return hits[0]


def cmd_mr_comment(c, a):
    base, iid = _mr(c, a.ref)
    body = _body(a)
    path = f"{base}/discussions/{_disc_id(c, base, a.reply)}/notes" if a.reply else f"{base}/notes"
    r = c.request("POST", path, body={"body": body})
    return f"commented on !{iid}: note {r.get('id')}" + (f" in thread {a.reply[:8]}" if a.reply else "")


def cmd_thread_resolve(c, a):
    base, iid = _mr(c, a.ref)
    c.request("PUT", f"{base}/discussions/{_disc_id(c, base, a.discussion)}", body={"resolved": not a.unresolve})
    return f"!{iid} thread {a.discussion[:8]} {'unresolved' if a.unresolve else 'resolved'}"


def cmd_mr_merge(c, a):
    base, iid = _mr(c, a.ref)
    body = {"squash": a.squash, "should_remove_source_branch": a.remove_branch}
    if a.auto:
        body["auto_merge"] = True
    r = c.request("PUT", f"{base}/merge", body=body)
    return f"!{iid} {r.get('state')} merge:{r.get('detailed_merge_status')}"


def cmd_mr_approve(c, a):
    base, iid = _mr(c, a.ref)
    r = c.request("POST", f"{base}/approve")
    return f"!{iid} approved by {len(r.get('approved_by', []))}"


def _job_action(verb):
    def run(c, a):
        r = c.request("POST", f"{_p(c, a.proj)}/jobs/{a.job}/{verb}")
        return f"job {a.job} {verb}: new id {r.get('id')} status {r.get('status')}"
    return run


def _pipe_action(verb):
    def run(c, a):
        r = c.request("POST", f"{_p(c, a.proj)}/pipelines/{a.pipeline}/{verb}")
        return f"pipeline #{r.get('id')} {verb}: {r.get('status')}"
    return run


def cmd_pipeline_create(c, a):
    vars_ = []
    for v in a.var or []:
        k, sep, val = v.partition("=")
        if not sep:
            raise GlError(f"bad --var {v!r}, want K=V")
        vars_.append({"key": k, "value": val})
    r = c.request("POST", f"{_p(c, a.proj)}/pipeline", body={"ref": a.ref, "variables": vars_})
    return f"pipeline #{r.get('id')} {r.get('status')} on {a.ref}\n{r.get('web_url')}"


def cmd_branch_create(c, a):
    r = c.request("POST", f"{_p(c, a.proj)}/repository/branches", body={"branch": a.name, "ref": a.ref})
    return f"branch {r['name']} created at {r['commit']['short_id']}"


def cmd_branch_delete(c, a):
    c.request("DELETE", f"{_p(c, a.proj)}/repository/branches/{urllib.parse.quote(a.name, safe='')}")
    return f"branch {a.name} deleted"


def cmd_tag_create(c, a):
    body = {"tag_name": a.name, "ref": a.ref}
    if a.message:
        body["message"] = a.message
    r = c.request("POST", f"{_p(c, a.proj)}/repository/tags", body=body)
    return f"tag {r['name']} created at {r['commit']['short_id']}"


def cmd_tag_delete(c, a):
    c.request("DELETE", f"{_p(c, a.proj)}/repository/tags/{urllib.parse.quote(a.name, safe='')}")
    return f"tag {a.name} deleted"


def cmd_mr_create(c, a):
    desc = Path(a.description_file).read_text() if a.description_file else (a.description or "")
    body = {"source_branch": a.source, "target_branch": a.target, "title": ("Draft: " + a.title) if a.draft else a.title,
            "description": desc, "squash": a.squash, "remove_source_branch": a.remove_branch}
    r = c.request("POST", f"{_p(c, a.proj)}/merge_requests", body=body)
    return f"!{r['iid']} created [{r['state']}] {r['title'][:80]}\n{r['web_url']}"


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
        raise GlError(f"{method} needs --yes")
    path = a.path if a.path.startswith("/") else "/" + a.path
    body = json.loads(Path(a.data_file).read_text() if a.data_file else a.data) if (a.data or a.data_file) else None
    res = c.request(method, path, body=body)
    if a.fields:
        res = pick(res, a.fields.split(","))
    text = res if isinstance(res, str) else json.dumps(res, separators=(",", ":"), default=str)
    if len(text) > a.max_chars:
        text = text[:a.max_chars] + f"...[truncated {len(text) - a.max_chars} chars]"
    return text


def build_parser():
    p = argparse.ArgumentParser(prog="gitlab-cli", description=__doc__.split("\n")[0])
    p.add_argument("--instance", help="gitlab integration instance name (config.yaml)")
    s = p.add_subparsers(dest="cmd", required=True)
    s.add_parser("test")
    f = s.add_parser("fetch", help="link card JSON (used by the web UI): --kind merge_request|pipeline|job --ident '<json>'")
    f.add_argument("--kind", required=True)
    f.add_argument("--ident", required=True)
    x = s.add_parser("mr-status"); x.add_argument("ref")
    x = s.add_parser("threads"); x.add_argument("ref"); x.add_argument("--unresolved", action="store_true")
    x = s.add_parser("pipeline-jobs"); x.add_argument("proj"); x.add_argument("pipeline", nargs="?")
    x.add_argument("--mr"); x.add_argument("--failed", action="store_true")
    x = s.add_parser("mr-list"); x.add_argument("--project"); x.add_argument("--state", default="opened")
    x.add_argument("--limit", type=int, default=15); x.add_argument("--mine", action="store_true")
    x.add_argument("--author"); x.add_argument("--since"); x.add_argument("--until")
    x.add_argument("--all", action="store_true")
    x = s.add_parser("job-log-tail"); x.add_argument("proj"); x.add_argument("job")
    x.add_argument("--lines", type=int, default=60); x.add_argument("--grep")
    x.add_argument("--max-bytes", type=int, default=65536)
    x = s.add_parser("api"); x.add_argument("method"); x.add_argument("path")
    x.add_argument("--fields"); x.add_argument("--max-chars", type=int, default=3000)
    x.add_argument("--data"); x.add_argument("--data-file"); x.add_argument("--yes", action="store_true")
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

    add("note-get", "ref", "note"); add("mr-files", "ref")
    add("pipelines", "proj", ref=None, status=None, limit=15)
    add("job-info", "proj", "job"); add("project-info", "proj")
    add("group-projects", "group", search=None, limit=50); add("search-projects", "query")
    x = s.add_parser("users"); x.add_argument("names", nargs="+")
    add("branches", "proj", search=None, limit=30); add("branch-diffs", "proj", "base", "head", limit=60)
    add("commits", "proj", ref=None, path=None, limit=15); add("commit", "proj", "sha")
    add("tags", "proj", search=None, limit=15); add("ci-lint", "proj", ref=None)
    add("mr-comment", "ref", body=None, body_file=None, reply=None)
    add("thread-resolve", "ref", "discussion", unresolve=True)
    add("mr-merge", "ref", squash=True, remove_branch=True, auto=True); add("mr-approve", "ref")
    for n in ("job-play", "job-retry", "job-cancel"):
        add(n, "proj", "job")
    for n in ("pipeline-retry", "pipeline-cancel"):
        add(n, "proj", "pipeline")
    add("pipeline-create", "proj", "ref", var="+")
    add("branch-create", "proj", "name", ref=None); add("branch-delete", "proj", "name")
    add("tag-create", "proj", "name", ref=None, message=None); add("tag-delete", "proj", "name")
    add("mr-create", "proj", source=None, target="master", title=None, description=None,
        description_file=None, draft=True, squash=True, remove_branch=True)
    return p


CMDS = {"fetch": lambda c, a: cmd_fetch(c, a), "mr-status": cmd_mr_status, "threads": cmd_threads, "pipeline-jobs": cmd_pipeline_jobs,
        "mr-list": cmd_mr_list, "job-log-tail": cmd_job_log_tail, "api": cmd_api,
        "note-get": cmd_note_get, "mr-files": cmd_mr_files, "pipelines": cmd_pipelines, "job-info": cmd_job_info,
        "project-info": cmd_project_info, "group-projects": cmd_group_projects, "search-projects": cmd_search_projects,
        "users": cmd_users, "branches": cmd_branches, "branch-diffs": cmd_branch_diffs, "commits": cmd_commits,
        "commit": cmd_commit, "tags": cmd_tags, "ci-lint": cmd_ci_lint, "mr-comment": cmd_mr_comment,
        "thread-resolve": cmd_thread_resolve, "mr-merge": cmd_mr_merge, "mr-approve": cmd_mr_approve,
        "job-play": _job_action("play"), "job-retry": _job_action("retry"), "job-cancel": _job_action("cancel"),
        "pipeline-retry": _pipe_action("retry"), "pipeline-cancel": _pipe_action("cancel"),
        "pipeline-create": cmd_pipeline_create, "branch-create": cmd_branch_create, "branch-delete": cmd_branch_delete,
        "tag-create": cmd_tag_create, "tag-delete": cmd_tag_delete, "mr-create": cmd_mr_create}


def _use_instance(name):
    """Resolve the instance and scope the project cache to it; returns a Client."""
    global CACHE
    name, base, token, _opts = glconfig.load_instance(name)
    CACHE = Path.home() / ".cache" / "dop-gitlab" / name / "projects.json"
    return Client(base_url=base, token=token)


MR_EMOJI = {"opened": "🟢", "merged": "🟣", "closed": "🔴", "locked": "🔒"}
CI_EMOJI = {"success": "✅", "failed": "❌", "running": "🔄", "pending": "⏳", "created": "➕", "canceled": "🚫",
            "canceling": "🚫", "skipped": "⏭️", "manual": "✋", "scheduled": "🕒", "preparing": "⏳",
            "waiting_for_resource": "⏳"}


def cmd_fetch(c, a):
    """Link card for the web UI: ONE JSON {title, status, status_emoji, url, fields}."""
    ident = json.loads(a.ident)
    proj, iid = urllib.parse.quote(ident.get("project", ""), safe=""), ident.get("id", "")
    if a.kind == "merge_request":
        mr = c.request("GET", f"/projects/{proj}/merge_requests/{iid}")
        try:
            approved = bool(c.request("GET", f"/projects/{proj}/merge_requests/{iid}/approvals").get("approved"))
        except Exception:
            approved = None
        st = mr.get("state") or ""
        return json.dumps({"title": mr.get("title") or "", "url": mr.get("web_url") or "", "status": st,
                           "status_emoji": MR_EMOJI.get(st, ""),
                           "fields": {"title": mr.get("title") or "", "state": st,
                                      "pipeline": (mr.get("head_pipeline") or {}).get("status") or "",
                                      "approvals": approved, "author": (mr.get("author") or {}).get("username") or ""}})
    if a.kind == "pipeline":
        p = c.request("GET", f"/projects/{proj}/pipelines/{iid}")
        st = p.get("status") or ""
        return json.dumps({"title": f"Pipeline #{iid}", "url": p.get("web_url") or "", "status": st,
                           "status_emoji": CI_EMOJI.get(st, ""),
                           "fields": {"status": st, "ref": p.get("ref") or "", "project": ident.get("project", ""),
                                      "duration": p.get("duration")}})
    if a.kind == "job":
        j = c.request("GET", f"/projects/{proj}/jobs/{iid}")
        st = j.get("status") or ""
        return json.dumps({"title": j.get("name") or f"Job #{iid}", "url": j.get("web_url") or "", "status": st,
                           "status_emoji": CI_EMOJI.get(st, ""),
                           "fields": {"name": j.get("name") or "", "stage": j.get("stage") or "", "status": st,
                                      "project": ident.get("project", "")}})
    raise GlError(f"unknown kind {a.kind!r}")


def cmd_test(_c, a):
    """Per-step connection test for Settings -> Test; never raises, never prints the token."""
    steps = []
    out = {"ok": False, "message": "", "steps": steps}

    def step(name, ok, detail=""):
        steps.append({"step": name, "ok": ok, "detail": detail})
        return ok

    try:
        name, base, token, _opts = glconfig.load_instance(a.instance)
        step("config", True, f"instance {name} -> {base}")
    except glconfig.InstanceError as exc:
        step("config", False, str(exc))
        out["message"] = str(exc)
        return json.dumps(out)
    try:
        import requests
        r = requests.get(f"{base}/api/v4/user", headers={"PRIVATE-TOKEN": token}, timeout=20)
    except Exception as exc:
        step("reach", False, type(exc).__name__)
        out["message"] = f"{base} unreachable ({type(exc).__name__}): check base_url / VPN"
        return json.dumps(out)
    step("reach", True, f"HTTP {r.status_code}")
    if not r.ok:
        step("auth", False, f"HTTP {r.status_code}")
        out["message"] = f"token rejected by {base} (HTTP {r.status_code}): check auth.token scope/expiry"
        return json.dumps(out)
    user = r.json().get("username", "")
    step("auth", True, f"user {user}")
    out.update(ok=True, message=f"authenticated as {user}")
    return json.dumps(out)


def main(argv=None, client=None):
    a = build_parser().parse_args(argv)
    try:
        if a.cmd == "test":
            print(cmd_test(None, a))
            return 0
        print(CMDS[a.cmd](client or _use_instance(a.instance), a))
    except (GlError, ConfigError, glconfig.InstanceError) as e:
        msg = re.sub(r"/projects/[^ :]+", "/projects/<p>", str(e))
        print(f"ERROR: {msg[:300]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
