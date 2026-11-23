"""Minimal Jenkins REST client + path/log helpers shared by the CLI and the watcher script.

Jobs are addressed by a slash path (`folder/sub/job`) or a Jenkins URL under base_url; every segment is validated
and quoted, so user input can never escape the `/job/<x>/` structure. Nothing here prints the token; error texts
go through `redact()`.
"""
from __future__ import annotations

import re
import urllib.parse

SECRET_RE = re.compile(r"(token|password|passwd|secret|authorization|api[_-]?key)(\s*[=:]\s*)\S+", re.I)
BEARER_RE = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}", re.I)
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\(B")
NOTE_RE = re.compile(r"\x1b\[8m.*?\x1b\[0m", re.S)  # Jenkins console annotations (hidden ha:////... payload)
SEGMENT_RE = re.compile(r"^[^/\\?#%\x00-\x1f]+$")
BUILD_SELECTORS = {"lastBuild", "lastCompletedBuild", "lastSuccessfulBuild", "lastFailedBuild", "lastStableBuild",
                   "lastUnstableBuild", "lastUnsuccessfulBuild"}
ALIASES = {"last": "lastBuild", "completed": "lastCompletedBuild", "success": "lastSuccessfulBuild",
           "failed": "lastFailedBuild", "stable": "lastStableBuild", "unstable": "lastUnstableBuild"}


class JenkinsError(RuntimeError):
    pass


def redact(text: str) -> str:
    return SECRET_RE.sub(r"\1\2***", BEARER_RE.sub(r"\1 ***", str(text)))


def clean_log(text: str) -> str:
    return ANSI_RE.sub("", NOTE_RE.sub("", text)).replace("\r", "\n")


def norm_path(raw: str, base_url: str = "") -> str:
    """'a/b/c', '/job/a/job/b/', or '<base_url>/job/a/job/b/12/' -> 'a/b' style slash path (no build number)."""
    s = (raw or "").strip()
    if base_url and s.startswith(base_url):
        s = s[len(base_url):]
    s = s.strip("/")
    if s.startswith("job/"):
        parts, names = s.split("/"), []
        while len(parts) >= 2 and parts[0] == "job":  # trailing build number / `console` is dropped
            names.append(parts[1])
            parts = parts[2:]
        s = "/".join(names)
    # %2F (branch names in multibranch jobs) stays encoded; everything else is decoded and re-validated
    segs = [urllib.parse.unquote(x.replace("%2F", "%252F").replace("%2f", "%252F")) for x in s.split("/")] if s else []
    for seg in segs:
        if not SEGMENT_RE.match(seg.replace("%2F", "")) or seg in (".", ".."):
            raise JenkinsError(f"invalid job path segment {seg!r}")
    return "/".join(segs)


def job_url_path(path: str) -> str:
    """'a/b' -> '/job/a/job/b' ('' -> '' = root)."""
    return "".join("/job/" + urllib.parse.quote(s, safe="%") for s in norm_path(path).split("/") if s)


def build_ref(sel) -> str:
    s = str(sel if sel not in (None, "") else "lastBuild")
    s = ALIASES.get(s, s)
    if s.isdigit() or s in BUILD_SELECTORS:
        return s
    raise JenkinsError(f"build must be a number or one of last|completed|success|failed|stable|unstable, got {s!r}")


class Client:
    def __init__(self, base_url, user, token, session=None, timeout=30):
        import requests
        self.base = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.auth = (user, token)
        self.timeout = timeout

    def _url(self, path):
        return self.base + path

    def _check(self, resp, what):
        if resp.status_code >= 400:
            raise JenkinsError(redact(f"{resp.status_code} {what}: {(resp.text or '')[:200]}"))
        return resp

    def get(self, path, params=None):
        try:
            r = self.session.get(self._url(path), params=params, timeout=self.timeout)
        except Exception as exc:
            raise JenkinsError(f"{type(exc).__name__} reaching Jenkins (check base_url / VPN)") from None
        return self._check(r, f"GET {path}")

    def json(self, path, tree=None, **params):
        if tree:
            params["tree"] = tree
        return self.get(path.rstrip("/") + "/api/json", params).json()

    def post(self, path, params=None, data=None):
        headers = {}
        try:  # CSRF crumb is optional (absent when CSRF protection is off); a failed lookup just means no header
            c = self.session.get(self._url("/crumbIssuer/api/json"), timeout=self.timeout)
            if c.ok:
                j = c.json()
                headers[j["crumbRequestField"]] = j["crumb"]
        except Exception:
            pass
        try:
            r = self.session.post(self._url(path), params=params, data=data, headers=headers,
                                  timeout=self.timeout, allow_redirects=False)
        except Exception as exc:
            raise JenkinsError(f"{type(exc).__name__} reaching Jenkins") from None
        if r.status_code >= 400:
            raise JenkinsError(redact(f"{r.status_code} POST {path}: {(r.text or '')[:200]}"))
        return r

    def log_lines(self, path, tail=60, grep=None, max_bytes=20_000_000):
        """Stream /consoleText and keep only the last `tail` (matching) lines; bounded by max_bytes."""
        from collections import deque
        rx = re.compile(grep, re.I) if grep else None
        keep, read, truncated = deque(maxlen=tail), 0, False
        try:
            r = self.session.get(self._url(path + "/consoleText"), stream=True, timeout=self.timeout)
        except Exception as exc:
            raise JenkinsError(f"{type(exc).__name__} reaching Jenkins") from None
        self._check(r, f"GET {path}/consoleText")
        buf = ""
        for chunk in r.iter_content(chunk_size=65536, decode_unicode=False):
            read += len(chunk)
            buf += chunk.decode("utf-8", "replace")
            *lines, buf = buf.split("\n")
            for ln in lines:
                self._keep(keep, ln, rx)
            if read >= max_bytes:
                truncated = True
                break
        r.close()
        if buf:
            self._keep(keep, buf, rx)
        return list(keep), truncated

    @staticmethod
    def _keep(keep, ln, rx):
        ln = clean_log(ln)
        for part in ln.split("\n"):
            part = redact(part.rstrip())
            if part and (rx is None or rx.search(part)):
                keep.append(part[:400])


FOLDER_HINTS = ("Folder", "OrganizationFolder", "WorkflowMultiBranchProject", "MultiBranchProject")
JOB_TREE = "name,fullName,url,color,_class,lastBuild[number,result,building,timestamp],lastCompletedBuild[number,result,timestamp,duration,url]"


def is_folder(job) -> bool:
    return any(h in (job.get("_class") or "") for h in FOLDER_HINTS)


def walk_jobs(client, folder="", max_jobs=200, max_requests=25, depth=3):
    """Breadth-first list of non-folder jobs under `folder` (bounded). Returns (jobs, truncated)."""
    out, queue, reqs, truncated = [], [(norm_path(folder), 0)], 0, False
    while queue:
        path, d = queue.pop(0)
        if reqs >= max_requests:
            truncated = True
            break
        reqs += 1
        jobs = client.json(job_url_path(path), tree=f"jobs[{JOB_TREE}]").get("jobs") or []
        for j in jobs:
            full = j.get("fullName") or "/".join(x for x in (path, j.get("name", "")) if x)
            j["fullName"] = full
            if is_folder(j):
                if d + 1 <= depth:
                    queue.append((full, d + 1))
                else:
                    truncated = True
            else:
                out.append(j)
        if len(out) >= max_jobs:
            return out[:max_jobs], True
    return out, truncated
