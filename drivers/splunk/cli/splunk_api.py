"""Thin Splunk REST client shared by the CLI, `test` and the watcher script (read-only by design).

    client(instance_name=None) -> Client
    Client.request(method, path, params=None, form=None) -> parsed JSON
    run_search(client, spl, earliest, latest, count, timeout) -> {sid, state, done, total, rows, note}

Safety: `check_spl` refuses SPL commands that write or execute (delete, outputlookup, collect, sendemail, script,
run, ...); the only POSTs are creating a search job and cancelling a job. Searches are always bounded by a time
window and a result cap (instance option `max_results`). Tokens only live in the Authorization header; error
messages and output go through `redact`.
"""
from __future__ import annotations

import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import splunk_config as sconfig

TIMEOUT_S = 25
HARD_MAX_RESULTS = 1000
_SECRET_RE = re.compile(r"(?i)\b(token|password|secret|authorization|bearer)([=:\s]+)(?:bearer\s+)?\S+")
_FORBIDDEN = ("delete", "outputlookup", "outputcsv", "collect", "mcollect", "meventcollect", "sendemail", "sendalert",
              "script", "run", "dbxquery", "tscollect", "outputtext")
_FORBIDDEN_RE = re.compile(r"(?i)(?:^|[|\[])\s*(" + "|".join(_FORBIDDEN) + r")\b")
_ID_RE = re.compile(r"[A-Za-z0-9_.\-+]{1,200}")


def redact(text: object) -> str:
    return _SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", str(text))


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(redact(f"HTTP {status}: {message}" if status else message))
        self.status = status

    @property
    def transient(self) -> bool:
        return self.status in (0, 429, 500, 502, 503, 504)


def join_time_args(argv: list[str]) -> list[str]:
    """argparse takes `--earliest -15m` for a flag; rewrite to `--earliest=-15m` (relative Splunk times start with -)."""
    out, i = [], 0
    while i < len(argv):
        if argv[i] in ("--earliest", "--latest") and i + 1 < len(argv):
            out.append(f"{argv[i]}={argv[i + 1]}")
            i += 2
        else:
            out.append(argv[i])
            i += 1
    return out


def lim(n: int, top: int = 200) -> int:
    """Clamp list limits: Splunk treats count=0 as 'everything'."""
    return max(1, min(int(n), top))


def q(value: str) -> str:
    return urllib.parse.quote(str(value), safe="")


def check_id(value: str, what: str = "id") -> str:
    if not _ID_RE.fullmatch(value or ""):
        raise ValueError(f"invalid {what}: {value!r}")
    return value


def check_spl(spl: str) -> str:
    spl = (spl or "").strip()
    if not spl:
        raise ValueError("empty SPL query")
    bad = _FORBIDDEN_RE.search(spl)
    if bad:
        raise ValueError(f"SPL command '{bad.group(1)}' is not allowed (read-only driver)")
    return spl


def build_spl(spl: str, index: str = "") -> str:
    """Prefix `search ` (required by the REST API) and the default index when the query names none."""
    spl = check_spl(spl)
    if spl.startswith("|"):
        return spl
    if not re.match(r"(?i)search\s", spl):
        if index and not re.search(r"(?i)\bindex\s*=", spl):
            spl = f"index={index} {spl}"
        spl = "search " + spl
    elif index and not re.search(r"(?i)\bindex\s*=", spl):
        spl = re.sub(r"(?i)^search\s+", f"search index={index} ", spl, count=1)
    return spl


class Client:
    def __init__(self, inst: sconfig.Instance):
        self.inst = inst
        self.base = inst.base_url
        self.app = str(inst.options.get("app") or "")
        self.index = str(inst.options.get("index") or "")
        try:
            cap = int(inst.options.get("max_results") or 100)
        except (TypeError, ValueError):
            cap = 100
        self.max_results = max(1, min(cap, HARD_MAX_RESULTS))
        self._ctx = None
        if str(inst.options.get("verify_tls", True)).lower() in ("false", "0", "no"):
            print("WARNING: splunk: TLS certificate verification is DISABLED (verify_tls=false in config) - "
                  "the token can be intercepted; use a trusted CA instead.", file=sys.stderr)
            self._ctx = ssl._create_unverified_context()

    def path(self, tail: str) -> str:
        return f"/servicesNS/-/{q(self.app)}{tail}" if self.app else f"/services{tail}"

    def request(self, method: str, path: str, params: dict | None = None, form: dict | None = None):
        params = dict(params or {})
        params["output_mode"] = "json"
        url = self.base + path + "?" + urllib.parse.urlencode(params)
        data = urllib.parse.urlencode(form).encode() if form is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.inst.token}", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S, context=self._ctx) as resp:
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                msgs = json.loads(exc.read().decode("utf-8", "replace")).get("messages") or []
                detail = "; ".join(m.get("text", "") for m in msgs)[:300]
            except (ValueError, OSError):
                pass
            raise ApiError(exc.code, detail or str(exc.reason)) from None
        except (urllib.error.URLError, OSError, ssl.SSLError) as exc:
            raise ApiError(0, f"network error: {getattr(exc, 'reason', exc)}") from None
        try:
            return json.loads(body) if body.strip() else {}
        except ValueError:
            raise ApiError(0, "non-JSON response (is base_url the management port, e.g. :8089?)") from None

    def entries(self, path: str, params: dict | None = None) -> list[dict]:
        return (self.request("GET", path, params) or {}).get("entry") or []


def client(name: str | None = None) -> Client:
    return Client(sconfig.load_instance(name))


def clip(value, width: int = 200) -> str:
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    s = s.replace("\n", " ").replace("\t", " ")
    return s if len(s) <= width else s[: width - 1] + "…"


def compact_rows(rows: list[dict], fields: list[str] | None = None, width: int = 200) -> list[dict]:
    """Drop Splunk-internal fields (leading `_`, except _time/_raw) unless `fields` names columns explicitly."""
    out = []
    for r in rows:
        keys = fields or [k for k in r if not k.startswith("_") or k in ("_time", "_raw")]
        out.append({k: clip(r.get(k, ""), width) for k in keys})
    return out


def table(rows: list[dict]) -> str:
    if not rows:
        return "(no rows)"
    cols = list(dict.fromkeys(k for r in rows for k in r))
    lines = ["\t".join(cols)] + ["\t".join(str(r.get(c, "")) for c in cols) for r in rows]
    return "\n".join(lines)


def job_info(c: Client, sid: str) -> dict:
    ent = c.entries(c.path(f"/search/jobs/{q(check_id(sid, 'sid'))}"))
    ct = (ent[0].get("content") if ent else None) or {}
    return {"sid": sid, "state": ct.get("dispatchState"), "done": bool(ct.get("isDone")),
            "failed": bool(ct.get("isFailed")), "progress": ct.get("doneProgress"),
            "events": ct.get("eventCount"), "results": ct.get("resultCount"), "scanned": ct.get("scanCount"),
            "earliest": ct.get("earliestTime"), "latest": ct.get("latestTime"), "run_s": ct.get("runDuration"),
            "ttl_s": ct.get("ttl"), "search": clip(ct.get("search") or "", 300),
            "messages": [m.get("text") for m in (ct.get("messages") or [])][:3] if isinstance(ct.get("messages"), list) else []}


def job_results(c: Client, sid: str, count: int, offset: int = 0) -> tuple[list[dict], int]:
    count = max(1, min(count, c.max_results))
    res = c.request("GET", c.path(f"/search/jobs/{q(check_id(sid, 'sid'))}/results"),
                    {"count": count, "offset": max(0, offset)})
    return res.get("results") or [], len(res.get("results") or [])


def cancel_job(c: Client, sid: str) -> dict:
    c.request("POST", c.path(f"/search/jobs/{q(check_id(sid, 'sid'))}/control"), form={"action": "cancel"})
    return {"sid": sid, "cancelled": True}


def run_search(c: Client, spl: str, earliest: str = "-15m", latest: str = "now", count: int = 20,
               timeout: int = 60) -> dict:
    """Create a normal-mode job, wait up to `timeout` s, return at most `count` rows. A job that is not done in
    time is cancelled (it is ours) and reported in `note`."""
    for label, v in (("earliest", earliest), ("latest", latest)):
        if not re.fullmatch(r"[A-Za-z0-9_@:.\-+/]{1,40}", v or ""):
            raise ValueError(f"invalid {label} time: {v!r}")
    count = max(1, min(int(count), c.max_results))
    form = {"search": build_spl(spl, c.index), "earliest_time": earliest, "latest_time": latest,
            "max_count": str(c.max_results), "exec_mode": "normal"}
    sid = (c.request("POST", c.path("/search/jobs"), form=form) or {}).get("sid")
    if not sid:
        raise ApiError(0, "Splunk returned no sid")
    deadline = time.monotonic() + max(5, min(int(timeout), 300))
    info = {}
    while time.monotonic() < deadline:
        info = job_info(c, sid)
        if info["done"] or info["failed"]:
            break
        time.sleep(1.0)
    if info.get("failed"):
        raise ApiError(0, f"search failed (sid {sid}): " + "; ".join(str(m) for m in info.get("messages") or [])[:300])
    if not info.get("done"):
        try:
            cancel_job(c, sid)
        except ApiError:
            pass
        return {"sid": sid, "state": "TIMEOUT", "done": False, "total": info.get("results"), "rows": [],
                "note": f"not finished in {timeout}s; job cancelled. Narrow the window/filters or raise --timeout."}
    rows, _ = job_results(c, sid, count)
    total = int(info.get("results") or 0)
    note = f"showing {len(rows)} of {total} results" + ("; raise --count (max %d) or refine the query" % c.max_results if total > len(rows) else "")
    return {"sid": sid, "state": info.get("state"), "done": True, "total": total, "rows": rows, "note": note}
