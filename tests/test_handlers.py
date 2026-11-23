"""
Unit tests for handlers.py — Slack event/action registration, auth gating,
message chunking, and the session-context helper.

The core turn engine (_handle) and voice-transcription glue are exercised
indirectly (through register()/is_authorized/_session_context)
rather than end-to-end, since fully driving _handle() would require faking
most of copilot_session/persistence/message_store together — those are each
already covered by their own test modules. A live Slack app/client is faked
here the same way tests/test_watchers.py fakes them, to avoid pulling in the
whole bot's startup side effects just to test this module.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import handlers
import message_store
import adapters


# --- fakes -------------------------------------------------------------

class FakeClient:
    def __init__(self):
        self.posted = []
        self.permalinks = {}
        self.conversations_info_result = {"channel": {"is_im": True}}
        self.auth_test_result = {"url": "https://example.slack.com/"}

    async def chat_postMessage(self, **kwargs):
        self.posted.append(kwargs)
        return {"ts": "111.222", "channel": kwargs.get("channel")}

    async def conversations_info(self, **kwargs):
        return self.conversations_info_result

    async def auth_test(self):
        return self.auth_test_result

    async def chat_getPermalink(self, **kwargs):
        return {"permalink": self.permalinks.get(kwargs.get("message_ts"), "https://example/perma")}


class FakeApp:
    """Records every @app.action/@app.event registration made by
    handlers.register(app), keyed by (kind, name)."""

    def __init__(self):
        self.registered: dict[tuple[str, str], object] = {}

    def action(self, action_id):
        def decorator(fn):
            self.registered[("action", action_id)] = fn
            return fn
        return decorator

    def event(self, event_name):
        def decorator(fn):
            self.registered[("event", event_name)] = fn
            return fn
        return decorator


@pytest.fixture(autouse=True)
def _isolated_message_store(tmp_path, monkeypatch):
    """Isolate message_store's DB so tests don't write to the developer's
    real bot_store.db."""
    monkeypatch.delenv("ALLOWED_USER_ID", raising=False)
    monkeypatch.delenv("WEB_UI_PASSWORD", raising=False)
    monkeypatch.setattr(message_store, "DB_PATH", tmp_path / "bot_store.db")
    monkeypatch.setattr(message_store, "_TITLES_FILE", tmp_path / "session_titles.json")
    message_store.init_db()
    yield


# --- is_authorized -------------------------------------------------------

def test_is_authorized_matches_allowed_user(monkeypatch):
    monkeypatch.setattr(handlers, "ALLOWED_USER_ID", "U123")
    assert handlers.is_authorized("U123") is True
    assert handlers.is_authorized("U999") is False


def test_is_authorized_fails_closed_when_unset(monkeypatch):
    """A blank/misconfigured ALLOWED_USER_ID denies everyone, so autopilot/--yolo
    code execution is never open by default."""
    monkeypatch.setattr(handlers, "ALLOWED_USER_ID", "")
    assert handlers.is_authorized("anyone") is False


def test_is_authorized_uses_generalized_allowed_identities_table(monkeypatch):
    """Slack auth consults the durable allowed_identities table (seeded from
    ALLOWED_USER_ID) rather than a string compare, so other platforms share the model."""
    monkeypatch.setattr(handlers, "ALLOWED_USER_ID", "U_OWNER")

    assert handlers.is_authorized("U_OWNER") is True
    assert message_store.is_allowed_identity("slack", "U_OWNER") is True
    assert handlers.is_authorized("U_INTRUDER") is False


# --- register() wiring ----------------------------------------------------

def test_register_wires_all_action_and_event_handlers():
    app = FakeApp()
    handlers.register(app)
    assert set(app.registered) == {
        ("action", "stop_working_turn"),
        ("action", "send_transcript_to_copilot"),
        ("action", "auto_send_transcript"),
        ("action", "preview_transcript"),
        ("event", "message"),
        ("event", "app_mention"),
    }


# --- action handlers require authorization --------------------------------

@pytest.mark.asyncio
async def test_stop_working_turn_ignores_unauthorized_user(monkeypatch):
    """Clicking Stop does not act (or even look up the conversation) for a user
    who isn't ALLOWED_USER_ID."""
    monkeypatch.setattr(handlers, "ALLOWED_USER_ID", "U_OWNER")
    app = FakeApp()
    handlers.register(app)
    fn = app.registered[("action", "stop_working_turn")]

    acked = []
    client = FakeClient()
    body = {
        "user": {"id": "U_INTRUDER"},
        "channel": {"id": "C1"},
        "message": {"ts": "100.1"},
        "container": {"thread_ts": "100.1"},
        "actions": [{"value": "some-conversation-id"}],
    }

    async def ack():
        acked.append(True)

    await fn(ack=ack, body=body, client=client)

    assert acked == [True]  # Slack still gets its required ack
    assert client.posted == []  # but no message was posted/updated


@pytest.mark.asyncio
async def test_send_transcript_to_copilot_ignores_unauthorized_user(monkeypatch):
    monkeypatch.setattr(handlers, "ALLOWED_USER_ID", "U_OWNER")
    called = []
    monkeypatch.setattr(handlers, "_send_transcript_buffer_to_copilot", lambda **kw: called.append(kw))

    app = FakeApp()
    handlers.register(app)
    fn = app.registered[("action", "send_transcript_to_copilot")]

    client = FakeClient()
    body = {
        "user": {"id": "U_INTRUDER"},
        "channel": {"id": "C1"},
        "message": {"ts": "100.1"},
        "container": {"thread_ts": "100.1"},
    }

    async def ack():
        pass

    await fn(ack=ack, body=body, client=client)

    assert called == []


@pytest.mark.asyncio
async def test_auto_send_transcript_ignores_unauthorized_user(monkeypatch):
    monkeypatch.setattr(handlers, "ALLOWED_USER_ID", "U_OWNER")
    handlers._transcript_mode_override.clear()

    app = FakeApp()
    handlers.register(app)
    fn = app.registered[("action", "auto_send_transcript")]

    client = FakeClient()
    body = {
        "user": {"id": "U_INTRUDER"},
        "channel": {"id": "C1"},
        "message": {"ts": "100.1"},
        "container": {"thread_ts": "100.1"},
    }

    async def ack():
        pass

    await fn(ack=ack, body=body, client=client)

    assert "100.1" not in handlers._transcript_mode_override
    assert client.posted == []


@pytest.mark.asyncio
async def test_preview_transcript_ignores_unauthorized_user(monkeypatch):
    monkeypatch.setattr(handlers, "ALLOWED_USER_ID", "U_OWNER")
    handlers._transcript_mode_override.clear()

    app = FakeApp()
    handlers.register(app)
    fn = app.registered[("action", "preview_transcript")]

    client = FakeClient()
    body = {
        "user": {"id": "U_INTRUDER"},
        "channel": {"id": "C1"},
        "message": {"ts": "100.1"},
        "container": {"thread_ts": "100.1"},
    }

    async def ack():
        pass

    await fn(ack=ack, body=body, client=client)

    assert "100.1" not in handlers._transcript_mode_override
    assert client.posted == []


# --- _session_context ----------------------------------------------------

@pytest.mark.asyncio
async def test_session_context_builds_full_slack_description():
    client = FakeClient()
    ctx = await handlers._session_context(client, "D123", None, "111.222", "conv-abc")
    assert ctx is not None
    assert "conversation_id: conv-abc" in ctx
    assert "direct message (channel id: D123)" in ctx
    assert "Workspace: https://example.slack.com/" in ctx
    assert "Thread permalink: https://example/perma" in ctx
    assert "Thread root ts: 111.222" in ctx


@pytest.mark.asyncio
async def test_session_context_survives_all_api_failures():
    class BrokenClient:
        async def conversations_info(self, **kwargs):
            raise RuntimeError("no scope")

        async def auth_test(self):
            raise RuntimeError("no scope")

        async def chat_getPermalink(self, **kwargs):
            raise RuntimeError("no scope")

    ctx = await handlers._session_context(BrokenClient(), "D123", None, "111.222", "conv-abc")
    # Falls back to the bare channel id + thread root ts lines only, but the
    # conversation_id line (needed by watch_add/session_get) always survives.
    assert ctx is not None
    assert "conversation_id: conv-abc" in ctx
    assert "D123" in ctx
    assert "Thread root ts: 111.222" in ctx


@pytest.mark.asyncio
async def test_session_context_for_web_ui_only_session_has_no_slack_fields():
    """A Web-UI-only session (channel="", no Slack binding) still gets a usable
    conversation_id line from _session_context, so an agent in it can register
    watches against its own session instead of borrowing another session's
    thread_ts. No Slack API call is attempted."""
    class ExplodingClient:
        async def conversations_info(self, **kwargs):
            raise AssertionError("must not call Slack API for a Web-UI-only session")

        async def auth_test(self):
            raise AssertionError("must not call Slack API for a Web-UI-only session")

    ctx = await handlers._session_context(ExplodingClient(), "", None, "111.222", "conv-web-only")
    assert ctx is not None
    assert "conversation_id: conv-web-only" in ctx
    assert "Web UI-only" in ctx


# --- _send_transcript_buffer_to_copilot ------------------------------------

@pytest.mark.asyncio
async def test_send_transcript_buffer_to_copilot_empty_buffer_posts_warning(monkeypatch):
    client = FakeClient()
    handlers._channel_audio_buffer.clear()
    conversation_id = message_store.get_or_create_conversation("D123")

    await handlers._send_transcript_buffer_to_copilot(
        adapter=adapters.SlackAdapter(client), thread_key={"channel": "D123", "thread_ts": "1.1"},
        message_ref="1.1", conversation_id=conversation_id, user_id="U1",
    )

    assert len(client.posted) == 1
    assert "no longer available" in client.posted[0]["text"]


# --- Web UI voice transcription (adapter-generalized path) -----------------
#
# These exercise transcribe_web_upload()/_transcribe_one(); Web UI voice messages
# reuse the same buffering/placeholder/auto-send glue as Slack — only the
# Adapter passed in differs.

@pytest.mark.asyncio
async def test_transcribe_web_upload_records_transcript_via_web_adapter(monkeypatch):
    """A Web UI voice upload flows through WebAdapter straight into
    message_store (no Slack client involved) and, with the default
    AUDIO_AUTO_SEND=true, forwards the transcript into the Copilot turn the
    same way a Slack voice message does."""
    monkeypatch.setattr(handlers, "AUDIO_AUTO_SEND", True)
    handlers._channel_audio_buffer.clear()
    handlers._transcript_mode_override.clear()

    def fake_whisper(local_path):
        # Transcription works from the persisted file (audio_store), not bytes.
        assert Path(local_path).read_bytes() == b"fake-wav-bytes"
        assert local_path.endswith(".wav")
        return "hello from the web"

    monkeypatch.setattr(handlers.transcription, "_transcribe_audio_file", fake_whisper)

    handled_turns = []

    async def fake_handle(**kwargs):
        handled_turns.append(kwargs)

    monkeypatch.setattr(handlers, "_handle", fake_handle)

    conversation_id = message_store.get_or_create_conversation(None)

    await handlers.transcribe_web_upload(
        b"fake-wav-bytes", "note.wav", conversation_id=conversation_id, user_id="U1",
    )

    # No Slack call is made (no client/channel). The placeholder is updated in
    # place, so exactly one message row exists, with the final ":microphone: ..."
    # content rather than the "Transcribing..." placeholder text.
    stored = message_store.get_messages(conversation_id)
    assert len(stored) == 1
    assert "hello from the web" in stored[0]["content"]
    assert "Transcribing" not in stored[0]["content"]

    # AUDIO_AUTO_SEND=true means the transcript is forwarded to _handle()
    # automatically, same as the Slack path.
    assert len(handled_turns) == 1
    assert handled_turns[0]["text"] == "hello from the web"
    assert handled_turns[0]["conversation_id"] == conversation_id
    assert "client" not in handled_turns[0]  # no Slack client shim reaches the turn engine


@pytest.mark.asyncio
async def test_transcribe_web_upload_manual_mode_shows_send_button_text(monkeypatch):
    """With AUDIO_AUTO_SEND=false, a Web UI upload must behave like Slack's
    manual mode: the transcript is recorded but NOT forwarded to Copilot
    until explicitly sent — _handle() must not be invoked."""
    monkeypatch.setattr(handlers, "AUDIO_AUTO_SEND", False)
    handlers._channel_audio_buffer.clear()
    handlers._transcript_mode_override.clear()

    monkeypatch.setattr(handlers.transcription, "_transcribe_audio_file", lambda local_path: "manual mode text")

    handled_turns = []

    async def fake_handle(**kwargs):
        handled_turns.append(kwargs)

    monkeypatch.setattr(handlers, "_handle", fake_handle)

    conversation_id = message_store.get_or_create_conversation(None)

    await handlers.transcribe_web_upload(
        b"bytes", "note.wav", conversation_id=conversation_id, user_id="U1",
    )

    assert handled_turns == []
    stored = message_store.get_messages(conversation_id)
    contents = [m["content"] for m in stored]
    assert any("manual mode text" in c for c in contents)

    # The final buffered-transcript message carries a "Send to agent" action the
    # Web UI renders as an inline button, keyed by the same message id
    # web_transcript_action() dispatches against below.
    final_msg = next(m for m in stored if "manual mode text" in m["content"])
    assert final_msg["actions"] == [{"id": "send_transcript_to_copilot", "label": ":arrow_right: Send to agent"}]


@pytest.mark.asyncio
async def test_web_transcript_action_send_to_copilot_forwards_buffered_text(monkeypatch):
    """Clicking the Web UI's "Send to agent" button (rendered from the
    message's `actions` field — see test above) must forward the buffered
    transcript to _handle() and clear the button, exactly like Slack's
    send_transcript_to_copilot action handler."""
    monkeypatch.setattr(handlers, "AUDIO_AUTO_SEND", False)
    handlers._channel_audio_buffer.clear()
    handlers._transcript_mode_override.clear()

    monkeypatch.setattr(handlers.transcription, "_transcribe_audio_file", lambda local_path: "click send please")

    handled_turns = []

    async def fake_handle(**kwargs):
        handled_turns.append(kwargs)

    monkeypatch.setattr(handlers, "_handle", fake_handle)

    conversation_id = message_store.get_or_create_conversation(None)
    await handlers.transcribe_web_upload(
        b"bytes", "note.wav", conversation_id=conversation_id, user_id="U1",
    )

    stored = message_store.get_messages(conversation_id)
    final_msg = next(m for m in stored if "click send please" in m["content"])
    assert final_msg["actions"]  # button is present before the click

    handled = await handlers.web_transcript_action(
        "send_transcript_to_copilot", conversation_id=conversation_id,
        message_id=final_msg["id"], user_id="U1",
    )

    assert handled is True
    assert len(handled_turns) == 1
    assert handled_turns[0]["text"] == "click send please"

    updated = message_store.get_messages(conversation_id)
    final_after = next(m for m in updated if m["id"] == final_msg["id"])
    assert final_after["actions"] == []  # button cleared once consumed


@pytest.mark.asyncio
async def test_web_transcript_action_toggle_arms_auto_send_mode(monkeypatch):
    """Clicking the Web UI's "Auto-send" button on a still-transcribing
    placeholder must arm the same `_transcript_mode_override` the Slack
    auto_send_transcript action handler uses, so the next finished
    transcription on that message is forwarded automatically."""
    handlers._channel_audio_buffer.clear()
    handlers._transcript_mode_override.clear()
    conversation_id = message_store.get_or_create_conversation(None)
    message_id = message_store.record_message(
        conversation_id, "status", ":headphones: _Transcribing..._",
        actions=[{"id": "auto_send_transcript", "label": ":zap: Auto-send when ready"}],
    )

    handled = await handlers.web_transcript_action(
        "auto_send_transcript", conversation_id=conversation_id,
        message_id=message_id, user_id="U1",
    )

    assert handled is True
    assert handlers._transcript_mode_override[message_id] == "auto"
    updated = next(m for m in message_store.get_messages(conversation_id) if m["id"] == message_id)
    assert "Will send to the agent automatically" in updated["content"]
    assert updated["actions"] == []


# --- _handle() tests --------------------------------------------------------
#
# These lock down _handle()'s auth-gating, INTERRUPTED/session-reuse-on-abort,
# and new-session announcement + Slack-context injection. A FakeConversation
# stands in for copilot_session.CopilotConversation so _handle() can be driven
# end-to-end without a live Copilot CLI process.

class FakeConversation:
    def __init__(self, response="hello from copilot"):
        self.session_id = "fake-session-id"
        self.model_name = None
        self._response = response
        self.send_calls: list[str] = []
        self.reset_called = False
        self.ensure_ready_called = False

    async def ensure_ready(self):
        self.ensure_ready_called = True

    async def send(self, text, on_status=None):
        self.send_calls.append(text)
        return self._response

    async def reset(self):
        self.reset_called = True

    @property
    def is_alive(self):
        return True

    async def interrupt(self):
        return True


def _make_say(client, channel):
    async def _say(**kwargs):
        return await client.chat_postMessage(channel=channel, **kwargs)
    return _say


@pytest.mark.asyncio
async def test_handle_ignores_unauthorized_user_end_to_end(monkeypatch):
    """Auth-gating must block the turn logic itself, not just is_authorized()
    in isolation: an unauthorized user's message must never reach get_convo()/
    convo.send() and must get no reply at all (silent drop, like Telegram)."""
    monkeypatch.setattr(handlers, "ALLOWED_USER_ID", "U_OWNER")
    convo = FakeConversation()
    monkeypatch.setattr(handlers, "get_convo", lambda cid: convo)
    monkeypatch.setattr(handlers, "_is_new_conversation", lambda cid: True)

    client = FakeClient()
    channel = "D_AUTH"
    await handlers._handle(
        user_id="U_INTRUDER", text="do something", ts="1.1",
        adapter=adapters.SlackAdapter(client), thread_key={"channel": channel},
    )

    assert convo.send_calls == []  # never reached the Copilot turn
    assert client.posted == []


@pytest.mark.asyncio
async def test_voice_transcript_forward_does_not_reject_with_empty_user_id(monkeypatch):
    """A Web UI whisper voice message auto-forwarded via _handle() with a blank
    (Slack-unconfigured) allowed user id is not rejected with "Not authorized".
    Callers already authorize the sender, so the forward passes authorized=True
    and reaches the turn."""
    monkeypatch.setattr(handlers, "ALLOWED_USER_ID", "")
    seen = {}

    async def fake_handle(*a, **kw):
        seen.update(kw)

    monkeypatch.setattr(handlers, "_handle", fake_handle)
    handlers._channel_audio_buffer["c1"] = [{"text": "hello"}]

    class A:
        platform = "web"
        def external_ref(self, r): return None
        def message_ref(self, tk, r): return r

    async def noop(*a, **k): pass
    monkeypatch.setattr(handlers.adapters, "deliver_update", noop)
    await handlers._send_transcript_buffer_to_copilot(
        adapter=A(), thread_key={"conversation_id": "c1"}, message_ref="m1",
        conversation_id="c1", user_id="")
    assert seen.get("authorized") is True


@pytest.mark.asyncio
async def test_web_voice_transcript_auto_sends_even_with_empty_user_id(monkeypatch):
    """With AUDIO_AUTO_SEND on (default), a Web UI voice message is auto-sent
    even though the Web UI's user id is blank when Slack isn't configured; web
    senders are already authenticated, so no manual "Send to agent" button."""
    monkeypatch.setattr(handlers, "AUDIO_AUTO_SEND", True)
    sent = []

    async def fake_send(**kw):
        sent.append(kw)

    monkeypatch.setattr(handlers, "_send_transcript_buffer_to_copilot", fake_send)
    handlers._channel_audio_buffer.pop("cw", None)

    class A:
        platform = "web"

    await handlers._deliver_transcript(
        adapter=A(), thread_key={"conversation_id": "cw"}, conversation_id="cw",
        user_id="", message_ref="m1", buf_key="cw", message_id="m1", reply="hello")
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_handle_interrupted_response_preserves_session(monkeypatch):
    """A turn that returns the INTERRUPTED sentinel must not reset/evict the
    conversation's saved session — the next message should resume the same
    CopilotConversation, not start a fresh one."""
    monkeypatch.setattr(handlers, "ALLOWED_USER_ID", "U_OWNER")
    convo = FakeConversation(response=handlers.INTERRUPTED)
    monkeypatch.setattr(handlers, "get_convo", lambda cid: convo)
    monkeypatch.setattr(handlers, "_is_new_conversation", lambda cid: False)

    client = FakeClient()
    channel = "D_INT"
    conversation_id = message_store.get_or_create_conversation(channel)
    await handlers._handle(
        user_id="U_OWNER", text="keep going", ts="2.1",
        adapter=adapters.SlackAdapter(client), thread_key={"channel": channel},
        conversation_id=conversation_id,
    )

    assert convo.send_calls == ["keep going"]
    assert convo.reset_called is False  # session must survive an interrupt
    # A second turn on the same conversation_id must reuse the same object —
    # nothing in _handle() should have evicted it from _convos.
    assert handlers.get_convo(conversation_id) is convo


@pytest.mark.asyncio
async def test_handle_announces_new_session_once_and_skips_on_repeat(monkeypatch):
    """The "🧵 New session"/Slack-context-injection block must fire exactly
    once, on the first message of a brand new conversation, and be skipped
    on every subsequent message in that same conversation."""
    monkeypatch.setattr(handlers, "ALLOWED_USER_ID", "U_OWNER")
    convo = FakeConversation()
    monkeypatch.setattr(handlers, "get_convo", lambda cid: convo)

    is_new_flags = iter([True, False])
    monkeypatch.setattr(handlers, "_is_new_conversation", lambda cid: next(is_new_flags))

    client = FakeClient()
    channel = "D_NEW"
    conversation_id = message_store.get_or_create_conversation(channel)

    await handlers._handle(
        user_id="U_OWNER", text="first message", ts="3.1",
        adapter=adapters.SlackAdapter(client), thread_key={"channel": channel},
        conversation_id=conversation_id,
    )
    first_turn_texts = [p["text"] for p in client.posted]
    assert any("New session" in t for t in first_turn_texts)
    assert convo.ensure_ready_called is True

    client.posted.clear()
    convo.ensure_ready_called = False
    await handlers._handle(
        user_id="U_OWNER", text="second message", ts="3.2",
        adapter=adapters.SlackAdapter(client), thread_key={"channel": channel},
        conversation_id=conversation_id,
    )
    second_turn_texts = [p["text"] for p in client.posted]
    assert not any("New session" in t for t in second_turn_texts)
    assert convo.ensure_ready_called is False


@pytest.mark.asyncio
async def test_handle_web_only_conversation_updates_single_web_message_without_slack_client(monkeypatch):
    """When a conversation exists only in the Web UI (no Slack binding/client),
    _handle() completes via the WebAdapter path instead of posting Slack replies."""
    monkeypatch.setattr(handlers, "ALLOWED_USER_ID", "U_OWNER")
    convo = FakeConversation(response="web-only answer")
    monkeypatch.setattr(handlers, "get_convo", lambda cid: convo)
    monkeypatch.setattr(handlers, "_is_new_conversation", lambda cid: False)

    conversation_id = message_store.get_or_create_conversation(None)
    message_store.bind_conversation(conversation_id, message_store.WEB_PLATFORM, conversation_id)

    await handlers._handle(
        user_id="U_OWNER",
        text="from web only",
        ts="",
        conversation_id=conversation_id,
        record_user_message=False,
    )

    messages = message_store.get_messages(conversation_id)
    assert len(messages) == 1
    assert messages[0]["role"] == "assistant"
    assert messages[0]["content"] == "web-only answer"


# --- maybe_capture_images / _capture_images_and_prepare_text -------------

# A minimal valid 1x1 PNG (magic bytes + enough to pass the sniff check).
_FAKE_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


class FakeAdapter:
    """Records every send() call - used where a test only cares whether a
    status note was posted (and with what text), not real Slack delivery."""

    capabilities = adapters.Capabilities()
    platform = "slack"

    def __init__(self):
        self.sent = []

    async def send(self, thread_key, text, **kwargs):
        self.sent.append({"thread_key": thread_key, "text": text, **kwargs})
        return {"ts": "999.999"}

    def message_ref(self, thread_key, native, message_id=None):
        return adapters.MessageRef("slack", thread_key.get("channel", ""), native["ts"])

    def external_ref(self, ref):
        return None


@pytest.mark.asyncio
async def test_maybe_capture_images_returns_empty_when_no_image_files():
    captured, skipped = await handlers.maybe_capture_images({"files": []}, conversation_id="c1")
    assert captured == []
    assert skipped == []

    captured, skipped = await handlers.maybe_capture_images(
        {"files": [{"mimetype": "audio/mp4", "name": "voice.m4a"}]}, conversation_id="c1",
    )
    assert captured == []
    assert skipped == []


@pytest.mark.asyncio
async def test_maybe_capture_images_downloads_and_caches_valid_image(monkeypatch, tmp_path):
    monkeypatch.setattr(handlers, "IMAGE_CACHE_DIR", tmp_path)
    monkeypatch.setattr(handlers.transcription, "_download_slack_file", lambda url, token: _FAKE_PNG_BYTES)

    event = {
        "files": [{
            "id": "F123", "name": "screenshot.png", "mimetype": "image/png",
            "size": len(_FAKE_PNG_BYTES), "url_private_download": "https://files.slack.com/f123",
        }]
    }
    captured, skipped = await handlers.maybe_capture_images(event, conversation_id="conv-1")

    assert skipped == []
    assert len(captured) == 1
    assert captured[0]["filename"] == "screenshot.png"
    assert captured[0]["mimetype"] == "image/png"
    assert captured[0]["slack_file_id"] == "F123"
    assert Path(captured[0]["local_path"]).read_bytes() == _FAKE_PNG_BYTES
    # Cached under IMAGE_CACHE_DIR/<conversation_id>/...
    assert Path(captured[0]["local_path"]).parent == tmp_path / "conv-1"


@pytest.mark.asyncio
async def test_maybe_capture_images_skips_oversized_file_by_declared_size(monkeypatch, tmp_path):
    monkeypatch.setattr(handlers, "IMAGE_CACHE_DIR", tmp_path)
    monkeypatch.setattr(handlers, "MAX_IMAGE_BYTES", 10)
    called = []
    monkeypatch.setattr(
        handlers.transcription, "_download_slack_file",
        lambda url, token: called.append(1) or _FAKE_PNG_BYTES,
    )

    event = {"files": [{
        "id": "F1", "name": "huge.png", "mimetype": "image/png",
        "size": 999999, "url_private_download": "https://files.slack.com/f1",
    }]}
    captured, skipped = await handlers.maybe_capture_images(event, conversation_id="conv-1")

    assert captured == []
    assert len(skipped) == 1
    assert "too large" in skipped[0]["reason"]
    # The declared-size check must short-circuit BEFORE downloading.
    assert called == []


@pytest.mark.asyncio
async def test_maybe_capture_images_skips_file_failing_magic_byte_sniff(monkeypatch, tmp_path):
    monkeypatch.setattr(handlers, "IMAGE_CACHE_DIR", tmp_path)
    monkeypatch.setattr(handlers.transcription, "_download_slack_file", lambda url, token: b"not-actually-an-image")

    event = {"files": [{
        "id": "F1", "name": "fake.png", "mimetype": "image/png",
        "size": 20, "url_private_download": "https://files.slack.com/f1",
    }]}
    captured, skipped = await handlers.maybe_capture_images(event, conversation_id="conv-1")

    assert captured == []
    assert len(skipped) == 1
    assert "not a recognized image format" in skipped[0]["reason"]


@pytest.mark.asyncio
async def test_capture_images_and_prepare_text_records_attachment_and_hint(monkeypatch, tmp_path):
    monkeypatch.setattr(handlers, "IMAGE_CACHE_DIR", tmp_path)
    monkeypatch.setattr(handlers.transcription, "_download_slack_file", lambda url, token: _FAKE_PNG_BYTES)

    conv = message_store.get_or_create_conversation(None)
    event = {"files": [{
        "id": "F1", "name": "shot.png", "mimetype": "image/png",
        "size": len(_FAKE_PNG_BYTES), "url_private_download": "https://files.slack.com/f1",
    }]}

    adapter = FakeAdapter()
    text, record_user_message = await handlers._capture_images_and_prepare_text(
        event, text="check this out", channel="C1", ts="111.222", thread_ts="100.000",
        conversation_id=conv, adapter=adapter,
    )

    assert record_user_message is False
    assert "check this out" in text
    assert "[Attached image saved to:" in text
    assert adapter.sent == []  # no skipped files -> no warning posted

    messages = message_store.get_messages(conv)
    assert len(messages) == 1
    attachments = message_store.get_attachments_for_message(messages[0]["id"])
    assert len(attachments) == 1
    assert attachments[0]["filename"] == "shot.png"


@pytest.mark.asyncio
async def test_capture_images_and_prepare_text_posts_warning_for_skipped_files(monkeypatch, tmp_path):
    monkeypatch.setattr(handlers, "IMAGE_CACHE_DIR", tmp_path)
    monkeypatch.setattr(handlers.transcription, "_download_slack_file", lambda url, token: b"not-an-image")

    conv = message_store.get_or_create_conversation(None)
    event = {"files": [{
        "id": "F1", "name": "bad.png", "mimetype": "image/png",
        "size": 20, "url_private_download": "https://files.slack.com/f1",
    }]}

    adapter = FakeAdapter()
    text, record_user_message = await handlers._capture_images_and_prepare_text(
        event, text="", channel="C1", ts="111.222", thread_ts="100.000",
        conversation_id=conv, adapter=adapter,
    )

    assert record_user_message is True
    assert text == ""
    assert len(adapter.sent) == 1
    assert "bad.png" in adapter.sent[0]["text"]
    # Status note must thread into the conversation, not post as a new
    # top-level channel message (SlackAdapter.send() drops threading without thread_ts).
    assert adapter.sent[0]["thread_key"]["thread_ts"] == "100.000"
    # deliver() records the status note exactly once, as a system message
    assert [m["role"] for m in message_store.get_messages(conv)] == ["system"]


@pytest.mark.asyncio
async def test_handle_mention_with_image_ignores_unauthorized_user(monkeypatch, tmp_path):
    """An unauthorized user @mentioning the bot with an image attached gets no
    image downloaded/cached/recorded: image capture is gated by is_authorized()
    like maybe_transcribe_audio(), before _handle()'s own auth check."""
    monkeypatch.setattr(handlers, "ALLOWED_USER_ID", "U_OWNER")
    monkeypatch.setattr(handlers, "IMAGE_CACHE_DIR", tmp_path)

    def _boom(*a, **kw):
        raise AssertionError("maybe_capture_images must not run for an unauthorized user")

    monkeypatch.setattr(handlers, "maybe_capture_images", _boom)

    app = FakeApp()
    handlers.register(app)
    handle_mention = app.registered[("event", "app_mention")]

    client = FakeClient()
    event = {
        "type": "app_mention", "user": "U_INTRUDER", "channel": "C1", "ts": "111.222",
        "text": "<@BOT> look at this", "files": [{"id": "F1", "name": "x.png", "mimetype": "image/png", "size": 10}],
    }
    await handle_mention(event=event, say=_make_say(client, "C1"), client=client)

    # Falls through to _handle(), which silently drops the unauthorized sender.
    assert client.posted == []


@pytest.mark.asyncio
async def test_maybe_capture_images_falls_back_to_filetype_when_mimetype_missing(monkeypatch, tmp_path):
    """Mirrors transcription._is_audio_file()'s mimetype-OR-filetype check -
    Slack normally sets mimetype on image files, but a client that omits it
    should still be recognized via the `filetype` field."""
    monkeypatch.setattr(handlers, "IMAGE_CACHE_DIR", tmp_path)
    monkeypatch.setattr(handlers.transcription, "_download_slack_file", lambda url, token: _FAKE_PNG_BYTES)

    event = {"files": [{
        "id": "F1", "name": "shot.png", "filetype": "png",
        "size": len(_FAKE_PNG_BYTES), "url_private_download": "https://files.slack.com/f1",
    }]}
    captured, skipped = await handlers.maybe_capture_images(event, conversation_id="conv-1")

    assert skipped == []
    assert len(captured) == 1


# --- Durable voice recordings: restart/resume ---------------------------------
# Feature: recordings are saved to ~/.dop-agent/audio first and each has a
# sidecar job record, so a bot restart mid-transcription can resume from the
# saved audio.

@pytest.mark.asyncio
async def test_web_voice_upload_is_saved_to_disk_and_marked_done_only_after_delivery(monkeypatch):
    import audio_store
    monkeypatch.setattr(handlers, "AUDIO_AUTO_SEND", False)
    handlers._channel_audio_buffer.clear()
    monkeypatch.setattr(handlers.transcription, "_transcribe_audio_file", lambda p: "saved text")
    conv = message_store.get_or_create_conversation(None)

    await handlers.transcribe_web_upload(b"wav", "n.wav", conversation_id=conv, user_id="U1")

    [(path, meta)] = list(audio_store._all_jobs())
    assert path.read_bytes() == b"wav", "audio is kept after a successful transcription"
    assert meta["state"] == "done" and meta["delivered"] and meta["transcript"] == "saved text"


@pytest.mark.asyncio
async def test_failed_transcription_keeps_audio_and_marks_job_failed(monkeypatch):
    import audio_store
    handlers._channel_audio_buffer.clear()

    def boom(p):
        raise RuntimeError("whisper crashed")

    monkeypatch.setattr(handlers.transcription, "_transcribe_audio_file", boom)
    conv = message_store.get_or_create_conversation(None)
    await handlers.transcribe_web_upload(b"wav", "n.wav", conversation_id=conv, user_id="U1")

    [(path, meta)] = list(audio_store._all_jobs())
    assert path.exists() and meta["state"] == "failed" and "whisper crashed" in meta["error"]


@pytest.mark.asyncio
async def test_resume_after_restart_reuses_stored_transcript_without_rerunning_whisper(monkeypatch):
    """A job interrupted after Whisper finished but before delivery must be
    delivered on resume WITHOUT transcribing again."""
    import audio_store
    monkeypatch.setattr(handlers, "AUDIO_AUTO_SEND", False)
    handlers._channel_audio_buffer.clear()
    handlers._inflight_audio.clear()
    conv = message_store.get_or_create_conversation(None)
    path = audio_store.save_bytes(conv, b"wav", "n.wav", platform="web", user_id="U1")
    audio_store.update(path, state="transcribed", transcript="from before restart", attempts=1)

    def must_not_run(p):
        raise AssertionError("Whisper re-run although transcript was stored")

    monkeypatch.setattr(handlers.transcription, "_transcribe_audio_file", must_not_run)
    assert await handlers.resume_audio_jobs() == 1
    for _ in range(100):
        if audio_store.read(path)["state"] == "done":
            break
        await asyncio.sleep(0.05)

    assert audio_store.read(path)["state"] == "done"
    texts = [m["content"] for m in message_store.get_messages(conv)]
    assert any("from before restart" in t for t in texts)


@pytest.mark.asyncio
async def test_resume_gives_up_after_max_attempts_but_keeps_the_audio(monkeypatch):
    import audio_store
    handlers._inflight_audio.clear()
    conv = message_store.get_or_create_conversation(None)
    path = audio_store.save_bytes(conv, b"wav", "n.wav", platform="web")
    audio_store.update(path, state="transcribing", attempts=audio_store.MAX_ATTEMPTS)

    assert await handlers.resume_audio_jobs() == 0
    assert audio_store.read(path)["state"] == "failed" and path.exists()


@pytest.mark.asyncio
async def test_slack_voice_note_is_saved_before_placeholder_and_job_tracks_message_refs(monkeypatch):
    """Slack path: the downloaded recording must be on disk (state pending) before
    the 'Transcribing...' placeholder is posted, and the sidecar must record the
    placeholder refs so a restart can update that same message."""
    import audio_store
    from tests.adapter_contract.fake_adapter import FakeAdapter

    handlers._channel_audio_buffer.clear()
    monkeypatch.setattr(handlers, "AUDIO_AUTO_SEND", False)
    class SlackShapedFake(FakeAdapter):
        platform = "slack"

        @staticmethod
        def message_ref(thread_key, native, message_id=None):
            return FakeAdapter.message_ref({"chat_id": 1}, native, message_id)

    fake = SlackShapedFake()
    monkeypatch.setattr(handlers.adapters.REGISTRY, "slack_for", lambda client: fake)
    seen = {}
    orig_deliver = handlers.adapters.deliver

    async def spy_deliver(adapter, thread_key, text, **kw):
        if "Transcribing" in text:
            seen["jobs_on_disk"] = [m.get("state") for _p, m in audio_store._all_jobs()]
        return await orig_deliver(adapter, thread_key, text, **kw)

    monkeypatch.setattr(handlers.adapters, "deliver", spy_deliver)
    monkeypatch.setattr(handlers.transcription, "_download_slack_file", lambda url, tok: b"slack-audio")
    monkeypatch.setattr(handlers.transcription, "_transcribe_audio_file", lambda p: "slack words")
    conv = message_store.get_or_create_conversation(None)
    event = {"channel": "D1", "files": [{"name": "v.m4a", "mimetype": "audio/mp4",
                                          "url_private": "https://files.slack.com/v.m4a"}]}

    assert await handlers.maybe_transcribe_audio(event, None, None, thread_ts="1.1",
                                                 conversation_id=conv, user_id="U1") is True

    assert seen["jobs_on_disk"] == ["pending"]
    [(path, meta)] = list(audio_store._all_jobs())
    assert path.read_bytes() == b"slack-audio" and meta["platform"] == "slack"
    assert meta["state"] == "done" and meta["message_ref"] and meta["transcript"] == "slack words"


@pytest.mark.asyncio
async def test_slack_job_without_placeholder_is_resumed_with_new_placeholder(monkeypatch):
    """Crash between 'file saved' and 'placeholder posted': resume posts a fresh
    placeholder through the Slack adapter and finishes the transcription."""
    import audio_store
    from tests.adapter_contract.fake_adapter import FakeAdapter

    handlers._channel_audio_buffer.clear()
    handlers._inflight_audio.clear()
    monkeypatch.setattr(handlers, "AUDIO_AUTO_SEND", False)
    monkeypatch.setattr(handlers.transcription, "_transcribe_audio_file", lambda p: "resumed words")
    conv = message_store.get_or_create_conversation(None)
    path = audio_store.save_bytes(conv, b"a", "v.m4a", platform="slack", user_id="U1",
                                  conversation_ref=conv, thread_key=FakeAdapter.thread_key())
    fake = FakeAdapter()

    assert await handlers.resume_audio_jobs(lambda: fake) == 1
    for _ in range(100):
        if audio_store.read(path)["state"] == "done":
            break
        await asyncio.sleep(0.05)
    assert audio_store.read(path)["state"] == "done"
    assert any("Transcribing" in str(m) for m in fake.sent)
