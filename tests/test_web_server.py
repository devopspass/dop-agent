"""
Unit tests for web_server.py's REST API, run against a throwaway sqlite DB
(monkeypatched message_store globals, same pattern as test_message_store.py)
and a FastAPI TestClient — no real Slack client, no live uvicorn server, no
real Copilot SDK session. Browser/DOM-level coverage for the Web UI lives
separately in dop-agent/ui_tests/ (Playwright).
"""

from __future__ import annotations

import asyncio
import os

import pytest
from fastapi.testclient import TestClient

import copilot_session
import handlers
import message_store
import transcription
import web_server
from adapters import ConversationBroadcaster, SlackAdapter, WebAdapter


class _FakeConvo:
    """Stand-in for handlers.CopilotConversation - only the bits
    web_server.api_stop() touches."""

    def __init__(self, interrupt_result: bool = True):
        self._interrupt_result = interrupt_result
        self.interrupt_called = False

    async def interrupt(self) -> bool:
        self.interrupt_called = True
        return self._interrupt_result


class _FakeSlackClient:
    def __init__(self):
        self.posted = []

    async def chat_postMessage(self, **kwargs):
        self.posted.append(kwargs)
        return {"ts": "111.222", "channel": kwargs.get("channel")}


def _use_tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(message_store, "DB_PATH", tmp_path / "bot_store.db")
    monkeypatch.setattr(message_store, "_TITLES_FILE", tmp_path / "session_titles.json")
    message_store.init_db()


def _slack_conversation_id(channel: str, thread_ts: str) -> str:
    return copilot_session._conversation_id_for(channel, thread_ts)


def _login(client: TestClient, monkeypatch):
    monkeypatch.setattr(web_server, "WEB_UI_PASSWORD", "secret123")
    resp = client.post("/api/login", json={"password": "secret123"})
    assert resp.status_code == 200


def test_stop_endpoint_interrupts_running_conversation(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv_id = message_store.get_or_create_conversation(None)
    convo = _FakeConvo(interrupt_result=True)
    monkeypatch.setattr(web_server, "_convos", {conv_id: convo})

    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.post(f"/api/sessions/{conv_id}/stop")

    assert resp.status_code == 200
    assert resp.json() == {"stopped": True}
    assert convo.interrupt_called is True


def test_authenticated_web_ui_send_bypasses_chat_adapter_identity_allowlist(tmp_path, monkeypatch):
    """Web UI sends already pass the password-cookie check and are not
    rejected by the Slack/Telegram sender allow-list, which has no browser
    user ID."""
    _use_tmp_store(tmp_path, monkeypatch)
    monkeypatch.setattr(web_server, "_webui_adapter", WebAdapter())
    monkeypatch.setattr(web_server, "_broadcaster", None)
    monkeypatch.setattr(web_server, "_allowed_user_id", "")
    calls = []

    async def capture_handle(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(web_server, "_handle_fn", capture_handle)
    scheduled = []
    monkeypatch.setattr(
        web_server.asyncio,
        "create_task",
        lambda coro: scheduled.append(coro) or type("_DummyTask", (), {})(),
    )

    client = TestClient(web_server.app)
    _login(client, monkeypatch)
    response = client.post("/api/send", json={"text": "hello from Web UI"})
    assert response.status_code == 200
    asyncio.run(scheduled.pop())

    assert len(calls) == 1
    assert calls[0]["user_id"] == ""
    assert calls[0]["authorized"] is True
    assert response.json()["conversation_id"]


def test_send_message_mirrors_web_origin_text_to_existing_slack_binding_without_duplicate_db_row(tmp_path, monkeypatch):
    """Phase 2 broadcaster integration: a Web-origin user message should be
    recorded once in SQLite for the Web UI and mirrored to the already-bound
    Slack thread without creating a second logical user turn in the store."""
    _use_tmp_store(tmp_path, monkeypatch)
    conversation_id = message_store.get_or_create_conversation(None)
    message_store.bind_conversation(conversation_id, message_store.WEB_PLATFORM, conversation_id)
    message_store.bind_conversation(conversation_id, message_store.SLACK_PLATFORM, "D123:999.1")

    fake_slack = _FakeSlackClient()
    monkeypatch.setattr(web_server, "_slack_client", fake_slack)
    monkeypatch.setattr(web_server, "_handle_fn", lambda **kwargs: asyncio.sleep(0))
    monkeypatch.setattr(web_server, "_allowed_user_id", "U_OWNER")
    monkeypatch.setattr(web_server, "_webui_adapter", WebAdapter())
    monkeypatch.setattr(
        web_server,
        "_broadcaster",
        ConversationBroadcaster(
            {
                message_store.WEB_PLATFORM: WebAdapter(),
                message_store.SLACK_PLATFORM: SlackAdapter(fake_slack),
            }
        ),
    )

    scheduled = []

    def _capture_task(coro):
        scheduled.append(coro)
        class _DummyTask:
            pass
        return _DummyTask()

    monkeypatch.setattr(web_server.asyncio, "create_task", _capture_task)

    returned_id = asyncio.run(web_server.send_message(conversation_id, "hello from browser"))
    for coro in scheduled:
        coro.close()

    assert returned_id == conversation_id
    assert len(fake_slack.posted) == 1
    assert fake_slack.posted[0]["channel"] == "D123"
    assert fake_slack.posted[0]["thread_ts"] == "999.1"
    assert "(via Web UI) hello from browser" in fake_slack.posted[0]["text"]
    messages = message_store.get_messages(conversation_id)
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "hello from browser"
    assert messages[0]["external_ref"] == "slack:D123:111.222"


def test_send_message_reads_slack_thread_from_binding_table_not_legacy_slack_key(tmp_path, monkeypatch):
    """Phase 4 binding migration: the Web UI must still locate the right Slack
    thread for a mirrored send even if conversations.slack_key is blank and
    only conversation_bindings carries the thread binding."""
    _use_tmp_store(tmp_path, monkeypatch)
    conversation_id = message_store.get_or_create_conversation(None)
    message_store.bind_conversation(conversation_id, message_store.WEB_PLATFORM, conversation_id)
    message_store.bind_conversation(conversation_id, message_store.SLACK_PLATFORM, "D999:777.1")

    fake_slack = _FakeSlackClient()
    monkeypatch.setattr(web_server, "_slack_client", fake_slack)
    monkeypatch.setattr(web_server, "_handle_fn", lambda **kwargs: asyncio.sleep(0))
    monkeypatch.setattr(web_server, "_allowed_user_id", "U_OWNER")
    monkeypatch.setattr(web_server, "_webui_adapter", WebAdapter())
    monkeypatch.setattr(
        web_server,
        "_broadcaster",
        ConversationBroadcaster(
            {
                message_store.WEB_PLATFORM: WebAdapter(),
                message_store.SLACK_PLATFORM: SlackAdapter(fake_slack),
            }
        ),
    )
    scheduled = []
    monkeypatch.setattr(
        web_server.asyncio,
        "create_task",
        lambda coro: scheduled.append(coro) or type("_DummyTask", (), {})(),
    )

    returned_id = asyncio.run(web_server.send_message(conversation_id, "binding-aware mirror"))
    for coro in scheduled:
        coro.close()

    assert returned_id == conversation_id
    assert fake_slack.posted[0]["channel"] == "D999"
    assert fake_slack.posted[0]["thread_ts"] == "777.1"


def test_stop_endpoint_returns_false_when_nothing_running(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv_id = message_store.get_or_create_conversation(None)
    convo = _FakeConvo(interrupt_result=False)
    monkeypatch.setattr(web_server, "_convos", {conv_id: convo})

    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.post(f"/api/sessions/{conv_id}/stop")

    assert resp.status_code == 200
    assert resp.json() == {"stopped": False}


def test_stop_endpoint_handles_conversation_with_no_registered_convo(tmp_path, monkeypatch):
    """A conversation can exist in bot_store.db (e.g. history-only, bot
    restarted since) without a live in-memory CopilotConversation - stop
    must not error, just report nothing was running."""
    _use_tmp_store(tmp_path, monkeypatch)
    conv_id = message_store.get_or_create_conversation(None)
    monkeypatch.setattr(web_server, "_convos", {})

    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.post(f"/api/sessions/{conv_id}/stop")

    assert resp.status_code == 200
    assert resp.json() == {"stopped": False}


def test_stop_endpoint_404s_for_unknown_conversation(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    monkeypatch.setattr(web_server, "_convos", {})

    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.post("/api/sessions/does-not-exist/stop")

    assert resp.status_code == 404


def test_stop_endpoint_requires_auth(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv_id = message_store.get_or_create_conversation(None)
    monkeypatch.setattr(web_server, "_convos", {})

    client = TestClient(web_server.app)
    resp = client.post(f"/api/sessions/{conv_id}/stop")

    assert resp.status_code == 401


def test_continue_endpoint_creates_new_conversation_with_backlink(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    old_id = message_store.get_or_create_conversation(None)
    message_store.record_message(old_id, "user", "hello from the original session")

    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.post(f"/api/sessions/{old_id}/continue")

    assert resp.status_code == 200
    new_id = resp.json()["conversation_id"]
    assert new_id != old_id
    assert message_store.get_conversation(new_id) is not None

    messages = message_store.get_messages(new_id)
    assert len(messages) == 1
    assert messages[0]["role"] == "system"
    assert "Continued from" in messages[0]["content"]
    assert f"#session:{old_id}" in messages[0]["content"]
    assert "hello from the original session" in messages[0]["content"]


def test_continue_endpoint_uses_custom_title_when_set(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    old_id = message_store.get_or_create_conversation(None)
    message_store.record_message(old_id, "user", "hello")
    message_store.set_title(old_id, "My Renamed Session")

    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.post(f"/api/sessions/{old_id}/continue")

    new_id = resp.json()["conversation_id"]
    messages = message_store.get_messages(new_id)
    assert "My Renamed Session" in messages[0]["content"]


def test_continue_endpoint_404s_for_unknown_conversation(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)

    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.post("/api/sessions/does-not-exist/continue")

    assert resp.status_code == 404


def test_continue_endpoint_requires_auth(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    old_id = message_store.get_or_create_conversation(None)

    client = TestClient(web_server.app)
    resp = client.post(f"/api/sessions/{old_id}/continue")

    assert resp.status_code == 401


def test_continue_endpoint_records_parent_conversation_id_not_just_backlink_text(tmp_path, monkeypatch):
    """The "Continue in new session" button and the session_create MCP tool
    share _new_conversation_id() precisely so both leave a real, queryable
    parent_conversation_id (not just a human-readable backlink message) -
    this is what lets a future feature (e.g. "show session tree") walk the
    link programmatically instead of parsing message text."""
    _use_tmp_store(tmp_path, monkeypatch)
    old_id = message_store.get_or_create_conversation(None)

    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.post(f"/api/sessions/{old_id}/continue")

    assert resp.status_code == 200
    new_id = resp.json()["conversation_id"]
    convo = message_store.get_conversation(new_id)
    assert convo["parent_conversation_id"] == old_id


def _setup_session_create_runtime(monkeypatch, fake_slack):
    monkeypatch.setattr(web_server, "_slack_client", fake_slack)
    monkeypatch.setattr(web_server, "_handle_fn", lambda **kwargs: asyncio.sleep(0))
    monkeypatch.setattr(web_server, "_allowed_user_id", "U_OWNER")
    monkeypatch.setattr(web_server, "_webui_adapter", WebAdapter())
    monkeypatch.setattr(
        web_server,
        "_broadcaster",
        ConversationBroadcaster(
            {
                message_store.WEB_PLATFORM: WebAdapter(),
                message_store.SLACK_PLATFORM: SlackAdapter(fake_slack),
            }
        ),
    )
    scheduled = []
    monkeypatch.setattr(
        web_server.asyncio,
        "create_task",
        lambda coro: scheduled.append(coro) or type("_DummyTask", (), {})(),
    )
    return scheduled


def test_session_create_mcp_tool_returns_conversation_id_and_web_url(tmp_path, monkeypatch):
    """Core contract of the new session_create MCP tool: given just a
    prompt, it creates a brand-new session and returns enough for the
    caller to both track it (conversation_id, for session_get/session_search)
    and open it directly (web_url, using the new #open= deep-link format),
    without blocking on the turn itself finishing."""
    _use_tmp_store(tmp_path, monkeypatch)
    fake_slack = _FakeSlackClient()
    scheduled = _setup_session_create_runtime(monkeypatch, fake_slack)

    result = asyncio.run(web_server.session_create("investigate the flaky test"))
    for coro in scheduled:
        coro.close()

    assert result["conversation_id"]
    # web_url is built from WEB_UI_PUBLIC_HOST (the hostname the browser is
    # actually logged in under), not WEB_UI_HOST (the bind address) - they
    # can legitimately differ (e.g. bind 127.0.0.1, browse via localhost),
    # see web_server.py's WEB_UI_PUBLIC_HOST docstring.
    assert result["web_url"] == f"http://{web_server.WEB_UI_PUBLIC_HOST}:{web_server.WEB_UI_PORT}/#open={result['conversation_id']}"
    convo = message_store.get_conversation(result["conversation_id"])
    assert convo is not None
    assert convo["parent_conversation_id"] is None
    # the turn is dispatched in the background, never awaited inline
    assert len(scheduled) == 1


def test_web_ui_link_uses_public_host_not_bind_host_when_they_differ(tmp_path, monkeypatch):
    """Generated Web UI links (session_create's web_url, _web_ui_new_session_url)
    are built from WEB_UI_PUBLIC_HOST (defaults to WEB_UI_HOST, the loopback bind
    address, but can be set independently), so the link's host matches the
    hostname the user browses with (e.g. "localhost"). webui/app.js's
    parseOwnSessionLink() is origin-scoped: a link on another host would open
    in a new tab with a separate cookie jar and bounce the user to /login.
    This locks in that the two can diverge and the link follows
    WEB_UI_PUBLIC_HOST."""
    monkeypatch.setattr(web_server, "WEB_UI_HOST", "127.0.0.1")
    monkeypatch.setattr(web_server, "WEB_UI_PUBLIC_HOST", "localhost")
    monkeypatch.setattr(web_server, "WEB_UI_PORT", 10818)

    url = web_server._web_ui_url_for("some-conversation-id")
    assert url == "http://localhost:10818/#open=some-conversation-id"
    assert "127.0.0.1" not in url

    new_session_url = web_server._web_ui_new_session_url()
    assert new_session_url == "http://localhost:10818/#new"
    assert "127.0.0.1" not in new_session_url


def test_session_create_with_parent_session_id_leaves_backlink_matching_continue_button(tmp_path, monkeypatch):
    """parent_session_id must reuse the exact same linkage mechanism as the
    Web UI's "Continue in new session" button (both call
    _new_conversation_id() under the hood) - not a parallel/divergent
    implementation."""
    _use_tmp_store(tmp_path, monkeypatch)
    parent_id = message_store.get_or_create_conversation(None)
    message_store.set_title(parent_id, "Original investigation")
    fake_slack = _FakeSlackClient()
    scheduled = _setup_session_create_runtime(monkeypatch, fake_slack)

    result = asyncio.run(
        web_server.session_create("follow-up task", parent_session_id=parent_id)
    )
    for coro in scheduled:
        coro.close()

    convo = message_store.get_conversation(result["conversation_id"])
    assert convo["parent_conversation_id"] == parent_id
    history = message_store.get_messages(result["conversation_id"])
    backlink_texts = [m["content"] for m in history if m["role"] == "system"]
    assert any("Continued from" in t and parent_id in t for t in backlink_texts)


def test_session_create_mirror_true_fans_out_to_known_slack_dm(tmp_path, monkeypatch):
    """Per the standing decision that mirroring to all plugged-in
    destinations defaults on: a session_create call with the default
    mirror=True must reach an already-known Slack DM channel, same as any
    other new Web UI conversation."""
    _use_tmp_store(tmp_path, monkeypatch)
    existing = message_store.get_or_create_conversation("D555:1111.0001")
    fake_slack = _FakeSlackClient()
    scheduled = _setup_session_create_runtime(monkeypatch, fake_slack)

    asyncio.run(web_server.session_create("mirrored prompt"))
    for coro in scheduled:
        coro.close()

    assert fake_slack.posted
    assert fake_slack.posted[0]["channel"] == "D555"


def test_session_create_mirror_false_stays_web_ui_only(tmp_path, monkeypatch):
    """mirror=False must produce a Web-UI-only session even when a Slack DM
    channel is already known - this is the opt-out for agent-spawned
    side-investigations that shouldn't interrupt the user's phone/Slack."""
    _use_tmp_store(tmp_path, monkeypatch)
    message_store.get_or_create_conversation("D555:1111.0001")
    fake_slack = _FakeSlackClient()
    scheduled = _setup_session_create_runtime(monkeypatch, fake_slack)

    asyncio.run(web_server.session_create("quiet prompt", mirror=False))
    for coro in scheduled:
        coro.close()

    assert fake_slack.posted == []


def test_session_create_rejects_empty_prompt(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    fake_slack = _FakeSlackClient()
    scheduled = _setup_session_create_runtime(monkeypatch, fake_slack)

    try:
        asyncio.run(web_server.session_create("   "))
        raised = False
    except ValueError:
        raised = True
    for coro in scheduled:
        coro.close()

    assert raised is True


def test_session_create_rejects_unknown_parent_session_id(tmp_path, monkeypatch):
    """A typo'd/stale parent_session_id must fail with a clean ValueError,
    not an unhandled KeyError bubbling up from get_display_title() deep
    inside _new_conversation_id() - matches how api_continue_in_new_session
    already 404s on an unknown conversation_id before ever calling
    _new_conversation_id()."""
    _use_tmp_store(tmp_path, monkeypatch)
    fake_slack = _FakeSlackClient()
    scheduled = _setup_session_create_runtime(monkeypatch, fake_slack)

    try:
        asyncio.run(web_server.session_create("prompt", parent_session_id="does-not-exist"))
        raised = False
    except ValueError:
        raised = True
    for coro in scheduled:
        coro.close()

    assert raised is True


def test_session_create_with_title_sets_custom_title_immediately(tmp_path, monkeypatch):
    """title lets a caller (human via a prompt, or an agent) give the new
    session a meaningful name right away, instead of waiting for the
    session list to derive one from the first message's text."""
    _use_tmp_store(tmp_path, monkeypatch)
    fake_slack = _FakeSlackClient()
    scheduled = _setup_session_create_runtime(monkeypatch, fake_slack)

    result = asyncio.run(web_server.session_create("do the thing", title=":star: Sprint 24"))
    for coro in scheduled:
        coro.close()

    assert message_store.get_title(result["conversation_id"]) == ":star: Sprint 24"


def test_session_create_with_pinned_true_pins_new_session_immediately(tmp_path, monkeypatch):
    """pinned lets a caller create a session already pinned to the top of
    the session list, without a separate session_pin() round trip - same
    end state as calling session_pin(conversation_id, True) right after
    creation, just one call instead of two."""
    _use_tmp_store(tmp_path, monkeypatch)
    fake_slack = _FakeSlackClient()
    scheduled = _setup_session_create_runtime(monkeypatch, fake_slack)

    result = asyncio.run(web_server.session_create("do the thing", pinned=True))
    for coro in scheduled:
        coro.close()

    convo = message_store.get_conversation(result["conversation_id"])
    assert convo["pinned"] == 1


def test_session_create_without_pinned_leaves_session_unpinned(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    fake_slack = _FakeSlackClient()
    scheduled = _setup_session_create_runtime(monkeypatch, fake_slack)

    result = asyncio.run(web_server.session_create("do the thing"))
    for coro in scheduled:
        coro.close()

    convo = message_store.get_conversation(result["conversation_id"])
    assert convo["pinned"] == 0


# --- session_send MCP tool -------------------------------------------------
#
# Injects a new message into an EXISTING session - same effect as a human
# typing into the Web UI compose box or replying in that session's bound
# Slack thread, but callable by another agent/session. Reuses send_message()
# (the exact same path session_create()/the Web UI compose box use) so no
# new turn-dispatch machinery is introduced; the only new logic is the
# busy-target handling (interrupt=True/False + busy-context framing),
# mirroring watchers.py's _inject_watch_message().

class _FakeBusyConvo:
    """Stand-in for copilot_session.CopilotConversation - only is_busy()
    matters for session_send()'s busy-check branch."""

    def __init__(self, busy: bool):
        self._busy = busy

    def is_busy(self) -> bool:
        return self._busy


def _setup_session_send_runtime(monkeypatch, fake_slack, record_into=None):
    """Like _setup_session_create_runtime, but records the kwargs passed to
    _handle_fn (specifically `text`) so tests can assert on busy-context
    framing being appended (or not) to the injected message."""
    monkeypatch.setattr(web_server, "_slack_client", fake_slack)

    async def _recording_handle_fn(**kwargs):
        if record_into is not None:
            record_into.append(kwargs)

    monkeypatch.setattr(web_server, "_handle_fn", _recording_handle_fn)
    monkeypatch.setattr(web_server, "_allowed_user_id", "U_OWNER")
    monkeypatch.setattr(web_server, "_webui_adapter", WebAdapter())
    monkeypatch.setattr(
        web_server,
        "_broadcaster",
        ConversationBroadcaster(
            {
                message_store.WEB_PLATFORM: WebAdapter(),
                message_store.SLACK_PLATFORM: SlackAdapter(fake_slack),
            }
        ),
    )
    scheduled = []
    monkeypatch.setattr(
        web_server.asyncio,
        "create_task",
        lambda coro: scheduled.append(coro) or type("_DummyTask", (), {})(),
    )
    return scheduled


def test_session_send_rejects_unknown_conversation_id(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    fake_slack = _FakeSlackClient()
    _setup_session_send_runtime(monkeypatch, fake_slack)

    with pytest.raises(ValueError, match="does not refer to an existing session"):
        asyncio.run(web_server.session_send("does-not-exist", "hello"))


def test_session_send_rejects_empty_text(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conversation_id = message_store.get_or_create_conversation(None)
    fake_slack = _FakeSlackClient()
    _setup_session_send_runtime(monkeypatch, fake_slack)

    with pytest.raises(ValueError, match="text must be non-empty"):
        asyncio.run(web_server.session_send(conversation_id, "   "))


def test_session_send_happy_path_delivers_into_existing_session(tmp_path, monkeypatch):
    """Core contract: given an existing conversation_id and text, the
    message is recorded against that SAME conversation (not a new one) and
    the turn is dispatched in the background, mirroring session_create()'s
    own "return immediately, don't block on the turn" behavior."""
    _use_tmp_store(tmp_path, monkeypatch)
    conversation_id = message_store.get_or_create_conversation(None)
    fake_slack = _FakeSlackClient()
    scheduled = _setup_session_send_runtime(monkeypatch, fake_slack)
    monkeypatch.setattr(copilot_session, "_convos", {})

    result = asyncio.run(web_server.session_send(conversation_id, "resume the task"))
    for coro in scheduled:
        coro.close()

    assert result["conversation_id"] == conversation_id
    assert result["web_url"] == f"http://{web_server.WEB_UI_PUBLIC_HOST}:{web_server.WEB_UI_PORT}/#open={conversation_id}"
    history = message_store.get_messages(conversation_id)
    assert any(m["role"] == "user" and m["content"] == "resume the task" for m in history)


def test_session_send_mirror_false_stays_web_ui_only(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conversation_id = message_store.get_or_create_conversation(None)
    fake_slack = _FakeSlackClient()
    _setup_session_send_runtime(monkeypatch, fake_slack)
    monkeypatch.setattr(copilot_session, "_convos", {})

    asyncio.run(web_server.session_send(conversation_id, "quiet nudge", mirror=False))

    assert fake_slack.posted == []


def test_session_send_to_idle_target_delivers_text_unmodified(tmp_path, monkeypatch):
    """An idle target gets exactly the caller's text, with no busy-context
    note appended - that framing is only relevant when a real in-progress
    turn is being interrupted."""
    _use_tmp_store(tmp_path, monkeypatch)
    conversation_id = message_store.get_or_create_conversation(None)
    fake_slack = _FakeSlackClient()
    recorded = []
    scheduled = _setup_session_send_runtime(monkeypatch, fake_slack, record_into=recorded)
    monkeypatch.setattr(copilot_session, "_convos", {conversation_id: _FakeBusyConvo(busy=False)})

    async def _run():
        await web_server.session_send(conversation_id, "quick question")
        for coro in scheduled:
            await coro

    asyncio.run(_run())

    assert len(recorded) == 1
    assert recorded[0]["text"] == "quick question"


def test_session_send_to_busy_target_with_interrupt_true_appends_busy_context_note(tmp_path, monkeypatch):
    """Default behavior (interrupt=True): sending into a busy target aborts
    its in-flight turn (same abort-on-new-message semantics as any real
    follow-up), but the injected text is enriched with a note stating the
    new message supersedes the interrupted task - mirrors watchers.py's
    _inject_watch_message() busy-check, so the interrupted work isn't
    silently dropped without any trace in the conversation history. The wording
    says "supersedes" (not "handle this first, then finish that task") so the
    agent switches to the new request instead of finishing the old one."""
    _use_tmp_store(tmp_path, monkeypatch)
    conversation_id = message_store.get_or_create_conversation(None)
    message_store.record_message(conversation_id, "user", "working on the flaky test fix")
    fake_slack = _FakeSlackClient()
    recorded = []
    scheduled = _setup_session_send_runtime(monkeypatch, fake_slack, record_into=recorded)
    monkeypatch.setattr(copilot_session, "_convos", {conversation_id: _FakeBusyConvo(busy=True)})

    async def _run():
        await web_server.session_send(conversation_id, "check the other MR first")
        for coro in scheduled:
            await coro

    asyncio.run(_run())

    assert len(recorded) == 1
    sent_text = recorded[0]["text"]
    assert sent_text.startswith("check the other MR first")
    assert "supersedes" in sent_text
    assert "working on the flaky test fix" in sent_text


def test_session_send_to_busy_target_with_interrupt_false_raises_without_sending(tmp_path, monkeypatch):
    """interrupt=False is the opt-out for a caller that wants to wait its
    turn rather than preempt a live one - raises instead of ever calling
    send_message(), so nothing is recorded/dispatched for the busy target."""
    _use_tmp_store(tmp_path, monkeypatch)
    conversation_id = message_store.get_or_create_conversation(None)
    message_store.record_message(conversation_id, "user", "working on the flaky test fix")
    fake_slack = _FakeSlackClient()
    recorded = []
    scheduled = _setup_session_send_runtime(monkeypatch, fake_slack, record_into=recorded)
    monkeypatch.setattr(copilot_session, "_convos", {conversation_id: _FakeBusyConvo(busy=True)})

    with pytest.raises(ValueError, match="is busy"):
        asyncio.run(web_server.session_send(conversation_id, "check the other MR first", interrupt=False))

    assert recorded == []
    assert scheduled == []


def test_session_pin_tool_pins_existing_session(tmp_path, monkeypatch):
    """The session_pin MCP tool pins an already-created session, e.g. a recurring
    'Sprint status' session created earlier in the same conversation."""
    _use_tmp_store(tmp_path, monkeypatch)
    conversation_id = message_store.get_or_create_conversation(None)

    result = web_server.session_pin(conversation_id)

    assert result == {"conversation_id": conversation_id, "pinned": True}
    convo = message_store.get_conversation(conversation_id)
    assert convo["pinned"] == 1


def test_session_pin_tool_can_unpin(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conversation_id = message_store.get_or_create_conversation(None)
    message_store.set_pinned(conversation_id, True)

    result = web_server.session_pin(conversation_id, pinned=False)

    assert result == {"conversation_id": conversation_id, "pinned": False}
    convo = message_store.get_conversation(conversation_id)
    assert convo["pinned"] == 0


def test_session_pin_tool_rejects_unknown_session_id(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)

    try:
        web_server.session_pin("does-not-exist")
        raised = False
    except ValueError:
        raised = True

    assert raised is True


def _use_tmp_watches_db(tmp_path, monkeypatch):
    import watchers

    monkeypatch.setattr(watchers, "DB_PATH", tmp_path / "watchers.db")
    monkeypatch.setattr(watchers, "check_watch_resource_exists", lambda *a, **kw: None)
    watchers.init_db()
    return watchers


def test_watches_endpoint_lists_active_watches(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    watchers = _use_tmp_watches_db(tmp_path, monkeypatch)
    conversation_id = _slack_conversation_id("C123", "111.222")
    watchers.add_watch("time", conversation_id, key="in:5m", label="My MR", announce=False)

    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.get("/api/watches")

    assert resp.status_code == 200
    watches = resp.json()["watches"]
    assert len(watches) == 1
    assert watches[0]["type"] == "time"
    assert watches[0]["label"] == "My MR"
    assert watches[0]["status"] == "active"


def test_watches_endpoint_excludes_done_by_default(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    watchers = _use_tmp_watches_db(tmp_path, monkeypatch)
    conversation_id = _slack_conversation_id("C123", "111.222")
    watch_id = watchers.add_watch("time", conversation_id, key="in:5m", label="reminder", announce=False)
    watchers.set_status(watch_id, "done")

    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.get("/api/watches")
    assert resp.json()["watches"] == []

    resp2 = client.get("/api/watches?include_inactive=true")
    assert len(resp2.json()["watches"]) == 1


def test_watches_endpoint_requires_auth(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    _use_tmp_watches_db(tmp_path, monkeypatch)

    client = TestClient(web_server.app)
    resp = client.get("/api/watches")

    assert resp.status_code == 401


def test_watches_endpoint_filters_by_conversation_id(tmp_path, monkeypatch):
    """P0 feature (conversation-owned watchers): passing ?conversation_id=...
    scopes the list to watches registered for that bot conversation, which
    is now the watch system's stable identity even when a Slack thread is
    absent or later rebound."""
    _use_tmp_store(tmp_path, monkeypatch)
    watchers = _use_tmp_watches_db(tmp_path, monkeypatch)
    conversation_id_a = _slack_conversation_id("C123", "111.222")
    conversation_id_b = _slack_conversation_id("C123", "333.444")
    watchers.add_watch("time", conversation_id_a, key="in:5m", label="thread A watch", announce=False)
    watchers.add_watch("time", conversation_id_b, key="in:6m", label="thread B watch", announce=False)

    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.get(f"/api/watches?conversation_id={conversation_id_a}")

    assert resp.status_code == 200
    watches = resp.json()["watches"]
    assert len(watches) == 1
    assert watches[0]["label"] == "thread A watch"
    assert watches[0]["thread_ts"] == "111.222"


def test_watches_endpoint_conversation_id_and_include_inactive_combine(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    watchers = _use_tmp_watches_db(tmp_path, monkeypatch)
    conversation_id = _slack_conversation_id("C123", "111.222")
    watch_id = watchers.add_watch("time", conversation_id, key="in:5m", label="reminder", announce=False)
    watchers.set_status(watch_id, "done")
    other_conversation_id = _slack_conversation_id("C123", "333.444")
    watchers.add_watch("time", other_conversation_id, key="in:7m", label="other thread", announce=False)

    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.get(f"/api/watches?conversation_id={conversation_id}")
    assert resp.json()["watches"] == []

    resp2 = client.get(f"/api/watches?conversation_id={conversation_id}&include_inactive=true")
    watches = resp2.json()["watches"]
    assert len(watches) == 1
    assert watches[0]["label"] == "reminder"


def test_watch_pause_resume_endpoints_flip_status(tmp_path, monkeypatch):
    """Inline Watches-panel row actions: POST .../pause and
    .../resume let the Web UI flip a watch's status without a round-trip
    through the agent, reusing the exact same mcp_tools.watch_pause/
    watch_resume the agent's own MCP tools call."""
    _use_tmp_store(tmp_path, monkeypatch)
    watchers = _use_tmp_watches_db(tmp_path, monkeypatch)
    conversation_id = _slack_conversation_id("C123", "111.222")
    watch_id = watchers.add_watch("time", conversation_id, key="in:5m", label="my mr", announce=False)

    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.post(f"/api/watches/{watch_id}/pause")
    assert resp.status_code == 200
    assert resp.json()["status"] == "paused"

    resp2 = client.post(f"/api/watches/{watch_id}/resume")
    assert resp2.status_code == 200
    assert resp2.json()["status"] == "active"


def test_watch_pause_endpoint_404s_for_unknown_watch(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    _use_tmp_watches_db(tmp_path, monkeypatch)

    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.post("/api/watches/999999/pause")
    assert resp.status_code == 404


def test_watch_delete_endpoint_removes_watch(tmp_path, monkeypatch):
    """DELETE .../{id} backs the Watches panel's inline remove
    row action (behind a confirm in the UI) — permanently drops the row,
    verified here by re-listing with include_inactive=true (removed, not
    just marked done, so it shouldn't appear at all)."""
    _use_tmp_store(tmp_path, monkeypatch)
    watchers = _use_tmp_watches_db(tmp_path, monkeypatch)
    conversation_id = _slack_conversation_id("C123", "111.222")
    watch_id = watchers.add_watch("time", conversation_id, key="in:5m", label="t", announce=False)

    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.delete(f"/api/watches/{watch_id}")
    assert resp.status_code == 200
    assert resp.json() == {"removed": True}

    resp2 = client.get("/api/watches?include_inactive=true")
    assert resp2.json()["watches"] == []


def test_watch_recheck_endpoint_runs_poll_fn_immediately(tmp_path, monkeypatch):
    """POST .../recheck ('re-check now' row action) calls
    watchers.poll_once() for that single watch right away instead of
    waiting for the next watcher_loop() tick — verified by monkeypatching
    the type's poll function and asserting it was actually invoked with
    this watch."""
    _use_tmp_store(tmp_path, monkeypatch)
    watchers = _use_tmp_watches_db(tmp_path, monkeypatch)
    conversation_id = _slack_conversation_id("C123", "111.222")
    watch_id = watchers.add_watch("time", conversation_id, key="in:5m", label="my mr", announce=False)

    called_with = []

    async def _fake_poll_time_watch(w):
        called_with.append(w["id"])

    monkeypatch.setattr(watchers, "_poll_time_watch", _fake_poll_time_watch)

    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.post(f"/api/watches/{watch_id}/recheck")
    assert resp.status_code == 200
    assert called_with == [watch_id]


def test_watch_recheck_endpoint_rejects_self_restart_type(tmp_path, monkeypatch):
    """self_restart watches are resolved once at process boot (see
    watchers.poll_once()'s docstring) — recheck must 400, not silently
    no-op or 500."""
    _use_tmp_store(tmp_path, monkeypatch)
    watchers = _use_tmp_watches_db(tmp_path, monkeypatch)
    conversation_id = _slack_conversation_id("C123", "111.222")
    watch_id = watchers.add_watch("self_restart", conversation_id, label="restart watch", announce=False)

    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.post(f"/api/watches/{watch_id}/recheck")
    assert resp.status_code == 400


def test_watch_recheck_endpoint_rejects_paused_watch(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    watchers = _use_tmp_watches_db(tmp_path, monkeypatch)
    conversation_id = _slack_conversation_id("C123", "111.222")
    watch_id = watchers.add_watch("time", conversation_id, key="in:5m", label="my mr", announce=False)
    watchers.set_status(watch_id, "paused")

    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.post(f"/api/watches/{watch_id}/recheck")
    assert resp.status_code == 400


def test_watch_preview_endpoint_returns_none_type_for_time_watch(tmp_path, monkeypatch):
    """time/teams/self_restart watches have no single external resource to
    preview — the endpoint should return {"type": null} quietly (same
    "nothing to show" contract as api_unfurl()), not error."""
    _use_tmp_store(tmp_path, monkeypatch)
    watchers = _use_tmp_watches_db(tmp_path, monkeypatch)
    conversation_id = _slack_conversation_id("C123", "111.222")
    watch_id = watchers.add_watch("time", conversation_id, key="in:5m", label="reminder", announce=False)
    web_server._unfurl_cache.clear()

    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.get(f"/api/watches/{watch_id}/preview")
    assert resp.status_code == 200
    assert resp.json() == {"type": None}


def test_login_cookie_expires_after_12_hours(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    monkeypatch.setattr(web_server, "WEB_UI_PASSWORD", "secret123")

    client = TestClient(web_server.app)
    resp = client.post("/api/login", json={"password": "secret123"})
    assert resp.status_code == 200

    cookie = next(c for c in client.cookies.jar if c.name == web_server._COOKIE_NAME)
    max_age = cookie.expires - int(__import__("time").time())

    assert web_server._COOKIE_MAX_AGE == 60 * 60 * 12
    # Allow generous slack for test execution time.
    assert 60 * 60 * 11 < max_age <= 60 * 60 * 12


def test_login_locks_out_after_too_many_wrong_passwords(tmp_path, monkeypatch):
    """Unlimited automated password guessing is blocked -
    after _LOGIN_MAX_ATTEMPTS wrong passwords from the same client, further
    attempts get 429 even with the CORRECT password, until the window
    expires."""
    _use_tmp_store(tmp_path, monkeypatch)
    monkeypatch.setattr(web_server, "WEB_UI_PASSWORD", "secret123")
    web_server._login_failures.clear()

    client = TestClient(web_server.app)

    for _ in range(web_server._LOGIN_MAX_ATTEMPTS):
        resp = client.post("/api/login", json={"password": "wrong"})
        assert resp.status_code == 403

    # One more attempt (even with the right password) is locked out.
    resp = client.post("/api/login", json={"password": "secret123"})
    assert resp.status_code == 429


def test_login_rate_limit_resets_after_successful_login(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    monkeypatch.setattr(web_server, "WEB_UI_PASSWORD", "secret123")
    web_server._login_failures.clear()

    client = TestClient(web_server.app)
    for _ in range(web_server._LOGIN_MAX_ATTEMPTS - 1):
        resp = client.post("/api/login", json={"password": "wrong"})
        assert resp.status_code == 403

    # Still under the limit — correct password succeeds and clears history.
    resp = client.post("/api/login", json={"password": "secret123"})
    assert resp.status_code == 200
    assert all(not attempts for attempts in web_server._login_failures.values())



def test_login_rate_limit_is_per_ip_not_global(tmp_path, monkeypatch):
    """Attempts recorded under one client IP must not lock out a different
    IP — the failure tracker is keyed by request.client.host."""
    _use_tmp_store(tmp_path, monkeypatch)
    monkeypatch.setattr(web_server, "WEB_UI_PASSWORD", "secret123")
    web_server._login_failures.clear()
    web_server._login_failures["10.0.0.1"] = [__import__("time").time()] * web_server._LOGIN_MAX_ATTEMPTS

    client = TestClient(web_server.app)
    # TestClient's default client host is 127.0.0.1 (a different IP), so
    # this request should not be affected by the pre-seeded lockout above.
    resp = client.post("/api/login", json={"password": "secret123"})
    assert resp.status_code == 200


def test_ensure_persisted_secret_key_generates_and_writes_when_unset(tmp_path, monkeypatch):
    """An unset WEB_UI_SECRET_KEY is generated ONCE and
    persisted to .env, not regenerated in memory on every process
    start (which would invalidate every open Web UI login session on
    every bot restart)."""
    env_file = tmp_path / ".env"
    env_file.write_text("SOME_OTHER_VAR=1\n")
    monkeypatch.setattr(web_server, "_ENV_FILE", env_file)
    monkeypatch.delenv("WEB_UI_SECRET_KEY", raising=False)

    key = web_server._ensure_persisted_secret_key()

    assert len(key) == 64  # secrets.token_hex(32) -> 64 hex chars
    assert f"WEB_UI_SECRET_KEY={key}" in env_file.read_text()
    assert os.environ["WEB_UI_SECRET_KEY"] == key


def test_ensure_persisted_secret_key_reuses_existing_env_value(monkeypatch):
    monkeypatch.setenv("WEB_UI_SECRET_KEY", "already-set-value")
    key = web_server._ensure_persisted_secret_key()
    assert key == "already-set-value"


def test_ensure_persisted_secret_key_replaces_blank_line_in_place(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("WEB_UI_SECRET_KEY=\nOTHER=2\n")
    monkeypatch.setattr(web_server, "_ENV_FILE", env_file)
    monkeypatch.delenv("WEB_UI_SECRET_KEY", raising=False)

    key = web_server._ensure_persisted_secret_key()

    content = env_file.read_text()
    assert f"WEB_UI_SECRET_KEY={key}" in content
    assert "OTHER=2" in content
    assert content.count("WEB_UI_SECRET_KEY=") == 1


def test_index_page_serves_cache_busted_static_asset_urls(tmp_path, monkeypatch):
    """/static/app.js and /static/style.css are referenced with a
    ?v=<mtime> cache-busting suffix, so a browser tab left open across a bot
    restart always fetches fresh assets after any change to these files."""
    _use_tmp_store(tmp_path, monkeypatch)
    _login_client = TestClient(web_server.app)
    _login(_login_client, monkeypatch)

    resp = _login_client.get("/")

    assert resp.status_code == 200
    body = resp.text
    assert 'src="/static/app.js?v=' in body
    assert 'href="/static/style.css?v=' in body


def test_index_page_404s_when_frontend_disabled(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    monkeypatch.setattr(web_server, "WEB_UI_FRONTEND_ENABLED", False)
    _login_client = TestClient(web_server.app)
    _login(_login_client, monkeypatch)

    resp = _login_client.get("/")

    assert resp.status_code == 404


def test_login_page_404s_when_frontend_disabled(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    monkeypatch.setattr(web_server, "WEB_UI_FRONTEND_ENABLED", False)
    client = TestClient(web_server.app)

    resp = client.get("/login")

    assert resp.status_code == 404


def test_api_endpoints_still_work_when_frontend_disabled(tmp_path, monkeypatch):
    """The whole point of WEB_UI_FRONTEND_ENABLED=false: /api/* keeps
    working for a headless deployment even though the browser frontend
    itself (/, /login, /static/*) is disabled."""
    _use_tmp_store(tmp_path, monkeypatch)
    monkeypatch.setattr(web_server, "WEB_UI_FRONTEND_ENABLED", False)
    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.get("/api/sessions")

    assert resp.status_code == 200


class _FakeSlackClientForHealth:
    def __init__(self, ok: bool = True):
        self._ok = ok

    async def auth_test(self):
        return {"ok": self._ok}


class _FakeSlackClientRaises:
    async def auth_test(self):
        raise RuntimeError("network down")


def test_health_endpoint_reports_slack_connected(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    monkeypatch.setattr(web_server, "_slack_client", _FakeSlackClientForHealth(ok=True))

    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["slack_connected"] is True
    assert isinstance(body["uptime_seconds"], (int, float))
    assert body["uptime_seconds"] >= 0
    assert isinstance(body["bot_version"], str) and body["bot_version"]
    assert isinstance(body["mcp_servers"], list)
    assert isinstance(body["mcp_configured"], bool)
    assert isinstance(body["mcp_status"], list)
    # Always includes at least this bot's own self-hosted /mcp entry.
    assert any("this bot" in e["name"] for e in body["mcp_status"])
    for entry in body["mcp_status"]:
        assert set(entry.keys()) == {"name", "type", "status", "detail"}
        assert entry["status"] in ("ok", "warning", "error", "unknown")


def test_health_endpoint_reports_slack_disconnected_on_error(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    monkeypatch.setattr(web_server, "_slack_client", _FakeSlackClientRaises())

    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["slack_connected"] is False


def test_health_endpoint_reports_disconnected_without_slack_client(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    monkeypatch.setattr(web_server, "_slack_client", None)

    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.get("/api/health")
    assert resp.json()["slack_connected"] is False


def test_health_endpoint_requires_auth(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)

    client = TestClient(web_server.app)
    resp = client.get("/api/health")

    assert resp.status_code == 401


class _FakeConvoWithTurnTiming:
    """Stand-in exercising web_server._running_since()'s use of
    convo._lock.locked() and convo.turn_started_at (see
    copilot_session.CopilotConversation)."""

    def __init__(self, running: bool, turn_started_at: float | None):
        self._lock = _FakeLock(locked=running)
        self.turn_started_at = turn_started_at


class _FakeLock:
    def __init__(self, locked: bool):
        self._locked = locked

    def locked(self) -> bool:
        return self._locked


def test_sessions_list_reports_turn_started_at_for_running_session(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv_id = message_store.get_or_create_conversation(None)
    convo = _FakeConvoWithTurnTiming(running=True, turn_started_at=12345.0)
    monkeypatch.setattr(web_server, "_convos", {conv_id: convo})

    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.get("/api/sessions")
    assert resp.status_code == 200
    session = next(s for s in resp.json()["sessions"] if s["conversation_id"] == conv_id)
    assert session["running"] is True
    assert session["turn_started_at"] == 12345.0


def test_sessions_list_reports_no_turn_started_at_when_idle(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv_id = message_store.get_or_create_conversation(None)
    convo = _FakeConvoWithTurnTiming(running=False, turn_started_at=None)
    monkeypatch.setattr(web_server, "_convos", {conv_id: convo})

    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.get("/api/sessions")
    session = next(s for s in resp.json()["sessions"] if s["conversation_id"] == conv_id)
    assert session["running"] is False
    assert session["turn_started_at"] is None


def test_transcript_reports_turn_started_at_for_running_session(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv_id = message_store.get_or_create_conversation(None)
    convo = _FakeConvoWithTurnTiming(running=True, turn_started_at=99.5)
    monkeypatch.setattr(web_server, "_convos", {conv_id: convo})

    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.get(f"/api/sessions/{conv_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["running"] is True
    assert body["turn_started_at"] == 99.5


def test_health_endpoint_reports_session_timeout_seconds(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    monkeypatch.setattr(web_server, "_slack_client", _FakeSlackClientForHealth(ok=True))
    monkeypatch.setattr(web_server.bot_config, "SESSION_TIMEOUT", 1800.0)

    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["session_timeout_seconds"] == 1800.0


def test_health_endpoint_reports_configured_workdir(tmp_path, monkeypatch):
    """The Copilot working directory shown next to a session's session_id (with
    a copy button) is a fixed, global constant (COPILOT_WORKDIR), not
    per-session, so it's surfaced via /api/health alongside the other
    global bot config fields."""
    _use_tmp_store(tmp_path, monkeypatch)
    monkeypatch.setattr(web_server, "_slack_client", _FakeSlackClientForHealth(ok=True))
    monkeypatch.setattr(web_server.bot_config, "COPILOT_WORKDIR", "/tmp/example-workdir")

    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["workdir"] == "/tmp/example-workdir"


def test_sessions_list_flags_session_whose_last_turn_timed_out(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv_id = message_store.get_or_create_conversation(None)
    message_store.record_message(conv_id, "user", "do the thing")
    message_store.record_message(
        conv_id,
        "assistant",
        ":warning: DOP Agent request timed out (a tool call likely got stuck). "
        "Your thread history is preserved — send another message to resume.",
    )

    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.get("/api/sessions")
    session = next(s for s in resp.json()["sessions"] if s["conversation_id"] == conv_id)
    assert session["errored"] is True


def test_sessions_list_flags_session_whose_last_turn_errored(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv_id = message_store.get_or_create_conversation(None)
    message_store.record_message(conv_id, "user", "do the thing")
    message_store.record_message(conv_id, "assistant", ":x: Error: boom")

    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.get("/api/sessions")
    session = next(s for s in resp.json()["sessions"] if s["conversation_id"] == conv_id)
    assert session["errored"] is True


def test_sessions_list_does_not_flag_a_successful_session(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv_id = message_store.get_or_create_conversation(None)
    message_store.record_message(conv_id, "user", "do the thing")
    message_store.record_message(conv_id, "assistant", "all good, done!")

    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.get("/api/sessions")
    session = next(s for s in resp.json()["sessions"] if s["conversation_id"] == conv_id)
    assert session["errored"] is False


def test_sessions_list_does_not_flag_an_error_looking_session_while_still_running(tmp_path, monkeypatch):
    # A running turn should never show the errored flag, even if an earlier
    # turn in the same session happened to end in an error - the flag is
    # about the session's current resting state, not its full history.
    _use_tmp_store(tmp_path, monkeypatch)
    conv_id = message_store.get_or_create_conversation(None)
    message_store.record_message(conv_id, "user", "do the thing")
    message_store.record_message(conv_id, "assistant", ":x: Error: boom")
    message_store.record_message(conv_id, "user", "try again")
    convo = _FakeConvoWithTurnTiming(running=True, turn_started_at=1.0)
    monkeypatch.setattr(web_server, "_convos", {conv_id: convo})

    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.get("/api/sessions")
    session = next(s for s in resp.json()["sessions"] if s["conversation_id"] == conv_id)
    assert session["errored"] is False



def test_search_endpoint_returns_matching_messages(tmp_path, monkeypatch):
    """Full-text search across sessions/messages. Confirms
    GET /api/search?q=... surfaces message_store.search_messages() results
    over the REST API the Web UI's sidebar search box calls."""
    _use_tmp_store(tmp_path, monkeypatch)
    conv_id = message_store.get_or_create_conversation(None)
    message_store.record_message(conv_id, "user", "please bump example-app to 0.19.3")

    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.get("/api/search", params={"q": "example-app"})

    assert resp.status_code == 200
    results = resp.json()["results"]
    assert any(r["conversation_id"] == conv_id for r in results)


def test_search_endpoint_requires_auth(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    client = TestClient(web_server.app)

    resp = client.get("/api/search", params={"q": "anything"})

    assert resp.status_code == 401


def test_search_endpoint_empty_query_returns_no_results(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv_id = message_store.get_or_create_conversation(None)
    message_store.record_message(conv_id, "user", "hello world")

    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.get("/api/search", params={"q": ""})

    assert resp.status_code == 200
    assert resp.json()["results"] == []


def test_archive_endpoint_marks_session_archived(tmp_path, monkeypatch):
    """Archive/hide session. Confirms PUT
    /api/sessions/{id}/archive persists via message_store.set_archived()
    and the session then reports archived=True."""
    _use_tmp_store(tmp_path, monkeypatch)
    conv_id = message_store.get_or_create_conversation(None)

    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.put(f"/api/sessions/{conv_id}/archive", json={"archived": True})

    assert resp.status_code == 200
    assert resp.json() == {"archived": True}
    assert message_store.get_conversation(conv_id)["archived"] == 1


def test_archive_endpoint_can_unarchive(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv_id = message_store.get_or_create_conversation(None)
    message_store.set_archived(conv_id, True)

    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.put(f"/api/sessions/{conv_id}/archive", json={"archived": False})

    assert resp.status_code == 200
    assert resp.json() == {"archived": False}
    assert message_store.get_conversation(conv_id)["archived"] == 0


def test_archive_endpoint_requires_auth(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv_id = message_store.get_or_create_conversation(None)
    client = TestClient(web_server.app)

    resp = client.put(f"/api/sessions/{conv_id}/archive", json={"archived": True})

    assert resp.status_code == 401


def test_archive_endpoint_404s_for_unknown_conversation(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.put("/api/sessions/does-not-exist/archive", json={"archived": True})

    assert resp.status_code == 404


# --------------------------------------------------------------------------
# /api/sessions/{id}/note (private per-session note-to-self)
# --------------------------------------------------------------------------


def test_note_endpoint_defaults_to_empty_string(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv_id = message_store.get_or_create_conversation(None)
    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.get(f"/api/sessions/{conv_id}/note")

    assert resp.status_code == 200
    assert resp.json() == {"note": ""}


def test_note_endpoint_set_then_get_round_trips(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv_id = message_store.get_or_create_conversation(None)
    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    put_resp = client.put(f"/api/sessions/{conv_id}/note", json={"note": "waiting on Arunas' approval"})
    get_resp = client.get(f"/api/sessions/{conv_id}/note")

    assert put_resp.status_code == 200
    assert put_resp.json() == {"note": "waiting on Arunas' approval"}
    assert get_resp.status_code == 200
    assert get_resp.json() == {"note": "waiting on Arunas' approval"}


def test_note_endpoint_does_not_touch_updated_at(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv_id = message_store.get_or_create_conversation(None)
    before = message_store.get_conversation(conv_id)["updated_at"]
    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    client.put(f"/api/sessions/{conv_id}/note", json={"note": "a private thought"})

    after = message_store.get_conversation(conv_id)["updated_at"]
    assert after == before


def test_note_endpoint_get_requires_auth(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv_id = message_store.get_or_create_conversation(None)
    client = TestClient(web_server.app)

    resp = client.get(f"/api/sessions/{conv_id}/note")

    assert resp.status_code == 401


def test_note_endpoint_put_requires_auth(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv_id = message_store.get_or_create_conversation(None)
    client = TestClient(web_server.app)

    resp = client.put(f"/api/sessions/{conv_id}/note", json={"note": "x"})

    assert resp.status_code == 401


def test_note_endpoint_get_404s_for_unknown_conversation(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.get("/api/sessions/does-not-exist/note")

    assert resp.status_code == 404


def test_note_endpoint_put_404s_for_unknown_conversation(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.put("/api/sessions/does-not-exist/note", json={"note": "x"})

    assert resp.status_code == 404


def test_note_endpoint_clearing_note_sets_it_back_to_empty(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv_id = message_store.get_or_create_conversation(None)
    client = TestClient(web_server.app)
    _login(client, monkeypatch)
    client.put(f"/api/sessions/{conv_id}/note", json={"note": "something"})

    resp = client.put(f"/api/sessions/{conv_id}/note", json={"note": ""})

    assert resp.status_code == 200
    assert resp.json() == {"note": ""}
    assert client.get(f"/api/sessions/{conv_id}/note").json() == {"note": ""}


# --------------------------------------------------------------------------
# /api/sessions/{id}/messages/{message_id}/bookmark and
# /api/sessions/{id}/bookmarks
# --------------------------------------------------------------------------


def test_bookmark_endpoint_toggles_and_persists(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv_id = message_store.get_or_create_conversation(None)
    msg_id = message_store.record_message(conv_id, "user", "do the thing")
    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    put_resp = client.put(f"/api/sessions/{conv_id}/messages/{msg_id}/bookmark", json={"bookmarked": True})
    list_resp = client.get(f"/api/sessions/{conv_id}/bookmarks")

    assert put_resp.status_code == 200
    assert put_resp.json() == {"bookmarked": True}
    assert list_resp.status_code == 200
    assert list_resp.json() == {"message_ids": [msg_id]}


def test_bookmark_endpoint_removes_bookmark(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv_id = message_store.get_or_create_conversation(None)
    msg_id = message_store.record_message(conv_id, "user", "do the thing")
    client = TestClient(web_server.app)
    _login(client, monkeypatch)
    client.put(f"/api/sessions/{conv_id}/messages/{msg_id}/bookmark", json={"bookmarked": True})

    resp = client.put(f"/api/sessions/{conv_id}/messages/{msg_id}/bookmark", json={"bookmarked": False})

    assert resp.status_code == 200
    assert resp.json() == {"bookmarked": False}
    assert client.get(f"/api/sessions/{conv_id}/bookmarks").json() == {"message_ids": []}


def test_bookmark_endpoint_requires_auth(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv_id = message_store.get_or_create_conversation(None)
    msg_id = message_store.record_message(conv_id, "user", "do the thing")
    client = TestClient(web_server.app)

    resp = client.put(f"/api/sessions/{conv_id}/messages/{msg_id}/bookmark", json={"bookmarked": True})

    assert resp.status_code == 401


def test_bookmarks_list_endpoint_requires_auth(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv_id = message_store.get_or_create_conversation(None)
    client = TestClient(web_server.app)

    resp = client.get(f"/api/sessions/{conv_id}/bookmarks")

    assert resp.status_code == 401


def test_bookmark_endpoint_404s_for_unknown_conversation(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.put("/api/sessions/does-not-exist/messages/some-msg/bookmark", json={"bookmarked": True})

    assert resp.status_code == 404


def test_bookmarks_list_endpoint_404s_for_unknown_conversation(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.get("/api/sessions/does-not-exist/bookmarks")

    assert resp.status_code == 404


# --------------------------------------------------------------------------
# Server-side read/unread tracking: PUT /api/sessions/{id}/read
# --------------------------------------------------------------------------


def test_mark_read_endpoint_sets_last_read_at(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv_id = message_store.get_or_create_conversation(None)
    message_store.record_message(conv_id, "user", "hello")
    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.put(f"/api/sessions/{conv_id}/read")

    assert resp.status_code == 200
    assert resp.json()["last_read_at"] is not None
    assert message_store.get_last_read(conv_id) == resp.json()["last_read_at"]


def test_mark_read_endpoint_requires_auth(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv_id = message_store.get_or_create_conversation(None)
    client = TestClient(web_server.app)

    resp = client.put(f"/api/sessions/{conv_id}/read")

    assert resp.status_code == 401


def test_mark_read_endpoint_404s_for_unknown_conversation(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.put("/api/sessions/does-not-exist/read")

    assert resp.status_code == 404


def test_sessions_list_endpoint_includes_last_read_at(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv_id = message_store.get_or_create_conversation(None)
    message_store.record_message(conv_id, "user", "hello")
    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    before = next(s for s in client.get("/api/sessions").json()["sessions"] if s["conversation_id"] == conv_id)
    assert before["last_read_at"] is None

    client.put(f"/api/sessions/{conv_id}/read")

    after = next(s for s in client.get("/api/sessions").json()["sessions"] if s["conversation_id"] == conv_id)
    assert after["last_read_at"] is not None


def test_transcript_payload_includes_bookmarked_flag(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv_id = message_store.get_or_create_conversation(None)
    msg_id = message_store.record_message(conv_id, "user", "do the thing")
    client = TestClient(web_server.app)
    _login(client, monkeypatch)
    client.put(f"/api/sessions/{conv_id}/messages/{msg_id}/bookmark", json={"bookmarked": True})

    resp = client.get(f"/api/sessions/{conv_id}")

    assert resp.status_code == 200
    turns = {t["id"]: t for t in resp.json()["turns"]}
    assert turns[msg_id]["bookmarked"] is True


# --------------------------------------------------------------------------
# Voice transcription Web UI parity: POST .../transcript-action,
# POST .../transcribe, and the `actions` field in the transcript payload.
# --------------------------------------------------------------------------


def test_transcript_payload_includes_actions_field(tmp_path, monkeypatch):
    """The transcript-button-parity feature needs the Web UI to know which
    messages have live buttons (Auto-send/Preview/Send to agent) without
    guessing from message content — GET .../sessions/{id} must forward each
    message's `actions` list (from message_store's new `actions` column)
    straight through to the frontend."""
    _use_tmp_store(tmp_path, monkeypatch)
    conv_id = message_store.get_or_create_conversation(None)
    msg_id = message_store.record_message(
        conv_id, "status", ":microphone: ready to send",
        actions=[{"id": "send_transcript_to_copilot", "label": ":arrow_right: Send to agent"}],
    )
    plain_msg_id = message_store.record_message(conv_id, "user", "no buttons here")
    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.get(f"/api/sessions/{conv_id}")

    assert resp.status_code == 200
    turns = {t["id"]: t for t in resp.json()["turns"]}
    assert turns[msg_id]["actions"] == [{"id": "send_transcript_to_copilot", "label": ":arrow_right: Send to agent"}]
    assert turns[plain_msg_id]["actions"] == []


def test_transcript_action_endpoint_dispatches_to_handlers(tmp_path, monkeypatch):
    """POST .../messages/{id}/transcript-action is the Web UI's click target
    for the Auto-send/Preview/Send-to-Copilot buttons rendered from a
    message's `actions` field — it must delegate to
    handlers.web_transcript_action() with the authenticated web-ui user id,
    not reimplement any of that dispatch logic itself."""
    _use_tmp_store(tmp_path, monkeypatch)
    conv_id = message_store.get_or_create_conversation(None)
    msg_id = message_store.record_message(conv_id, "status", ":microphone: ready")
    monkeypatch.setattr(web_server, "_allowed_user_id", "U_WEBUI")

    calls = []

    async def fake_web_transcript_action(action_id, *, conversation_id, message_id, user_id):
        calls.append((action_id, conversation_id, message_id, user_id))
        return True

    monkeypatch.setattr(handlers, "web_transcript_action", fake_web_transcript_action)
    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.post(
        f"/api/sessions/{conv_id}/messages/{msg_id}/transcript-action",
        json={"action_id": "send_transcript_to_copilot"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert calls == [("send_transcript_to_copilot", conv_id, msg_id, "U_WEBUI")]


def test_transcript_action_endpoint_400s_for_unknown_action_id(tmp_path, monkeypatch):
    """An unrecognized action_id (handlers.web_transcript_action() returns
    False) must surface as 400, not silently succeed."""
    _use_tmp_store(tmp_path, monkeypatch)
    conv_id = message_store.get_or_create_conversation(None)
    msg_id = message_store.record_message(conv_id, "status", ":microphone: ready")

    async def fake_web_transcript_action(action_id, *, conversation_id, message_id, user_id):
        return False

    monkeypatch.setattr(handlers, "web_transcript_action", fake_web_transcript_action)
    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.post(
        f"/api/sessions/{conv_id}/messages/{msg_id}/transcript-action",
        json={"action_id": "not_a_real_action"},
    )

    assert resp.status_code == 400


def test_transcript_action_endpoint_requires_auth(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv_id = message_store.get_or_create_conversation(None)
    msg_id = message_store.record_message(conv_id, "status", ":microphone: ready")
    client = TestClient(web_server.app)

    resp = client.post(
        f"/api/sessions/{conv_id}/messages/{msg_id}/transcript-action",
        json={"action_id": "send_transcript_to_copilot"},
    )

    assert resp.status_code == 401


def test_transcript_action_endpoint_404s_for_unknown_conversation(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.post(
        "/api/sessions/does-not-exist/messages/some-msg/transcript-action",
        json={"action_id": "send_transcript_to_copilot"},
    )

    assert resp.status_code == 404


def test_transcribe_upload_endpoint_runs_transcription_in_background(tmp_path, monkeypatch):
    """POST .../transcribe is the Web UI's mic-button upload target (see
    webui/app.js's MediaRecorder wiring): it must hand the raw bytes off to
    handlers.transcribe_web_upload() (the same platform-agnostic core Slack
    voice messages use) and return immediately rather than blocking the HTTP
    response on the full Whisper transcription."""
    _use_tmp_store(tmp_path, monkeypatch)
    conv_id = message_store.get_or_create_conversation(None)
    monkeypatch.setattr(web_server, "_allowed_user_id", "U_WEBUI")

    calls = []

    def fake_start_audio_job(path, *, conversation_id, user_id):
        # The recording must already be on disk when the job is started (save-first).
        calls.append((path.read_bytes(), path.suffix, conversation_id, user_id))

    monkeypatch.setattr(handlers, "start_audio_job", fake_start_audio_job)
    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.post(
        f"/api/sessions/{conv_id}/transcribe",
        files={"audio": ("note.webm", b"fake-audio-bytes", "audio/webm")},
    )

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    assert calls == [(b"fake-audio-bytes", ".webm", conv_id, "U_WEBUI")]


def test_transcribe_upload_endpoint_rejects_oversized_audio(tmp_path, monkeypatch):
    """Guards against the Web UI's mic button uploading an unbounded blob —
    must 413 before ever touching Whisper, mirroring
    transcription.MAX_AUDIO_BYTES (the same limit Slack voice messages are
    held to via transcribe_audio_bytes())."""
    _use_tmp_store(tmp_path, monkeypatch)
    conv_id = message_store.get_or_create_conversation(None)
    monkeypatch.setattr(transcription, "MAX_AUDIO_BYTES", 10)

    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.post(
        f"/api/sessions/{conv_id}/transcribe",
        files={"audio": ("note.webm", b"way more than ten bytes of fake audio", "audio/webm")},
    )

    assert resp.status_code == 413


def test_transcribe_upload_endpoint_requires_auth(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv_id = message_store.get_or_create_conversation(None)
    client = TestClient(web_server.app)

    resp = client.post(
        f"/api/sessions/{conv_id}/transcribe",
        files={"audio": ("note.webm", b"bytes", "audio/webm")},
    )

    assert resp.status_code == 401


def test_transcribe_upload_endpoint_404s_for_unknown_conversation(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    resp = client.post(
        "/api/sessions/does-not-exist/transcribe",
        files={"audio": ("note.webm", b"bytes", "audio/webm")},
    )

    assert resp.status_code == 404


def test_run_in_background_sets_ready_event_only_after_uvicorn_actually_listens(monkeypatch):
    """Auto-resumes of interrupted turns / self_restart watches must not fire
    before web_server.run_in_background()'s uvicorn server has bound its listen
    socket: every bot-spawned session's mcp_servers= includes this process's own
    self-hosted /mcp endpoint (bot_config.SESSION_MCP_SERVERS), and a session
    started while that socket isn't listening gets connection-refused, after
    which the Copilot CLI SDK marks that MCP server unreachable for the rest of
    the session (no retry).

    run_in_background()'s ready_event is only set once uvicorn's
    Server.started flips True (the socket is bound and accepting connections) —
    not merely once the task has begun running — so a caller awaiting the event
    never races ahead of a real listening socket, however long startup takes."""

    class _FakeServer:
        def __init__(self, config):
            self.config = config
            self.started = False

        async def serve(self, sockets=None):
            # Simulate uvicorn's real startup() taking a little while before
            # flipping `started`, then "serving" until cancelled.
            await asyncio.sleep(0.05)
            self.started = True
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                pass

    class _FakeUvicornModule:
        Config = lambda *a, **kw: object()  # noqa: E731 - trivial stand-in, args unused
        Server = _FakeServer

    monkeypatch.setitem(__import__("sys").modules, "uvicorn", _FakeUvicornModule())
    monkeypatch.setattr(web_server, "WEB_UI_PASSWORD", "secret123")

    async def run():
        ready = asyncio.Event()
        task = asyncio.create_task(web_server.run_in_background(ready))

        # Immediately after creating the task, the server has not started
        # yet (the fake sleeps 0.05s first) - the event must NOT be set.
        await asyncio.sleep(0.001)
        assert not ready.is_set(), "ready_event was set before the server actually started listening"

        await asyncio.wait_for(ready.wait(), timeout=2.0)
        assert ready.is_set()

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())


def test_attachment_route_serves_cached_image_and_scopes_to_conversation(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv_a = message_store.get_or_create_conversation(None)
    conv_b = message_store.get_or_create_conversation(None)
    msg_id = message_store.record_message(conv_a, "user", "here's a screenshot")

    fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    local_file = tmp_path / "cached.png"
    local_file.write_bytes(fake_png)

    attachment_id = message_store.record_attachment(
        msg_id, conv_a, kind="image", local_path=str(local_file),
        slack_file_id="F1", filename="cached.png", mimetype="image/png", size=len(fake_png),
    )

    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    # Owning conversation can fetch the file.
    resp = client.get(f"/api/sessions/{conv_a}/attachments/{attachment_id}")
    assert resp.status_code == 200
    assert resp.content == fake_png
    assert resp.headers["content-type"] == "image/png"

    # A different conversation must not be able to read it by id-guessing.
    resp_wrong_conv = client.get(f"/api/sessions/{conv_b}/attachments/{attachment_id}")
    assert resp_wrong_conv.status_code == 404

    # Session-detail payload surfaces the attachment inline on its message.
    transcript = client.get(f"/api/sessions/{conv_a}").json()
    turn = next(t for t in transcript["turns"] if t["id"] == msg_id)
    assert len(turn["attachments"]) == 1
    assert turn["attachments"][0]["filename"] == "cached.png"
    assert turn["attachments"][0]["url"] == f"/api/sessions/{conv_a}/attachments/{attachment_id}"


def test_attachment_route_requires_auth(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    client = TestClient(web_server.app)
    resp = client.get(f"/api/sessions/{message_store.get_or_create_conversation(None)}/attachments/does-not-exist")
    assert resp.status_code == 401


def test_session_create_task_kind_routes_cheap_model_and_persists_it_before_turn(tmp_path, monkeypatch):
    """P3-S5: session_create(task_kind='research') must store the routed model
    on the new conversation BEFORE its turn is dispatched (so
    CopilotConversation._ensure_session passes it to create_session) and
    return model/model_reason to the caller."""
    import model_routing
    _use_tmp_store(tmp_path, monkeypatch)
    scheduled = _setup_session_create_runtime(monkeypatch, _FakeSlackClient())
    monkeypatch.setenv("MODEL_ROUTING", "on")

    async def _avail():
        return frozenset({"claude-haiku-5.5", "claude-sonnet-5.5"})

    monkeypatch.setattr(model_routing, "available_models", _avail)
    monkeypatch.setattr(model_routing, "default_model", lambda: "claude-sonnet-5.5")

    result = asyncio.run(web_server.session_create("summarise yesterday's pipeline failures", task_kind="research"))
    for coro in scheduled:
        coro.close()

    assert result["model"] == "claude-haiku-5.5"
    assert message_store.get_conversation_model(result["conversation_id"]) == "claude-haiku-5.5"
    sys_msgs = [m["content"] for m in message_store.get_messages(result["conversation_id"]) if m["role"] == "system"]
    assert any("Model: claude-haiku-5.5" in t for t in sys_msgs)


def test_session_create_without_task_kind_keeps_default_and_records_reason(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    scheduled = _setup_session_create_runtime(monkeypatch, _FakeSlackClient())
    result = asyncio.run(web_server.session_create("plain task"))
    for coro in scheduled:
        coro.close()
    assert result["model_reason"].startswith("no task_kind")
    assert message_store.get_conversation_model(result["conversation_id"]) is None
    assert message_store.get_conversation(result["conversation_id"])["model_reason"].startswith("no task_kind")


def test_chunked_voice_upload_saves_chunks_to_disk_and_starts_job_on_finish(tmp_path, monkeypatch):
    """Browser sends MediaRecorder timeslice chunks while still recording; each
    lands on disk immediately (so a closed tab/restart keeps the audio) and the
    transcription job only starts on finish."""
    import audio_store
    _use_tmp_store(tmp_path, monkeypatch)
    conv_id = message_store.get_or_create_conversation(None)
    monkeypatch.setattr(web_server, "_allowed_user_id", "U_WEBUI")
    started = []
    monkeypatch.setattr(handlers, "start_audio_job",
                        lambda path, *, conversation_id, user_id: started.append((path, conversation_id, user_id)))
    client = TestClient(web_server.app)
    _login(client, monkeypatch)

    up = client.post(f"/api/sessions/{conv_id}/audio-upload?ext=webm").json()["upload_id"]
    base = f"/api/sessions/{conv_id}/audio-upload/{up}"
    assert client.put(f"{base}/chunk?seq=0", content=b"aa").json() == {"next_seq": 1}
    assert client.put(f"{base}/chunk?seq=0", content=b"aa").json() == {"next_seq": 1}  # retry is idempotent
    assert client.put(f"{base}/chunk?seq=2", content=b"zz").status_code == 409
    assert client.put(f"{base}/chunk?seq=1", content=b"bb").json() == {"next_seq": 2}
    assert started == []

    assert client.post(f"{base}/finish").status_code == 200
    [(path, c, u)] = started
    assert path.read_bytes() == b"aabb" and c == conv_id and u == "U_WEBUI"


def test_chunked_voice_upload_abort_removes_partial_file(tmp_path, monkeypatch):
    import audio_store
    _use_tmp_store(tmp_path, monkeypatch)
    conv_id = message_store.get_or_create_conversation(None)
    client = TestClient(web_server.app)
    _login(client, monkeypatch)
    up = client.post(f"/api/sessions/{conv_id}/audio-upload").json()["upload_id"]
    base = f"/api/sessions/{conv_id}/audio-upload/{up}"
    client.put(f"{base}/chunk?seq=0", content=b"aa")
    assert client.delete(base).json() == {"aborted": True}
    assert list(audio_store._all_jobs()) == []
    assert client.put(f"{base}/chunk?seq=1", content=b"bb").status_code == 404


def test_health_adapters_hide_disabled_slack(tmp_path, monkeypatch):
    """Slack disabled (no slack client) does NOT appear in the
    health `adapters` list; Web always does."""
    _use_tmp_store(tmp_path, monkeypatch)
    monkeypatch.setattr(web_server, "_slack_client", None)
    client = TestClient(web_server.app)
    _login(client, monkeypatch)
    names = [a["name"] for a in client.get("/api/health").json()["adapters"]]
    assert "Slack" not in names and "Web" in names


def test_mutating_api_routes_require_csrf_header_and_same_origin(tmp_path, monkeypatch):
    """CSRF guard (security fix #29): every mutating /api/* route rejects requests lacking
    `X-Requested-With: dop` or with a foreign Origin (403), while reads and /api/login stay open."""
    _use_tmp_store(tmp_path, monkeypatch)
    client = TestClient(web_server.app)
    _login(client, monkeypatch)
    del client.headers["X-Requested-With"]
    assert client.post("/api/sessions/x/stop").status_code == 403
    assert client.delete("/api/groups/g1").status_code == 403
    assert client.post("/api/watches/1/pause").status_code == 403
    assert client.post("/api/sessions/x/stop", headers={"X-Requested-With": "dop", "Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/sessions/x/stop", headers={"X-Requested-With": "dop"}).status_code != 403
    assert client.get("/api/sessions").status_code == 200


def test_login_non_ascii_password_is_a_403_not_a_500_and_logout_revokes_cookie(monkeypatch):
    """Security fix #28/#27: compare_digest on str raised TypeError for non-ASCII input (500);
    now counted as a normal failure. Logout also revokes the signed cookie server-side."""
    monkeypatch.setattr(web_server, "WEB_UI_PASSWORD", "secret123")
    web_server._login_failures.clear()
    client = TestClient(web_server.app)
    assert client.post("/api/login", json={"password": "pässwörd😀"}).status_code == 403
    web_server._login_failures.clear()
    assert client.post("/api/login", json={"password": "secret123"}).status_code == 200
    stolen = client.cookies.get(web_server._COOKIE_NAME)
    assert web_server._cookie_is_valid(stolen)
    assert client.post("/api/logout").status_code == 200
    assert not web_server._cookie_is_valid(stolen)


def test_security_headers_and_secure_cookie_only_on_https(monkeypatch):
    """Strict CSP on responses (security fix #5); session cookie gets Secure only over https (#27)."""
    monkeypatch.setattr(web_server, "WEB_UI_PASSWORD", "secret123")
    web_server._login_failures.clear()
    client = TestClient(web_server.app)
    r = client.post("/api/login", json={"password": "secret123"})
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert "secure" not in r.headers["set-cookie"].lower()
    web_server._login_failures.clear()
    r = TestClient(web_server.app, base_url="https://testserver").post("/api/login", json={"password": "secret123"})
    assert "secure" in r.headers["set-cookie"].lower()


def test_forwarded_for_ignored_unless_peer_is_trusted_proxy(monkeypatch):
    """Throttle bucket (security fix #28): X-Forwarded-For is spoofable, so it only counts from a configured proxy."""
    class _Req:
        client = type("C", (), {"host": "10.0.0.5"})()
        headers = {"x-forwarded-for": "1.2.3.4"}

    assert web_server._client_ip(_Req()) == "10.0.0.5"
    monkeypatch.setattr(web_server, "_TRUSTED_PROXIES", frozenset({"10.0.0.5"}))
    assert web_server._client_ip(_Req()) == "1.2.3.4"
