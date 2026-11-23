"""
Integration-style tests for the multi-channel model.

These intentionally exercise Slack + Web UI together via the Phase 2
broadcaster and the shared SQLite transcript, rather than unit-testing each
adapter in isolation. The goal is to lock down the intended "one logical
conversation, one logical transcript" behavior: a Web-origin user message is
recorded once, mirrored to Slack, and the Slack-side assistant reply lands
back in the exact same transcript the Web UI reads.
"""

from __future__ import annotations

import asyncio

import handlers
import message_store
import web_server
from adapters import ConversationBroadcaster, SlackAdapter, WebAdapter


class _FakeSlackClient:
    def __init__(self):
        self.posted = []
        self.updated = []
        self.reactions_added = []
        self.reactions_removed = []

    async def chat_postMessage(self, **kwargs):
        self.posted.append(kwargs)
        return {"ts": f"{len(self.posted)}.000", "channel": kwargs.get("channel")}

    async def chat_update(self, **kwargs):
        self.updated.append(kwargs)
        return {"ts": kwargs["ts"], "channel": kwargs.get("channel")}

    async def reactions_add(self, **kwargs):
        self.reactions_added.append(kwargs)
        return {"ok": True}

    async def reactions_remove(self, **kwargs):
        self.reactions_removed.append(kwargs)
        return {"ok": True}


class _FakeConversation:
    def __init__(self, response: str):
        self.session_id = "session-123"
        self.model_name = None
        self._response = response
        self.send_calls: list[str] = []

    async def ensure_ready(self):
        return None

    async def send(self, text, on_status=None):
        self.send_calls.append(text)
        if on_status is not None:
            await on_status(":thinking_face: _Working..._")
        return self._response


def _use_tmp_store(tmp_path, monkeypatch):
    monkeypatch.delenv("ALLOWED_USER_ID", raising=False)
    monkeypatch.delenv("WEB_UI_PASSWORD", raising=False)
    monkeypatch.setattr(message_store, "DB_PATH", tmp_path / "bot_store.db")
    monkeypatch.setattr(message_store, "_TITLES_FILE", tmp_path / "session_titles.json")
    message_store.init_db()


def test_web_origin_user_message_and_slack_reply_share_one_transcript(tmp_path, monkeypatch):
    """A Web-origin user turn mirrored to Slack and the later Slack-side
    assistant reply stay in one shared transcript, not duplicate per-transport histories."""
    _use_tmp_store(tmp_path, monkeypatch)
    conversation_id = message_store.get_or_create_conversation(None)
    message_store.bind_conversation(conversation_id, message_store.WEB_PLATFORM, conversation_id)
    message_store.bind_conversation(conversation_id, message_store.SLACK_PLATFORM, "D123:9.9")

    fake_slack = _FakeSlackClient()
    monkeypatch.setattr(web_server, "_slack_client", fake_slack)
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
    monkeypatch.setattr(web_server.asyncio, "create_task", lambda coro: scheduled.append(coro) or type("_Task", (), {})())
    monkeypatch.setattr(web_server, "_handle_fn", lambda **kwargs: asyncio.sleep(0))

    returned_id = asyncio.run(web_server.send_message(conversation_id, "hello from web"))
    for coro in scheduled:
        coro.close()

    monkeypatch.setattr(handlers, "ALLOWED_USER_ID", "U_OWNER")
    fake_convo = _FakeConversation("assistant reply from slack path")
    monkeypatch.setattr(handlers, "get_convo", lambda cid: fake_convo)
    monkeypatch.setattr(handlers, "_is_new_conversation", lambda cid: False)

    asyncio.run(
        handlers._handle(
            user_id="U_OWNER",
            text="hello from web",
            ts="1.000",
            thread_ts="9.9",
            adapter=SlackAdapter(fake_slack),
            thread_key={"channel": "D123", "thread_ts": "9.9"},
            conversation_id=conversation_id,
            record_user_message=False,
        )
    )

    turns = message_store.get_messages(conversation_id)
    assert returned_id == conversation_id
    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert turns[0]["content"] == "hello from web"
    assert turns[1]["content"] == "assistant reply from slack path"
    assert fake_convo.send_calls == ["hello from web"]
    assert fake_slack.posted[0]["thread_ts"] == "9.9"
    assert fake_slack.updated[-1]["text"] == "assistant reply from slack path"
