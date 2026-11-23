"""Minimal GitHub REST + GraphQL client shared by the CLI and the watcher script (requests only).

Errors are `GhError("GitHub API error <status> <METHOD> <path>: <message>")` so callers can match the status.
The token only ever goes into the Authorization header; it is never part of an error message.
"""
from __future__ import annotations

from urllib.parse import urlsplit

import requests

TIMEOUT = 30


class GhError(RuntimeError):
    def __init__(self, msg, status=0):
        super().__init__(msg)
        self.status = status


class Client:
    def __init__(self, rest_url, token, graphql_url=None, session=None):
        self.api = rest_url.rstrip("/")
        self.graphql_url = graphql_url or self.api + "/graphql"
        self.session = session or requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "dop-agent-github"})

    def _url(self, path):
        if not path.startswith("http"):
            return f"{self.api}{path}"
        # absolute URLs (paging links) must stay on the configured API host so the PAT cannot be sent elsewhere
        u, hosts = urlsplit(path), {urlsplit(self.api).netloc.lower(), urlsplit(self.graphql_url).netloc.lower()}
        if u.scheme != "https" or u.netloc.lower() not in hosts:
            raise GhError("refusing to send credentials to a non-https or foreign host from an absolute URL")
        return path

    @staticmethod
    def _fail(resp, method, path):
        try:
            msg = resp.json().get("message", "")
        except (ValueError, AttributeError):
            msg = resp.text
        raise GhError(f"GitHub API error {resp.status_code} {method} {path}: {str(msg)[:300]}", resp.status_code)

    def get(self, path, params=None):
        resp = self.session.get(self._url(path), params=params, timeout=TIMEOUT)
        if not resp.ok:
            self._fail(resp, "GET", path.split("?")[0])
        return resp

    def json(self, path, params=None):
        return self.get(path, params).json()

    def get_all(self, path, params=None, max_pages=5, key=None):
        """Follow Link: rel=next up to max_pages; `key` unwraps {"<key>": [...]} envelopes (search, check-runs)."""
        out, url, p = [], path, dict(params or {}, per_page=100)
        for _ in range(max_pages):
            resp = self.get(url, p)
            data = resp.json()
            out += (data.get(key, []) if key else data)
            url = resp.links.get("next", {}).get("url")
            if not url:
                break
            p = None
        return out

    def request(self, method, path, params=None, body=None):
        resp = self.session.request(method, self._url(path), params=params, json=body, timeout=TIMEOUT)
        if not resp.ok:
            self._fail(resp, method, path)
        if resp.status_code == 204 or not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return resp.text

    def graphql(self, query, variables=None):
        resp = self.session.post(self.graphql_url, json={"query": query, "variables": variables or {}}, timeout=TIMEOUT)
        if not resp.ok:
            self._fail(resp, "POST", "/graphql")
        data = resp.json()
        if data.get("errors"):
            raise GhError("GitHub GraphQL error: " + "; ".join(e.get("message", "?") for e in data["errors"])[:300])
        return data["data"]


_OK_CONCLUSIONS = {"success", "neutral", "skipped"}


def checks_state(client, repo, sha):
    """Combined CI state of a commit from check runs + commit statuses.

    Returns (state, items): state is failure | pending | success | none; items are (name, state, url) with
    state failure | pending | success."""
    items = []
    runs = client.get_all(f"/repos/{repo}/commits/{sha}/check-runs", {"filter": "latest"}, max_pages=3, key="check_runs")
    for r in runs:
        if r.get("status") != "completed":
            st = "pending"
        else:
            st = "success" if r.get("conclusion") in _OK_CONCLUSIONS else "failure"
        items.append((r.get("name") or "?", st, r.get("html_url") or ""))
    combined = client.json(f"/repos/{repo}/commits/{sha}/status")
    for s in combined.get("statuses") or []:
        st = {"success": "success", "pending": "pending"}.get(s.get("state"), "failure")
        items.append((s.get("context") or "?", st, s.get("target_url") or ""))
    kinds = {st for _, st, _ in items}
    state = "failure" if "failure" in kinds else "pending" if "pending" in kinds else "success" if kinds else "none"
    return state, items
