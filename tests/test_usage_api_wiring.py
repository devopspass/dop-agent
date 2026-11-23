"""
Usage widget <-> conversation wiring.

message_store.list_conversations() exposes the Copilot session id under the key
``session_id`` (``copilot_session_id`` is only the raw DB column). Guarantee: a
session whose Copilot session id has indexed AIU gets that AIU in the session
payload, and /api/usage's top-sessions rows carry title/link.
"""

from __future__ import annotations

import usage_index
import web_server


def test_session_payload_carries_aiu_keyed_by_the_session_id_key(monkeypatch):
    convo = {"conversation_id": "c1", "session_id": "sid-1", "turn_count": 1}
    monkeypatch.setattr(web_server.message_store, "list_conversations", lambda: [dict(convo)])
    monkeypatch.setattr(web_server.message_store, "list_all_bindings", lambda: {})
    monkeypatch.setattr(web_server.message_store, "list_groups", lambda include_archived=True: [])
    monkeypatch.setattr(usage_index, "aiu_by_session_cached", lambda max_age=15.0: {"sid-1": 123.456})
    items = web_server._session_payload()
    assert items[0]["aiu"] == 123.5


def test_api_usage_refresh_param_forces_quota_fetch(monkeypatch):
    """`/api/usage?refresh=1` (the popover's 'refresh now' link) must reach usage_index.fetch_quota as force=True,
    while a plain poll passes force=False."""
    import asyncio
    seen = []

    async def fake_fetch(client, force=False):
        seen.append(force)
        return {"used": 1}

    monkeypatch.setattr(usage_index, "fetch_quota", fake_fetch)
    monkeypatch.setattr(usage_index, "enabled", lambda: True)
    monkeypatch.setattr(usage_index, "ensure_background_scan", lambda: None)
    monkeypatch.setattr(usage_index, "indexing", lambda: False)
    monkeypatch.setattr(usage_index, "summary", lambda: {"ranges": {}})
    monkeypatch.setattr(web_server, "_require_auth", lambda request: None)
    monkeypatch.setattr(web_server.message_store, "list_conversations", lambda: [])
    for flag in (False, True):
        asyncio.run(web_server.api_usage(None, None, flag))
    assert seen == [False, True]
