"""Bitbucket REST client (Cloud 2.0 and Server/Data Center 1.0) with a normalized model, shared by CLI and watcher.

`Client.<method>` returns the same dict shapes for both flavors so commands/watchers have no flavor branches:
  pr:      {repo,id,title,state(open|merged|declined),draft,author,source,dest,url,updated,head,reviewers,version,description}
  comment: {id,parent,author,text,created,path,line,resolved(True|False|None),deleted}
  check:   {name,state(success|failed|running|canceled),url}
`repo` is always `<workspace>/<slug>` (Cloud) or `<PROJECT_KEY>/<slug>` (Server). Errors are
`BbError("Bitbucket API error <status> <METHOD> <path>: <message>")`; the token only goes into the Authorization header.
"""
from __future__ import annotations

import base64
import re
from urllib.parse import urlsplit
from datetime import datetime, timezone

import requests

TIMEOUT = 30
STATE_MAP = {"successful": "success", "failed": "failed", "inprogress": "running", "stopped": "canceled",
             "error": "failed", "expired": "failed", "pending": "running", "in_progress": "running",
             "completed": "success"}
TERMINAL = {"success", "failed", "canceled"}


class BbError(RuntimeError):
    def __init__(self, msg, status=0):
        super().__init__(msg)
        self.status = status


def _ms_iso(ms):
    if not ms:
        return ""
    return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def norm_check(name, state, url=""):
    return {"name": name or "", "state": STATE_MAP.get(str(state).lower().replace(" ", ""), "running"), "url": url or ""}


def checks_summary(checks):
    """'failed' if any failed, 'running' if any not terminal, 'success' if all ok, 'none' when empty."""
    states = [c["state"] for c in checks]
    if not states:
        return "none"
    if "failed" in states:
        return "failed"
    if "running" in states:
        return "running"
    return "canceled" if "canceled" in states else "success"


class Client:
    def __init__(self, base_url, api, flavor, token, username="", session=None):
        self.base_url, self.api, self.flavor = base_url.rstrip("/"), api.rstrip("/"), flavor
        self.cloud = flavor == "cloud"
        self.session = session or requests.Session()
        if username:
            cred = base64.b64encode(f"{username}:{token}".encode()).decode()
            auth = f"Basic {cred}"
        else:
            auth = f"Bearer {token}"
        self.session.headers.update({"Authorization": auth, "Accept": "application/json", "User-Agent": "dop-agent-bitbucket"})
        self._me = None
        self.workspace = ""

    # ------------------------------------------------------------ transport
    def _url(self, path):
        if not path.startswith("http"):
            return f"{self.api}{path}"
        # absolute URLs (paging links) must stay on the configured host so the token cannot be redirected elsewhere
        if urlsplit(path).netloc not in (urlsplit(self.api).netloc, urlsplit(self.base_url).netloc):
            raise BbError("refusing to send credentials to a foreign host from a paging link")
        return path

    @staticmethod
    def _fail(resp, method, path):
        try:
            data = resp.json()
            msg = (data.get("error") or {}).get("message") if isinstance(data.get("error"), dict) else None
            msg = msg or ((data.get("errors") or [{}])[0].get("message") if isinstance(data.get("errors"), list) else None) or str(data)
        except (ValueError, AttributeError, IndexError):
            msg = resp.text
        raise BbError(f"Bitbucket API error {resp.status_code} {method} {path}: {str(msg)[:300]}", resp.status_code)

    def request(self, method, path, params=None, body=None, headers=None):
        resp = self.session.request(method, self._url(path), params=params, json=body, headers=headers, timeout=TIMEOUT)
        if not resp.ok:
            self._fail(resp, method, path)
        if resp.status_code == 204 or not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return resp.text

    def get(self, path, params=None):
        return self.request("GET", path, params)

    def text(self, path, params=None, headers=None):
        resp = self.session.get(self._url(path), params=params, headers=headers, timeout=TIMEOUT)
        if not resp.ok:
            self._fail(resp, "GET", path)
        return resp.text

    def pages(self, path, params=None, limit=50, max_pages=5):
        """First `limit` items of a paged collection (Cloud `next` links, Server `nextPageStart`)."""
        params, out = dict(params or {}), []
        params["pagelen" if self.cloud else "limit"] = min(limit, 100)
        nxt = None
        for _ in range(max_pages):
            data = self.request("GET", nxt or path, None if nxt else params)
            out += data.get("values", [])
            if len(out) >= limit:
                break
            if self.cloud:
                nxt = data.get("next")
                if not nxt:
                    break
            else:
                if data.get("isLastPage", True):
                    break
                params["start"] = data.get("nextPageStart", 0)
        return out[:limit]

    # ------------------------------------------------------------ paths
    def rp(self, repo):
        owner, slug = repo.split("/", 1)
        return f"/repositories/{owner}/{slug}" if self.cloud else f"/projects/{owner}/repos/{slug}"

    def prp(self, repo, n):
        return f"{self.rp(repo)}/{'pullrequests' if self.cloud else 'pull-requests'}/{n}"

    # ------------------------------------------------------------ identity
    def whoami(self):
        if self._me:
            return self._me
        if self.cloud:
            u = self.get("/user")
            self._me = {"name": u.get("nickname") or u.get("display_name", ""), "display": u.get("display_name", ""),
                        "id": u.get("uuid", "")}
        else:
            name = self.text(f"{self.base_url}/plugins/servlet/applinks/whoami").strip()
            if not re.fullmatch(r"[A-Za-z0-9_.@~\-]+", name):
                raise BbError("Bitbucket Server whoami returned no user: check auth.token / auth.username")
            u = self.get(f"/users/{name}")
            self._me = {"name": u.get("slug", name), "display": u.get("displayName", name), "id": u.get("slug", name)}
        return self._me

    # ------------------------------------------------------------ PRs
    def norm_pr(self, p):
        if self.cloud:
            dest = p.get("destination") or {}
            reviewers = [{"name": (r.get("user") or {}).get("display_name", ""), "state":
                          "approved" if r.get("approved") else (r.get("state") or "pending")}
                         for r in p.get("participants", []) if r.get("role") == "REVIEWER"]
            if not reviewers:
                reviewers = [{"name": r.get("display_name", ""), "state": "pending"} for r in p.get("reviewers", [])]
            return {"repo": (dest.get("repository") or {}).get("full_name", ""), "id": p["id"], "title": p.get("title", ""),
                    "state": {"OPEN": "open", "MERGED": "merged"}.get(p.get("state"), "declined"),
                    "draft": bool(p.get("draft")), "author": (p.get("author") or {}).get("display_name", ""),
                    "source": ((p.get("source") or {}).get("branch") or {}).get("name", ""),
                    "dest": (dest.get("branch") or {}).get("name", ""), "url": ((p.get("links") or {}).get("html") or {}).get("href", ""),
                    "updated": p.get("updated_on", ""), "head": ((p.get("source") or {}).get("commit") or {}).get("hash", ""),
                    "reviewers": reviewers, "version": None, "description": p.get("description") or ""}
        to = p.get("toRef") or {}
        repo = f"{(to.get('repository') or {}).get('project', {}).get('key', '')}/{(to.get('repository') or {}).get('slug', '')}"
        links = (p.get("links") or {}).get("self") or [{}]
        reviewers = [{"name": (r.get("user") or {}).get("displayName", ""),
                      "state": {"APPROVED": "approved", "NEEDS_WORK": "changes_requested"}.get(r.get("status"), "pending")}
                     for r in p.get("reviewers", [])]
        return {"repo": repo, "id": p["id"], "title": p.get("title", ""), "state": (p.get("state") or "").lower(),
                "draft": bool(p.get("draft")), "author": ((p.get("author") or {}).get("user") or {}).get("displayName", ""),
                "source": (p.get("fromRef") or {}).get("displayId", ""), "dest": to.get("displayId", ""),
                "url": links[0].get("href", ""), "updated": _ms_iso(p.get("updatedDate")),
                "head": (p.get("fromRef") or {}).get("latestCommit", ""), "reviewers": reviewers,
                "version": p.get("version"), "description": p.get("description") or ""}

    def pr_get(self, repo, n):
        pr = self.norm_pr(self.get(self.prp(repo, n)))
        pr["repo"] = pr["repo"] or repo
        return pr

    def pr_list(self, repo, state="open", limit=15):
        states = {"open": ["OPEN"], "merged": ["MERGED"], "declined": ["DECLINED"], "closed": ["MERGED", "DECLINED"],
                  "all": ["OPEN", "MERGED", "DECLINED"]}[state]
        if self.cloud:
            params = {"state": states, "sort": "-updated_on"}
        else:
            params = {"state": states[0] if len(states) == 1 else "ALL", "order": "NEWEST"}
        rows = self.pages(self.prp(repo, "").rstrip("/"), params, limit)
        prs = [self.norm_pr(r) for r in rows]
        return [p for p in prs if p["state"] in {s.lower() for s in states}]

    def pr_scan(self, role, repos=None, limit=30, workspace="", repo_limit=20):
        """Open PRs where I am `author` or `reviewer`. Server: the dashboard (all repos). Cloud: per repo."""
        me = self.whoami()
        if not self.cloud:
            rows = self.pages("/dashboard/pull-requests", {"role": role.upper(), "state": "OPEN", "order": "NEWEST"}, limit)
            prs = [self.norm_pr(r) for r in rows]
            return [p for p in prs if not repos or p["repo"] in repos]
        if not repos:
            if not workspace:
                raise BbError("Cloud scans need --repo workspace/slug or options.workspace on the instance")
            rows = self.pages(f"/repositories/{workspace}", {"sort": "-updated_on"}, repo_limit, max_pages=1)
            repos = [r["full_name"] for r in rows]
        field = "author.uuid" if role == "author" else "reviewers.uuid"
        out = []
        for repo in repos:
            q = f'state="OPEN" AND {field}="{me["id"]}"'
            try:
                rows = self.pages(f"{self.rp(repo)}/pullrequests", {"q": q, "sort": "-updated_on"}, limit, max_pages=1)
            except BbError as exc:
                if exc.status in (403, 404):
                    continue
                raise
            out += [self.norm_pr(r) for r in rows]
        return out[:limit]

    def pr_comments(self, repo, n, limit=100):
        if self.cloud:
            rows = self.pages(f"{self.prp(repo, n)}/comments", {"sort": "-created_on"}, limit)
            out = []
            for c in rows:
                inline = c.get("inline") or {}
                out.append({"id": c["id"], "parent": (c.get("parent") or {}).get("id"),
                            "author": (c.get("user") or {}).get("display_name", ""),
                            "text": (c.get("content") or {}).get("raw", ""), "created": c.get("created_on", ""),
                            "path": inline.get("path", ""), "line": inline.get("to") or inline.get("from"),
                            "resolved": (True if c.get("resolution") else None) if "resolution" in c else None,
                            "deleted": bool(c.get("deleted"))})
            return sorted(out, key=lambda c: c["created"])
        out = []
        for act in self.pages(f"{self.prp(repo, n)}/activities", {}, limit, max_pages=5):
            if act.get("action") != "COMMENTED":
                continue
            anchor = act.get("commentAnchor") or {}

            def add(c, parent):
                out.append({"id": c["id"], "parent": parent, "author": (c.get("author") or {}).get("displayName", ""),
                            "text": c.get("text", ""), "created": _ms_iso(c.get("createdDate")),
                            "path": anchor.get("path", ""), "line": anchor.get("line"),
                            "resolved": (c.get("threadResolved") if parent is None else None), "deleted": False})
                for r in c.get("comments", []):
                    add(r, c["id"])
            add(act["comment"], None)
        return sorted(out, key=lambda c: c["created"])

    def pr_files(self, repo, n, limit=100):
        if self.cloud:
            rows = self.pages(f"{self.prp(repo, n)}/diffstat", {}, limit)
            return [{"path": (r.get("new") or r.get("old") or {}).get("path", ""), "status": r.get("status", ""),
                     "add": r.get("lines_added"), "del": r.get("lines_removed")} for r in rows]
        rows = self.pages(f"{self.prp(repo, n)}/changes", {}, limit)
        return [{"path": (r.get("path") or {}).get("toString", ""), "status": (r.get("type") or "").lower(), "add": None, "del": None}
                for r in rows]

    def commit_checks(self, repo, sha, limit=50):
        """Build statuses of a commit (Cloud commit statuses incl. Pipelines; Server build-status plugin)."""
        if not sha:
            return []
        if self.cloud:
            rows = self.pages(f"{self.rp(repo)}/commit/{sha}/statuses", {}, limit, max_pages=2)
            return [norm_check(r.get("name") or r.get("key"), r.get("state"), r.get("url")) for r in rows]
        rows = self.pages(f"{self.base_url}/rest/build-status/1.0/commits/{sha}", {}, limit, max_pages=2)
        return [norm_check(r.get("name") or r.get("key"), r.get("state"), r.get("url")) for r in rows]

    def pr_checks(self, repo, n, head=None):
        if self.cloud:
            rows = self.pages(f"{self.prp(repo, n)}/statuses", {}, 50, max_pages=2)
            return [norm_check(r.get("name") or r.get("key"), r.get("state"), r.get("url")) for r in rows]
        return self.commit_checks(repo, head or self.pr_get(repo, n)["head"])

    # ------------------------------------------------------------ PR writes
    def pr_comment(self, repo, n, text, parent=None):
        if self.cloud:
            body = {"content": {"raw": text}}
            if parent:
                body["parent"] = {"id": int(parent)}
        else:
            body = {"text": text}
            if parent:
                body["parent"] = {"id": int(parent)}
        return self.request("POST", f"{self.prp(repo, n)}/comments", body=body)

    def pr_approve(self, repo, n, undo=False):
        return self.request("DELETE" if undo else "POST", f"{self.prp(repo, n)}/approve")

    def pr_merge(self, repo, n, strategy=None, close_branch=False, message=None):
        if self.cloud:
            body = {"close_source_branch": bool(close_branch)}
            if strategy:
                body["merge_strategy"] = strategy
            if message:
                body["message"] = message
            return self.request("POST", f"{self.prp(repo, n)}/merge", body=body)
        if strategy or close_branch:
            raise BbError("Server/DC merge uses the repository's default strategy; --strategy/--close-branch are Cloud-only")
        version = self.get(self.prp(repo, n)).get("version", 0)
        return self.request("POST", f"{self.prp(repo, n)}/merge", params={"version": version})

    def default_branch(self, repo):
        if self.cloud:
            return ((self.get(self.rp(repo)).get("mainbranch")) or {}).get("name", "main")
        return self.get(f"{self.rp(repo)}/branches/default").get("displayId", "master")

    def pr_create(self, repo, source, dest, title, description="", draft=False, close_branch=False):
        if self.cloud:
            body = {"title": title, "description": description, "source": {"branch": {"name": source}},
                    "destination": {"branch": {"name": dest}}, "close_source_branch": bool(close_branch), "draft": bool(draft)}
        else:
            owner, slug = repo.split("/", 1)
            ref = lambda b: {"id": f"refs/heads/{b}", "repository": {"slug": slug, "project": {"key": owner}}}  # noqa: E731
            body = {"title": title, "description": description, "fromRef": ref(source), "toRef": ref(dest), "reviewers": []}
        return self.norm_pr(self.request("POST", self.prp(repo, "").rstrip("/"), body=body))

    # ------------------------------------------------------------ repos, branches, commits
    def repo_info(self, repo):
        r = self.get(self.rp(repo))
        if self.cloud:
            return {"repo": r.get("full_name", repo), "private": r.get("is_private"), "default": (r.get("mainbranch") or {}).get("name", ""),
                    "language": r.get("language", ""), "updated": r.get("updated_on", ""), "description": r.get("description", ""),
                    "url": ((r.get("links") or {}).get("html") or {}).get("href", "")}
        return {"repo": repo, "private": not r.get("public"), "default": self.default_branch(repo), "language": "",
                "updated": "", "description": r.get("description", ""), "url": ((r.get("links") or {}).get("self") or [{}])[0].get("href", "")}

    def repos(self, scope, search="", limit=30):
        if self.cloud:
            params = {"sort": "-updated_on"}
            if search:
                params["q"] = f'name ~ "{search}"'
            rows = self.pages(f"/repositories/{scope}", params, limit, max_pages=2)
            return [{"repo": r.get("full_name", ""), "updated": r.get("updated_on", "")[:10]} for r in rows]
        params = {"name": search} if search else {}
        rows = self.pages(f"/projects/{scope}/repos", params, limit, max_pages=2)
        return [{"repo": f"{scope}/{r.get('slug')}", "updated": ""} for r in rows]

    def branches(self, repo, search="", limit=30):
        if self.cloud:
            params = {"sort": "-target.date"}
            if search:
                params["q"] = f'name ~ "{search}"'
            rows = self.pages(f"{self.rp(repo)}/refs/branches", params, limit, max_pages=2)
            return [{"name": r["name"], "sha": (r.get("target") or {}).get("hash", ""), "date": (r.get("target") or {}).get("date", "")[:10]} for r in rows]
        params = {"orderBy": "MODIFICATION"}
        if search:
            params["filterText"] = search
        rows = self.pages(f"{self.rp(repo)}/branches", params, limit, max_pages=2)
        return [{"name": r["displayId"], "sha": r.get("latestCommit", ""), "date": ""} for r in rows]

    def _norm_commit(self, c):
        if self.cloud:
            return {"sha": c.get("hash", ""), "author": ((c.get("author") or {}).get("user") or {}).get("display_name")
                    or (c.get("author") or {}).get("raw", ""), "date": c.get("date", ""), "message": c.get("message", "")}
        return {"sha": c.get("id", ""), "author": (c.get("author") or {}).get("name", ""), "date": _ms_iso(c.get("authorTimestamp")),
                "message": c.get("message", "")}

    def commits(self, repo, ref=None, path=None, limit=15):
        if self.cloud:
            p = f"{self.rp(repo)}/commits" + (f"/{ref}" if ref else "")
            rows = self.pages(p, {"path": path} if path else {}, limit, max_pages=2)
        else:
            params = {"until": ref} if ref else {}
            if path:
                params["path"] = path
            rows = self.pages(f"{self.rp(repo)}/commits", params, limit, max_pages=2)
        return [self._norm_commit(r) for r in rows]

    def commit(self, repo, sha):
        c = self._norm_commit(self.get(f"{self.rp(repo)}/commit{'s' if not self.cloud else ''}/{sha}"))
        if self.cloud:
            files = self.pages(f"{self.rp(repo)}/diffstat/{sha}", {}, 50, max_pages=1)
            c["files"] = [(r.get("new") or r.get("old") or {}).get("path", "") for r in files]
        else:
            rows = self.pages(f"{self.rp(repo)}/commits/{sha}/changes", {}, 50, max_pages=1)
            c["files"] = [(r.get("path") or {}).get("toString", "") for r in rows]
        return c

    def compare(self, repo, base, head, limit=30):
        """Commits in head not in base + changed files."""
        if self.cloud:
            commits = self.pages(f"{self.rp(repo)}/commits", {"include": head, "exclude": base}, limit, max_pages=1)
            files = self.pages(f"{self.rp(repo)}/diffstat/{head}..{base}", {}, 100, max_pages=1)
            fl = [{"path": (r.get("new") or r.get("old") or {}).get("path", ""), "status": r.get("status", "")} for r in files]
        else:
            commits = self.pages(f"{self.rp(repo)}/compare/commits", {"from": head, "to": base}, limit, max_pages=1)
            files = self.pages(f"{self.rp(repo)}/compare/changes", {"from": head, "to": base}, 100, max_pages=1)
            fl = [{"path": (r.get("path") or {}).get("toString", ""), "status": (r.get("type") or "").lower()} for r in files]
        return [self._norm_commit(c) for c in commits], fl

    # ------------------------------------------------------------ pipelines (Cloud) / build statuses (Server)
    def _need_cloud(self, what):
        if not self.cloud:
            raise BbError(f"{what} exists only on Bitbucket Cloud; Server/DC has build statuses (`checks`)")

    @staticmethod
    def pipe_status(p):
        st = p.get("state") or {}
        name = (st.get("name") or "").upper()
        if name == "COMPLETED":
            return STATE_MAP.get(((st.get("result") or {}).get("name") or "").lower(), "failed")
        return "running"

    def norm_pipeline(self, p):
        return {"uuid": p.get("uuid", ""), "id": p.get("build_number"), "status": self.pipe_status(p),
                "ref": (p.get("target") or {}).get("ref_name", ""), "sha": ((p.get("target") or {}).get("commit") or {}).get("hash", ""),
                "created": p.get("created_on", ""), "done": p.get("completed_on", ""), "seconds": p.get("duration_in_seconds"),
                "author": (p.get("creator") or {}).get("display_name", "")}

    def pipelines(self, repo, ref=None, limit=15):
        self._need_cloud("pipelines")
        params = {"sort": "-created_on"}
        if ref:
            params["target.branch"] = ref
        return [self.norm_pipeline(p) for p in self.pages(f"{self.rp(repo)}/pipelines/", params, limit, max_pages=2)]

    def pipeline(self, repo, ident):
        """By build number or uuid. A build number is tried directly, then resolved from the newest 100 pipelines."""
        self._need_cloud("pipelines")
        key = str(ident)
        try:
            return self.norm_pipeline(self.get(f"{self.rp(repo)}/pipelines/{key}"))
        except BbError as exc:
            if exc.status != 404 or not key.isdigit():
                raise
        for p in self.pipelines(repo, limit=100):
            if str(p["id"]) == key:
                return p
        raise BbError(f"pipeline #{key} not found in {repo} (searched the newest 100): check the build number", 404)

    def pipeline_steps(self, repo, uuid):
        self._need_cloud("pipelines")
        rows = self.pages(f"{self.rp(repo)}/pipelines/{uuid}/steps/", {}, 50, max_pages=1)
        return [{"uuid": s.get("uuid", ""), "name": s.get("name", ""), "status": self.pipe_status(s), "seconds": s.get("duration_in_seconds")}
                for s in rows]

    def step_log(self, repo, pipeline_uuid, step_uuid, tail_bytes=262144):
        self._need_cloud("pipelines")
        return self.text(f"{self.rp(repo)}/pipelines/{pipeline_uuid}/steps/{step_uuid}/log",
                         headers={"Range": f"bytes=-{tail_bytes}", "Accept": "application/octet-stream"})

    def ref_checks(self, repo, ref):
        """Latest build state of a branch/commit: Cloud newest pipeline; Server build statuses of the commit."""
        if self.cloud:
            rows = self.pipelines(repo, ref=ref, limit=1)
            if not rows:
                return "none", []
            p = rows[0]
            return p["status"], [{"name": f"pipeline #{p['id']}", "state": p["status"], "url": ""}]
        rows = self.commits(repo, ref=ref, limit=1)
        sha = rows[0]["sha"] if rows else ref
        checks = self.commit_checks(repo, sha)
        return checks_summary(checks), checks
