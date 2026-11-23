"""Chat-adapter catalog for the Settings UI (Slack, Telegram).

Adapters are fixed singletons (unlike named integrations): each maps onto the existing
``adapters.<id>.*`` settings in ``config.SETTINGS``, so the config format is unchanged. This module
holds the manifests and the connection tests; ``settings_api`` exposes them under ``/api/adapters``.

Tests never log or return a token: errors carry Slack/Telegram error codes only.
"""
from __future__ import annotations

import os
import re
import time
from typing import Any, Callable

import httpx

TIMEOUT_S = 10.0

ADAPTERS: dict[str, dict] = {
    "slack": {
        "title": "Slack",
        "icon": "slack",
        "help": "Socket Mode app. Needs a bot token (xoxb-…) and an app-level token (xapp-…, scope connections:write).",
        "required": ["bot_token", "app_token"],
        "fields": ["bot_token", "app_token", "allowed_user_id"],
    },
    "telegram": {
        "title": "Telegram",
        "icon": "telegram",
        "help": "Long-polling bot. Create it with @BotFather; at least one allowed user id is required (fail closed).",
        "required": ["bot_token", "allowed_user_ids"],
        "fields": ["bot_token", "allowed_user_ids", "forum_chat_id", "max_update_age_s", "poll_timeout_s"],
    },
}


# --- connection tests -------------------------------------------------------------------------

def _http(method: str, url: str, **kw) -> dict:
    """Single HTTP seam (tests monkeypatch it). Returns the decoded JSON body."""
    r = httpx.request(method, url, timeout=TIMEOUT_S, **kw)
    try:
        return r.json()
    except ValueError:
        return {"ok": False, "description": f"HTTP {r.status_code}"}


def _slack_test(v: dict) -> dict:
    bot, app = v.get("bot_token") or "", v.get("app_token") or ""
    if not bot or not app:
        return _fail("invalid_config", "bot_token and app_token are required")
    if not app.startswith("xapp-"):
        return _fail("invalid_config", "app_token must start with xapp-")
    auth = _http("POST", "https://slack.com/api/auth.test", headers={"Authorization": f"Bearer {bot}"})
    if not auth.get("ok"):
        return _fail("connection_failed", f"bot token: {auth.get('error', 'rejected')}")
    sock = _http("POST", "https://slack.com/api/apps.connections.open", headers={"Authorization": f"Bearer {app}"})
    if not sock.get("ok"):
        return _fail("connection_failed", f"app token: {sock.get('error', 'rejected')}")
    return {"ok": True, "identity": f"{auth.get('user', '?')} @ {auth.get('team', '?')}"}


def _telegram_test(v: dict) -> dict:
    token = v.get("bot_token") or ""
    if not token:
        return _fail("invalid_config", "bot_token is required")
    ids = v.get("allowed_user_ids") or []
    if not ids or not all(re.fullmatch(r"-?\d+", str(i)) for i in ids):
        return _fail("invalid_config", "allowed_user_ids must be a non-empty list of numeric ids")
    base = f"https://api.telegram.org/bot{token}"
    me = _http("GET", f"{base}/getMe")
    if not me.get("ok"):
        return _fail("connection_failed", f"getMe: {me.get('description', 'rejected')}")
    name = "@" + str((me.get("result") or {}).get("username", "?"))
    forum = v.get("forum_chat_id")
    if forum:
        chat = _http("GET", f"{base}/getChat", params={"chat_id": forum})
        if not chat.get("ok"):
            return _fail("connection_failed", f"forum chat: {chat.get('description', 'not reachable')}")
        if not (chat.get("result") or {}).get("is_forum"):
            return _fail("connection_failed", "forum chat is not a forum (enable Topics in the group)")
    return {"ok": True, "identity": name}


def _fail(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message}}


_TESTS: dict[str, Callable[[dict], dict]] = {"slack": _slack_test, "telegram": _telegram_test}


def run_test(adapter_id: str, values: dict[str, Any]) -> dict:
    """values: plaintext field values (secrets resolved). Returns {ok, identity?|error, latency_ms}."""
    t0 = time.monotonic()
    try:
        out = _TESTS[adapter_id](values)
    except httpx.TimeoutException:
        out = _fail("timeout", f"no answer within {int(TIMEOUT_S)}s")
    except Exception as e:  # never leak URLs (Telegram tokens live in them)
        out = _fail("connection_failed", type(e).__name__)
    out["latency_ms"] = int((time.monotonic() - t0) * 1000)
    return out
