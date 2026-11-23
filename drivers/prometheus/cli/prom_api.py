"""Thin Prometheus / Alertmanager HTTP client shared by the CLI, `fetch`/`test` and the watcher script.

    client(instance_name=None) -> Client
    Client.prom(path, params=None)          GET <prometheus_url>/api/v1/... -> `data` of the Prometheus envelope
    Client.am(method, path, params, body)   <alertmanager_url>/api/v2/...  (ApiError when alertmanager_url is unset)

Errors are `ApiError(status, message)`; `status` is 0 for network failures. Secrets only ever live in the request's
auth/Authorization header; error messages never contain them. Range queries are bounded (MAX_RANGE_S, MAX_POINTS).
"""
from __future__ import annotations

import json
import math
import re
import time
from urllib.parse import quote

import prom_config as pconfig

TIMEOUT_S = 25
MAX_RANGE_S = 7 * 86400
MAX_POINTS = 500
_SECRET_RE = re.compile(r"(?i)(token|password|secret|authorization)([=:]\s*)\S+")


def redact(text: str) -> str:
    return _SECRET_RE.sub(r"\1\2***", str(text))


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(redact(f"HTTP {status}: {message}" if status else message))
        self.status = status

    @property
    def transient(self) -> bool:
        return self.status in (0, 429, 500, 502, 503, 504)


class Client:
    def __init__(self, inst: pconfig.Instance):
        self.inst = inst

    def _request(self, base: str, method: str, path: str, params=None, body=None):
        import requests
        kw = {}
        headers = {"Accept": "application/json"}
        if self.inst.auth_type == "bearer":
            headers["Authorization"] = f"Bearer {self.inst.secret}"
        elif self.inst.auth_type == "basic":
            kw["auth"] = (self.inst.username, self.inst.secret)
        try:
            r = requests.request(method, base + path, headers=headers, params=params, json=body,
                                 timeout=TIMEOUT_S, **kw)
        except requests.RequestException as exc:
            raise ApiError(0, f"{base} unreachable ({type(exc).__name__}): check the URL / VPN") from None
        if not r.ok:
            try:
                j = r.json()
                msg = j.get("error") or j.get("message") or r.text if isinstance(j, dict) else r.text
            except ValueError:
                msg = r.text
            raise ApiError(r.status_code, " ".join(str(msg).split())[:300])
        try:
            return r.json()
        except ValueError:
            raise ApiError(r.status_code, "response is not JSON (is the URL the API root?)") from None

    def prom(self, path: str, params: dict | None = None):
        doc = self._request(self.inst.prometheus_url, "GET", path, params)
        if isinstance(doc, dict) and doc.get("status") == "error":
            raise ApiError(400, f"{doc.get('errorType', 'error')}: {doc.get('error', '')}")
        return doc.get("data") if isinstance(doc, dict) and "data" in doc else doc

    def am(self, method: str, path: str, params: dict | None = None, body=None):
        if not self.inst.alertmanager_url:
            raise ApiError(0, f"instance {self.inst.name!r} has no alertmanager_url option")
        return self._request(self.inst.alertmanager_url, method, path, params, body)


def client(instance: str | None = None) -> Client:
    return Client(pconfig.load_instance(instance))


def dumps(obj, limit: int = 0) -> str:
    s = json.dumps(obj, indent=2, ensure_ascii=False)
    return s if not limit or len(s) <= limit else s[:limit] + f"\n… [{len(s) - limit} more chars; narrow the query]"


def parse_duration(s: str) -> int:
    """'90s' / '30m' / '6h' / '2d' / '1w' -> seconds."""
    m = re.fullmatch(r"(\d{1,6})([smhdw])", str(s or ""))
    if not m:
        raise ValueError(f"bad duration {s!r} (use e.g. 30s, 15m, 6h, 2d)")
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]


def plan_range(range_s: int, step: str | None) -> tuple[int, int]:
    """Validate the window and pick a step so that at most MAX_POINTS points are requested."""
    if range_s <= 0 or range_s > MAX_RANGE_S:
        raise ValueError(f"range must be between 1s and {MAX_RANGE_S // 86400}d")
    min_step = max(1, math.ceil(range_s / MAX_POINTS))
    if step in (None, "", "auto"):
        return range_s, min_step
    st = parse_duration(step)
    if st < min_step:
        raise ValueError(f"step {step} too small for this range (max {MAX_POINTS} points): use at least {min_step}s")
    return range_s, st


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _fmt(f):
    return f if f is None else float(f"{f:.6g}")


def _label_str(metric: dict) -> str:
    return ",".join(f"{k}={v}" for k, v in sorted(metric.items()))[:200]


def summarize_result(data: dict, limit: int = 20) -> dict:
    """Compact view of a query/query_range `data`: per-series labels + last/min/max/avg instead of raw samples."""
    rtype, result = data.get("resultType"), data.get("result")
    if rtype in ("scalar", "string"):
        return {"type": rtype, "value": result[1] if result else None}
    series = []
    for s in result or []:
        item = {"labels": s.get("metric") or {}}
        if "values" in s:
            vals = [x for x in (_num(v[1]) for v in s["values"]) if x is not None]
            item["points"] = len(s["values"])
            if vals:
                item.update(last=_fmt(vals[-1]), min=_fmt(min(vals)), max=_fmt(max(vals)), avg=_fmt(sum(vals) / len(vals)))
        else:
            item["value"] = _fmt(_num(s["value"][1])) if _num(s["value"][1]) is not None else s["value"][1]
        series.append(item)
    out = {"type": rtype, "series_total": len(series), "series": series[:limit]}
    if len(series) > limit:
        out["truncated"] = f"{len(series) - limit} more series; narrow the query (sum by(...), topk)"
    return out


def instant_series(c: Client, expr: str) -> list[tuple[str, dict, float]]:
    """Instant query -> [(series key, labels, value)] with finite values only (for the threshold watcher)."""
    data = c.prom("/api/v1/query", {"query": expr})
    if data.get("resultType") == "scalar":
        v = _num(data["result"][1])
        return [("scalar", {}, v)] if v is not None else []
    out = []
    for s in data.get("result") or []:
        v = _num(s["value"][1])
        if v is not None:
            out.append((_label_str(s.get("metric") or {}) or "{}", s.get("metric") or {}, v))
    return out


def targets(c: Client, health: str = "all", job: str = "", limit: int = 200) -> list[dict]:
    data = c.prom("/api/v1/targets", {"state": "active"})
    out = []
    for t in data.get("activeTargets") or []:
        h = str(t.get("health") or "unknown")
        lab = t.get("labels") or {}
        if health != "all" and h != health:
            continue
        if job and lab.get("job") != job:
            continue
        out.append({"job": lab.get("job", ""), "instance": lab.get("instance", ""), "health": h,
                    "last_error": " ".join(str(t.get("lastError") or "").split())[:200],
                    "last_scrape": t.get("lastScrape", ""), "scrape_url": t.get("scrapeUrl", ""),
                    "labels": {k: v for k, v in lab.items() if k not in ("job", "instance")}})
        if len(out) >= limit:
            break
    return out


def alerts_prom(c: Client, states=("firing",), limit: int = 200) -> list[dict]:
    """Active alert instances reported by Prometheus (/api/v1/alerts)."""
    out = []
    for a in (c.prom("/api/v1/alerts").get("alerts") or []):
        state = str(a.get("state") or "")
        if "all" not in states and state not in states:
            continue
        labels = a.get("labels") or {}
        out.append({"alert": labels.get("alertname", ""), "state": state, "labels": labels,
                    "since": a.get("activeAt", ""), "value": a.get("value", ""),
                    "summary": " ".join(str((a.get("annotations") or {}).get("summary", "")).split())[:200],
                    "fp": _label_str(labels)})
        if len(out) >= limit:
            break
    return out


def alerts_am(c: Client, active: bool = True, silenced: bool = False, inhibited: bool = False, limit: int = 200) -> list[dict]:
    rows = c.am("GET", "/api/v2/alerts", {"active": str(active).lower(), "silenced": str(silenced).lower(),
                                           "inhibited": str(inhibited).lower()})
    out = []
    for a in rows or []:
        labels = a.get("labels") or {}
        out.append({"alert": labels.get("alertname", ""), "state": (a.get("status") or {}).get("state", ""),
                    "labels": labels, "since": a.get("startsAt", ""),
                    "silenced_by": (a.get("status") or {}).get("silencedBy") or [],
                    "summary": " ".join(str((a.get("annotations") or {}).get("summary", "")).split())[:200],
                    "fp": _label_str(labels)})
        if len(out) >= limit:
            break
    return out


def label_filter(rows: list[dict], pairs: list[str]) -> list[dict]:
    want = dict(kv.split("=", 1) for kv in pairs)
    return [r for r in rows if all(r["labels"].get(k) == v for k, v in want.items())]


def now() -> int:
    return int(time.time())
