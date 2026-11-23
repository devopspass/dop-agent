"""Minimal, metadata-only Vault HTTP client with a HARD GUARD against secret-data endpoints.

Vault answers some endpoints with secret values (KV `<mount>/data/..`, dynamic `creds`, transit
`decrypt`/`sign`, wrapped `unwrap`, `sys/raw`, cubbyhole, `read` of secrets, ...). This client never calls them:
every request goes through `check_request(method, path)`, which

  1. rejects malformed paths (`..`, `//`, query strings, odd characters),
  2. rejects any path with a secret-data segment (`data`, `creds`, `decrypt`, `sign`, `read`, ... before a
     `metadata` segment or anywhere outside `sys/leases/lookup/<lease id>`) with `SecretEndpointError` explaining why,
  3. then only allows an explicit allow-list of metadata endpoints (allow-list, not just deny-list, so an
     unknown endpoint is refused too). Writes are limited to the two read-only "lookup" POSTs
     (`sys/capabilities-self`, `sys/leases/lookup`); there is no generic write.

Responses of `auth/token/lookup-self` are scrubbed of the token itself (`id`), see `scrub`.
Token is sent only as the X-Vault-Token header and is redacted from every error text.
"""
from __future__ import annotations

import json
import re
import ssl
import sys
import urllib.error
import urllib.request

MAX_BYTES = 2_000_000
TIMEOUT = 20

SECRET_SEGMENTS = {
    "data", "creds", "cred", "decrypt", "rewrap", "sign", "sign-verbatim", "issue", "read", "sts", "unwrap",
    "raw", "cubbyhole", "export", "hmac", "secret-id", "role-id", "generate-root", "rekey", "unseal", "token-create",
}
_PATH_OK = re.compile(r"^[A-Za-z0-9_.\-/@+=:]+$")

_GET_ALLOW = [re.compile(p) for p in (
    r"^sys/(health|seal-status|leader|auth|mounts|audit|leases/count|internal/ui/mounts(/.+)?)$",
    r"^sys/policies/acl/[^/]+$",
    r"^sys/policy/[^/]+$",
    r"^sys/policy$",
    r"^auth/token/lookup-self$",
    r"^.+/metadata(/.*)?$",
)]
_LIST_ALLOW = [re.compile(p) for p in (
    r"^sys/policies/acl/?$",
    r"^sys/leases/lookup/.*$",
    r"^[^/]+(/[^/]+)*/?$",
)]
_POST_ALLOW = {"sys/capabilities-self", "sys/leases/lookup"}

_REDACT = re.compile(r"(?i)((?:token|password|secret|authorization|x-vault-token)[\"']?\s*[=:]\s*[\"']?)[^\s\"',}]+")
_VAULT_TOKEN = re.compile(r"\b(hvs|hvb|hvr|s)\.[A-Za-z0-9_\-]{16,}")


class ApiError(RuntimeError):
    pass


class SecretEndpointError(ApiError):
    """Raised for endpoints that would return secret data; never sent to the server."""


def redact(text: str) -> str:
    return _VAULT_TOKEN.sub("<token>", _REDACT.sub(r"\1<redacted>", str(text)))


def clean_path(path: str) -> str:
    p = (path or "").strip().lstrip("/")
    if p.startswith("v1/"):
        p = p[3:]
    if not p or ".." in p or "//" in p or not _PATH_OK.match(p):
        raise ApiError(f"invalid Vault path {path!r} (no '..', '//', query strings or special characters)")
    return p


def check_request(method: str, path: str) -> str:
    """Return the cleaned path if (method, path) is a metadata-only call, else raise."""
    p = clean_path(path)
    method = method.upper()
    segs = [s.lower() for s in p.rstrip("/").split("/")]
    if "metadata" in segs[1:]:
        scan = segs[: segs.index("metadata")]
    elif p.startswith("sys/leases/lookup"):
        scan = []  # the rest is a lease id such as aws/creds/role/<id>; lookup returns metadata only
    else:
        scan = segs
    bad = [s for s in scan if s in SECRET_SEGMENTS]
    if bad:
        raise SecretEndpointError(
            f"refused: '{p}' contains '{bad[0]}' and would return secret data. This integration is metadata-only "
            "(health, auth methods, mounts, policies, KV metadata, token/lease info, audit devices); "
            "secret values must never be read through it.")
    rules = {"GET": _GET_ALLOW, "LIST": _LIST_ALLOW}.get(method)
    if method == "POST":
        ok = p in _POST_ALLOW
    elif rules is None:
        raise ApiError(f"refused: method {method} is not allowed (read-only, metadata-only integration)")
    else:
        ok = any(r.match(p) for r in rules)
    if not ok:
        raise SecretEndpointError(
            f"refused: {method} '{p}' is not on the metadata allow-list (this integration only reads health, auth, "
            "mounts, policies, KV metadata, token/lease info and audit devices; it never reads secret data).")
    return p


def scrub(path: str, body):
    """Drop token material from responses that carry it (token lookup returns the token as `id`)."""
    if isinstance(body, dict) and path.startswith("auth/token/lookup"):
        d = body.get("data")
        if isinstance(d, dict):
            d.pop("id", None)
    return body


class Client:
    def __init__(self, inst):
        self.inst = inst
        self.ctx = None
        if inst.base_url.startswith("https://") and not inst.tls_verify:
            print("WARNING: vault: TLS certificate verification is DISABLED (tls_verify=false in config) - "
                  "the token can be intercepted; use a trusted CA instead.", file=sys.stderr)
            self.ctx = ssl._create_unverified_context()

    def request(self, method: str, path: str, body: dict | None = None, ok_statuses=()):
        """Guarded request -> (status, parsed JSON or {}). Raises ApiError on other non-2xx."""
        p = check_request(method, path)
        url = f"{self.inst.base_url}/v1/{p}"
        if method.upper() == "LIST":
            url += "?list=true"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method="GET" if method.upper() == "LIST" else method.upper())
        req.add_header("X-Vault-Token", self.inst.token)
        if self.inst.namespace:
            req.add_header("X-Vault-Namespace", self.inst.namespace.strip("/"))
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=self.ctx) as resp:
                status, raw = resp.status, resp.read(MAX_BYTES)
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read(MAX_BYTES)
            if status not in ok_statuses:
                try:
                    errs = "; ".join(json.loads(raw).get("errors") or [])
                except (ValueError, AttributeError):
                    errs = raw[:200].decode("utf-8", "replace")
                hint = " (check token policy/namespace)" if status in (403, 401) else ""
                raise ApiError(redact(f"HTTP {status} for {method} {p}: {errs}{hint}")) from None
        except (urllib.error.URLError, OSError) as exc:
            raise ApiError(redact(f"cannot reach {self.inst.base_url}: {getattr(exc, 'reason', exc)}")) from None
        try:
            parsed = json.loads(raw) if raw else {}
        except ValueError:
            parsed = {}
        return status, scrub(p, parsed)

    def get(self, path: str):
        return self.request("GET", path)[1]

    def list(self, path: str) -> list:
        try:
            return (self.request("LIST", path)[1].get("data") or {}).get("keys") or []
        except ApiError as exc:
            if "HTTP 404" in str(exc):
                return []
            raise

    def health(self) -> tuple[int, dict]:
        return self.request("GET", "sys/health", ok_statuses=(429, 472, 473, 501, 503))


HEALTH_STATES = {200: "active", 429: "standby", 472: "dr-secondary", 473: "performance-standby",
                 501: "not-initialized", 503: "sealed"}


def parse_time(value):
    """RFC3339 (Vault uses nanoseconds) -> aware datetime, or None for empty/zero/unparseable values."""
    from datetime import datetime, timezone
    if not value or str(value).startswith("0001-"):
        return None
    s = re.sub(r"(\.\d{6})\d+", r"\1", str(value)).replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def token_info(client) -> dict:
    """Own-token metadata (no token value): accessor, policies, ttl, expire_time, renewable, display_name."""
    d = client.get("auth/token/lookup-self").get("data") or {}
    keep = ("accessor", "display_name", "policies", "ttl", "creation_ttl", "expire_time", "renewable", "orphan",
            "type", "num_uses", "path", "meta", "entity_id")
    return {k: d.get(k) for k in keep if k in d}


def lease_rows(c, prefix: str, limit: int, within_h: float | None = None):
    """Walk lease listing below prefix (bounded by limit) and look each up: [(lease_id, ttl, expire_time)]."""
    from datetime import datetime, timezone
    rows, stack, now = [], [prefix.strip("/")], datetime.now(timezone.utc)
    seen = 0
    while stack and len(rows) < limit and seen < limit * 4:
        pre = stack.pop()
        for k in c.list(f"sys/leases/lookup/{pre}/" if pre else "sys/leases/lookup/"):
            seen += 1
            if k.endswith("/"):
                stack.append(f"{pre}/{k.rstrip('/')}" if pre else k.rstrip("/"))
                continue
            lid = f"{pre}/{k}" if pre else k
            d = c.request("POST", "sys/leases/lookup", {"lease_id": lid})[1].get("data") or {}
            exp = parse_time(d.get("expire_time"))
            if within_h is not None and (exp is None or (exp - now).total_seconds() > within_h * 3600):
                continue
            rows.append((lid, d.get("ttl"), d.get("expire_time"), d.get("renewable")))
            if len(rows) >= limit:
                break
    return rows
