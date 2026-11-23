"""Thin Artifactory REST client (requests). Only talks to the instance's own host; the token is never printed."""
from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import quote

import requests

from afconfig import Instance

TIMEOUT = 60


class ApiError(RuntimeError):
    pass


def clean_path(path: str) -> str:
    """`repo/dir/file` with no scheme, query, `..` or control characters (the path is URL-quoted by `url`)."""
    p = (path or "").strip().strip("/")
    if not p or "\\" in p or "?" in p or "#" in p or any(ord(c) < 32 for c in p) or ".." in p.split("/"):
        raise ApiError(f"invalid repo path {path!r}: use 'repo/dir/file' (no '..', '?', '#' or backslashes)")
    if "://" in p:
        raise ApiError(f"invalid repo path {path!r}: pass 'repo/dir/file', not a URL")
    return p


def sha256_of(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Client:
    def __init__(self, inst: Instance):
        self.inst = inst

    def url(self, path: str) -> str:
        return f"{self.inst.api}/{quote(path.lstrip('/'), safe='/:=,&?@*+%')}"

    def headers(self, extra=None) -> dict:
        h = {"Authorization": f"Bearer {self.inst.token}"} if self.inst.token else {}
        h.update(extra or {})
        return h

    def request(self, method: str, path: str, **kw) -> requests.Response:
        timeout = kw.pop("timeout", TIMEOUT)
        headers = self.headers(kw.pop("headers", None))
        try:
            return requests.request(method, self.url(path), headers=headers, timeout=timeout, **kw)
        except requests.RequestException as exc:
            raise ApiError(f"{self.inst.base_url} unreachable ({type(exc).__name__}): check base_url / VPN") from None

    def get_json(self, path: str, **kw):
        r = self.request("GET", path, **kw)
        if r.status_code != 200:
            raise ApiError(f"HTTP {r.status_code} for {path.split('?')[0]}: {r.text[:200]}")
        try:
            return r.json()
        except ValueError:
            raise ApiError(f"non-JSON answer for {path.split('?')[0]}") from None

    def storage(self, repo_path: str) -> requests.Response:
        return self.request("GET", f"api/storage/{clean_path(repo_path)}")

    def ping(self) -> requests.Response:
        return self.request("GET", "api/system/ping", timeout=20)

    def list_files(self, repo_path: str, depth: int = 1, cap: int = 5000) -> list[dict]:
        """Files below a folder (`?list&deep=1`); [{uri,size,lastModified,sha1}]. Works anonymously where reads do."""
        p = clean_path(repo_path)
        d = self.get_json(f"api/storage/{p}?list&deep=1&depth={int(depth)}&listFolders=0&includeRootPath=0",
                          timeout=120)
        return (d.get("files") or [])[:cap]

    def token_info(self, token_id: str | None = None) -> dict | None:
        """Own token record from the Access API (None when anonymous / not available)."""
        if not self.inst.token:
            return None
        r = requests.get(f"{self.inst.base_url}/access/api/v1/tokens", headers=self.headers(), timeout=20)
        if r.status_code != 200:
            return {"valid": False, "http": r.status_code}
        toks = r.json().get("tokens", [])
        if token_id:
            toks = [t for t in toks if t.get("token_id") == token_id] or toks
        return dict(max(toks, key=lambda t: t.get("issued_at", 0)), valid=True) if toks else {"valid": True}
