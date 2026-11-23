"""Reusable ServiceNow Table API helper (stdlib only), shared by cli.py and the watcher script.

Deliberately generic (no ITSM- or CMDB-specific logic): connection state, one HTTP function, validated table/query/field
arguments and thin `table_list` / `table_get` / `table_create` / `table_update` wrappers. The token lives only in module
state (never in CONF/argv/output) and is redacted from every error text.

    use(base_url, username, token, auth_type="basic")   set the connection (tests, embedding)
    configure(instance=None)                             resolve it from config.yaml via snowconfig
    table_list(table, query, fields, limit, offset, order)  -> list[dict] (display_value=all: {value, display_value} per field)
    table_get(table, sys_id)  table_create(table, data)  table_update(table, sys_id, data)

`fail` raises ApiError; callers (CLI/watch) turn it into a one-line message. HTTP 429/5xx/network errors are
`ApiError.transient` (code -1 = network).
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

CONF = {"base_url": "", "username": "", "auth_type": "basic"}
_TOKEN = ""
TABLE_RE = re.compile(r"[a-z][a-z0-9_]{1,79}")
SYS_ID_RE = re.compile(r"[0-9a-f]{32}")
FIELD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.]{0,79}")
SECRET_RE = re.compile(r"(?i)(token|password|secret|authorization)([=:\s]+)(?:(?:basic|bearer)\s+)?\S+")
MAX_LIMIT = 200
TIMEOUT = 60


class ApiError(RuntimeError):
    def __init__(self, msg, code=0):
        super().__init__(msg)
        self.code = code

    @property
    def transient(self):
        return self.code in (-1, 429) or self.code >= 500


def redact(text):
    text = SECRET_RE.sub(lambda m: m.group(1) + m.group(2) + "***", str(text))
    return text.replace(_TOKEN, "***") if _TOKEN else text


def use(base_url, username, token, auth_type="basic"):
    global _TOKEN
    CONF.update(base_url=base_url.rstrip("/"), username=username or "", auth_type=auth_type or "basic")
    _TOKEN = token


def configure(instance=None):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import snowconfig
    try:
        _, base, token, opts, auth_type = snowconfig.load_instance(instance)
    except snowconfig.InstanceError as exc:
        raise ApiError(redact(exc)) from None
    use(base, str(opts.get("username") or ""), token, auth_type)


def _auth():
    base = CONF["base_url"]
    if not (base and _TOKEN):
        raise ApiError("ServiceNow is not configured: pass --instance <name>")
    if not re.match(r"^https?://", base):
        raise ApiError("base URL must start with http(s)://")
    if CONF["auth_type"] == "bearer":
        return "Bearer " + _TOKEN
    return "Basic " + base64.b64encode(f"{CONF['username']}:{_TOKEN}".encode()).decode()


def request(method, path, query=None, data=None):
    """One JSON call to the instance; path must start with /api/ (never an arbitrary URL)."""
    if not path.startswith("/api/"):
        raise ApiError("path must start with /api/")
    auth = _auth()
    url = CONF["base_url"] + path
    if query:
        url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v not in (None, "")})
    req = urllib.request.Request(url, method=method, data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Authorization": auth, "Accept": "application/json", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = r.read()
    except urllib.error.HTTPError as exc:
        raise ApiError(f"HTTP {exc.code} {method} {path}: " + redact(exc.read().decode(errors="replace")[:300]), exc.code) from None
    except urllib.error.URLError as exc:
        raise ApiError(f"network error {method} {path}: {redact(exc.reason)}", -1) from None
    try:
        return json.loads(body) if body else {}
    except ValueError:
        raise ApiError(f"non-JSON answer from {path} (is base_url a ServiceNow instance and is the user allowed to use the REST API?)") from None


def check_table(table):
    if not TABLE_RE.fullmatch(table or ""):
        raise ApiError(f"invalid table name {table!r}")
    return table


def check_fields(fields):
    fields = [f for f in (fields or []) if f]
    for f in fields:
        if not FIELD_RE.fullmatch(f):
            raise ApiError(f"invalid field name {f!r}")
    return fields


def quote_value(v):
    """Value for an encoded query: `^` (clause separator) and newlines would change the query's meaning."""
    v = str(v)
    if re.search(r"[\^\r\n]", v):
        raise ApiError(f"value {v!r} must not contain ^ or newlines")
    return v


def check_user_query(q):
    """User-supplied encoded query: read-only through the Table API GET, but no server-side scripts."""
    q = (q or "").strip()
    if len(q) > 1000 or re.search(r"[\r\n]|javascript:|gs\.", q, re.I):
        raise ApiError("query too long or contains javascript:/gs. (server-side scripts are not allowed)")
    return q


def table_list(table, query="", fields=None, limit=20, offset=0, order=None, display="all"):
    """Rows of `table`; order like `-sys_updated_on` (desc) or `number`; limit is clamped to MAX_LIMIT."""
    check_table(table)
    q = query or ""
    if order:
        o = order[1:] if order.startswith("-") else order
        check_fields([o])
        q = (q + "^" if q else "") + ("ORDERBYDESC" if order.startswith("-") else "ORDERBY") + o
    res = request("GET", f"/api/now/table/{table}", {
        "sysparm_query": q, "sysparm_fields": ",".join(check_fields(fields)), "sysparm_limit": max(1, min(int(limit), MAX_LIMIT)),
        "sysparm_offset": max(0, int(offset)), "sysparm_display_value": display, "sysparm_exclude_reference_link": "true"})
    return res.get("result") or []


def table_get(table, sys_id, fields=None):
    check_table(table)
    if not SYS_ID_RE.fullmatch(sys_id or ""):
        raise ApiError(f"invalid sys_id {sys_id!r}")
    return request("GET", f"/api/now/table/{table}/{sys_id}", {
        "sysparm_fields": ",".join(check_fields(fields)), "sysparm_display_value": "all", "sysparm_exclude_reference_link": "true"}).get("result") or {}


def table_create(table, data):
    return request("POST", f"/api/now/table/{check_table(table)}", {"sysparm_display_value": "all"}, data).get("result") or {}


def table_update(table, sys_id, data):
    check_table(table)
    if not SYS_ID_RE.fullmatch(sys_id or ""):
        raise ApiError(f"invalid sys_id {sys_id!r}")
    return request("PATCH", f"/api/now/table/{table}/{sys_id}", {"sysparm_display_value": "all"}, data).get("result") or {}


def val(cell):
    """Raw value of a display_value=all cell (or of a plain value)."""
    return cell.get("value", "") if isinstance(cell, dict) else ("" if cell is None else cell)


def disp(cell):
    """Display value of a cell, falling back to the raw value."""
    if isinstance(cell, dict):
        return cell.get("display_value") or cell.get("value") or ""
    return "" if cell is None else str(cell)
