"""Thin Grafana HTTP client shared by the CLI, `fetch`/`test` and the watcher script.

    client(instance_name=None) -> Client      (config + token resolved via grafana_config / grafana_oauth)
    Client.request(method, path, params=None, body=None) -> parsed JSON (or text)

Errors are `ApiError(status, message)`; `status` is 0 for network failures. Tokens only ever live in the
Authorization header of the request; error messages never contain them.
"""
from __future__ import annotations

import json
from urllib.parse import quote

import grafana_config as grconfig
import grafana_oauth as oauth

TIMEOUT_S = 25


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}" if status else message)
        self.status = status

    @property
    def transient(self) -> bool:
        return self.status in (0, 429, 500, 502, 503, 504)


def q(value: str) -> str:
    return quote(str(value), safe="")


class Client:
    def __init__(self, inst: grconfig.Instance):
        self.inst = inst
        self.base = inst.base_url

    def _token(self) -> str:
        if self.inst.auth_type == grconfig.OAUTH_AUTH:
            try:
                return oauth.access_token(self.inst)
            except oauth.OAuthError as exc:
                raise ApiError(401, str(exc)) from None
            except Exception as exc:
                raise ApiError(0, f"OAuth refresh failed ({type(exc).__name__})") from None
        return self.inst.token

    def headers(self) -> dict:
        h = {"Authorization": f"Bearer {self._token()}", "Accept": "application/json"}
        org = str(self.inst.options.get("org_id") or "")
        if org:
            h["X-Grafana-Org-Id"] = org
        return h

    def request(self, method: str, path: str, params: dict | None = None, body=None):
        import requests
        if not path.startswith("/"):
            path = "/" + path
        try:
            r = requests.request(method.upper(), self.base + path, headers=self.headers(), params=params,
                                 json=body if body is not None else None, timeout=TIMEOUT_S)
        except requests.RequestException as exc:
            raise ApiError(0, f"{self.base} unreachable ({type(exc).__name__}): check base_url / VPN") from None
        if not r.ok:
            try:
                msg = r.json().get("message") or r.text
            except ValueError:
                msg = r.text
            raise ApiError(r.status_code, " ".join(str(msg).split())[:300])
        try:
            return r.json()
        except ValueError:
            return r.text

    def get(self, path, params=None):
        return self.request("GET", path, params)


def client(instance: str | None = None) -> Client:
    return Client(grconfig.load_instance(instance))


def dumps(obj, limit: int = 0) -> str:
    s = json.dumps(obj, indent=2, ensure_ascii=False)
    return s if not limit or len(s) <= limit else s[:limit] + f"\n… [{len(s) - limit} more chars; narrow the query]"


def alert_instances(c: Client, states=("firing",), limit: int = 200) -> list[dict]:
    """Flat list of Grafana-managed alert instances in the given states (bounded by `limit`)."""
    data = c.get("/api/prometheus/grafana/api/v1/rules").get("data") or {}
    out = []
    for g in data.get("groups") or []:
        for rule in g.get("rules") or []:
            for a in rule.get("alerts") or []:
                state = str(a.get("state") or "").lower()
                state = "firing" if state == "alerting" else state
                if "all" not in states and not any(state.startswith(s) for s in states):
                    continue
                labels = {k: v for k, v in (a.get("labels") or {}).items() if k != "alertname"}
                fp = rule.get("uid", "") + "|" + ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
                out.append({"rule": rule.get("name", ""), "uid": rule.get("uid", ""), "folder": g.get("file", ""),
                            "state": state, "labels": labels, "active_at": a.get("activeAt", ""), "fp": fp})
                if len(out) >= limit:
                    return out
    return out
