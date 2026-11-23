"""Thin JSON:API client for the HCP Terraform / Terraform Enterprise API v2 (requests).

Talks only to the instance's own host; the token goes in the Authorization header and is never printed (errors are
redacted). Reads are plain GETs; the few writes (create run, apply, discard, cancel) are POSTs the CLI only issues with
an explicit --yes. Every list is bounded (page size and max pages).
"""
from __future__ import annotations

import re

import requests

from tfconfig import Instance, redact

TIMEOUT = 30
_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
_WS_RE = re.compile(r"^[A-Za-z0-9_\-.]{1,90}$")
TERMINAL = {"applied", "planned_and_finished", "planned_and_saved", "discarded", "errored", "canceled",
            "force_canceled"}
ATTENTION = {"planned", "cost_estimated", "policy_checked", "policy_override", "post_plan_completed"}


class ApiError(RuntimeError):
    pass


def check_id(value: str, prefix: str = "") -> str:
    v = (value or "").strip()
    if not _ID_RE.match(v) or (prefix and not v.startswith(prefix)):
        raise ApiError(f"invalid id {value!r}" + (f" (expected {prefix}...)" if prefix else ""))
    return v


def check_ws(value: str) -> str:
    v = (value or "").strip()
    if not _WS_RE.match(v):
        raise ApiError(f"invalid workspace name {value!r}")
    return v


def attr(obj: dict, name: str, default=None):
    return ((obj or {}).get("attributes") or {}).get(name, default)


def rel_id(obj: dict, name: str):
    return ((((obj or {}).get("relationships") or {}).get(name) or {}).get("data") or {}).get("id")


class Client:
    def __init__(self, inst: Instance):
        self.inst = inst

    @property
    def api(self) -> str:
        return f"{self.inst.base_url}/api/v2"

    def request(self, method: str, path: str, params: dict | None = None, body: dict | None = None):
        headers = {"Authorization": f"Bearer {self.inst.token}", "Content-Type": "application/vnd.api+json"}
        try:
            r = requests.request(method, f"{self.api}/{path.lstrip('/')}", headers=headers, params=params,
                                 json=body, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise ApiError(f"{self.inst.base_url} unreachable ({type(exc).__name__}): check base_url / VPN") from None
        return r

    def _check(self, r, path: str):
        if r.status_code in (200, 201, 202, 204):
            return
        hint = {401: " (token invalid/expired: check auth.token)", 403: " (token lacks permission)",
                404: " (not found or no access: check names/organization)", 429: " (rate limited)"}.get(r.status_code, "")
        raise ApiError(f"HTTP {r.status_code} for {path.split('?')[0]}{hint}: {redact(r.text[:200], self.inst.token)}")

    def get(self, path: str, params: dict | None = None) -> dict:
        r = self.request("GET", path, params)
        self._check(r, path)
        try:
            return r.json()
        except ValueError:
            raise ApiError(f"non-JSON answer for {path.split('?')[0]}") from None

    def get_or_none(self, path: str, params: dict | None = None):
        r = self.request("GET", path, params)
        if r.status_code == 404:
            return None
        self._check(r, path)
        return r.json()

    def paginate(self, path: str, params: dict | None = None, size: int = 100, max_pages: int = 5):
        """-> (data, included) over at most `max_pages` pages."""
        data, inc = [], []
        for page in range(1, max_pages + 1):
            p = dict(params or {})
            p.update({"page[size]": size, "page[number]": page})
            d = self.get(path, p)
            data += d.get("data") or []
            inc += d.get("included") or []
            if not ((d.get("meta") or {}).get("pagination") or {}).get("next-page"):
                break
        return data, inc

    def post(self, path: str, body: dict | None = None) -> dict:
        r = self.request("POST", path, body=body)
        self._check(r, path)
        try:
            return r.json()
        except ValueError:
            return {}

    def workspace(self, name: str) -> dict:
        org = check_ws(self.inst.organization)
        return self.get(f"organizations/{org}/workspaces/{check_ws(name)}")["data"]

    def run(self, run_id: str) -> dict:
        return self.get(f"runs/{check_id(run_id, 'run-')}")["data"]

    def plan_summary(self, run: dict) -> dict:
        pid = rel_id(run, "plan")
        if not pid:
            return {}
        p = self.get(f"plans/{pid}")["data"]
        return {"add": attr(p, "resource-additions", 0), "change": attr(p, "resource-changes", 0),
                "destroy": attr(p, "resource-destructions", 0), "import": attr(p, "resource-imports", 0),
                "status": attr(p, "status")}

    def run_url(self, ws_name: str, run_id: str) -> str:
        return f"{self.inst.base_url}/app/{self.inst.organization}/workspaces/{ws_name}/runs/{run_id}"


def workspaces_with_runs(c: Client, search: str = "", tag: str = "", max_pages: int = 3):
    """-> [(workspace, current_run|None)] for the instance's organization; one request per page (include=current_run)."""
    params = {"include": "current_run"}
    if search:
        params["search[name]"] = search
    if tag:
        params["search[tags]"] = tag
    org = check_ws(c.inst.organization)
    data, inc = c.paginate(f"organizations/{org}/workspaces", params, max_pages=max_pages)
    runs = {i["id"]: i for i in inc if i.get("type") == "runs"}
    return [(w, runs.get(rel_id(w, "current-run"))) for w in data]
