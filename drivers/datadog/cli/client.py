"""Small Datadog REST client (stdlib only) used by the CLI and the watcher. Keys go only into request headers and are
redacted from every error text. All reads are GET (or the read-only POST logs search); writes are limited to downtimes.

    Client(inst).request(method, path, params=None, body=None) -> parsed JSON (dict/list)
    parse_time(s) -> epoch seconds   ("now", "15m", "2h", "7d", epoch or ISO-8601)
    monitor_states(c, tags, name, query, limit) -> [{id, name, state, tags}]   normalized (OK/Alert/Warn/No Data/...)
    search_logs(c, query, since, limit) -> [{ts, service, host, status, message}]
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

TIMEOUT = 30
MAX_BYTES = 8_000_000
_REDACT = re.compile(r"(?i)(api[_-]?key|app(lication)?[_-]?key|token|password|secret|authorization)([=:\s\"']+)[^\s,\"'&]+")


class ApiError(RuntimeError):
    pass


def redact(text: str, *secrets: str) -> str:
    for s in secrets:
        if s and len(s) >= 6:
            text = text.replace(s, "***")
    return _REDACT.sub(lambda m: m.group(1) + m.group(3) + "***", text)


def parse_time(value: str | int | float, now: float | None = None) -> int:
    now = time.time() if now is None else now
    s = str(value).strip().lower()
    if s in ("", "now"):
        return int(now)
    m = re.fullmatch(r"(\d+)\s*([smhdw])", s)
    if m:
        return int(now - int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)])
    if re.fullmatch(r"\d{9,13}", s):
        n = int(s)
        return n // 1000 if n > 10**11 else n
    try:
        return int(datetime.fromisoformat(value.strip().replace("Z", "+00:00")).timestamp())
    except ValueError:
        raise ApiError(f"bad time {value!r}: use now, 15m, 2h, 7d, epoch seconds or ISO-8601") from None


class Client:
    def __init__(self, inst):
        self.inst = inst

    def request(self, method: str, path: str, params: dict | None = None, body=None):
        url = f"{self.inst.api}/{path.lstrip('/')}"
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "DD-API-KEY": self.inst.api_key, "DD-APPLICATION-KEY": self.inst.app_key,
            "Accept": "application/json", "Content-Type": "application/json", "User-Agent": "dop-agent-datadog"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                raw = r.read(MAX_BYTES + 1)
        except urllib.error.HTTPError as exc:
            detail = redact(exc.read(500).decode("utf-8", "replace"), self.inst.api_key, self.inst.app_key)
            raise ApiError(f"HTTP {exc.code} on {method} /{path.lstrip('/').split('?')[0]}: {detail}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise ApiError(redact(f"network error on /{path.lstrip('/').split('?')[0]}: {exc}",
                                  self.inst.api_key, self.inst.app_key)) from None
        if len(raw) > MAX_BYTES:
            raise ApiError("response too large; narrow the query")
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            raise ApiError("non-JSON response") from None

    def get(self, path, **params):
        return self.request("GET", path, params)


def _norm_state(s: str) -> str:
    s = (s or "").strip()
    return {"ok": "OK", "alert": "Alert", "warn": "Warn", "no data": "No Data", "skipped": "Skipped",
            "ignored": "Ignored", "unknown": "Unknown"}.get(s.lower(), s or "Unknown")


def monitor_states(c: Client, tags: str = "", name: str = "", query: str = "", limit: int = 200) -> list[dict]:
    limit = max(1, min(int(limit), 1000))
    if query:
        d = c.get("api/v1/monitor/search", query=query, per_page=min(limit, 100), page=0)
        rows = [{"id": m.get("id"), "name": m.get("name", ""), "state": _norm_state(m.get("status", "")),
                 "tags": m.get("tags") or []} for m in (d.get("monitors") or [])]
    else:
        d = c.get("api/v1/monitor", monitor_tags=tags, name=name, page_size=min(limit, 1000), page=0)
        rows = [{"id": m.get("id"), "name": m.get("name", ""), "state": _norm_state(m.get("overall_state", "")),
                 "tags": m.get("tags") or []} for m in (d if isinstance(d, list) else [])]
    return rows[:limit]


def search_logs(c: Client, query: str, since: str = "15m", limit: int = 100) -> list[dict]:
    limit = max(1, min(int(limit), 1000))
    body = {"filter": {"query": query, "from": parse_time(since) * 1000, "to": int(time.time() * 1000)},
            "sort": "-timestamp", "page": {"limit": limit}}
    d = c.request("POST", "api/v2/logs/events/search", body=body)
    out = []
    for e in (d.get("data") or []):
        a = e.get("attributes") or {}
        out.append({"ts": a.get("timestamp", ""), "service": a.get("service", ""), "host": a.get("host", ""),
                    "status": a.get("status", ""), "message": str(a.get("message", ""))})
    return out
