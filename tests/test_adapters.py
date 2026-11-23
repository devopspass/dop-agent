"""
Unit tests for adapters.py — the Adapter protocol implementations (Slack, Web)
against a fake Slack client, plus the broadcaster that fans one logical
message out across bound channel adapters without looping it back to the
exact origin binding.
"""

from __future__ import annotations

import pytest

import message_store
from adapters import deliver, deliver_update
from adapters import Adapter, BroadcastOrigin, ConversationBroadcaster, SlackAdapter, WebAdapter


class FakeClient:
    def __init__(self):
        self.posted = []
        self.updated = []
        self.uploaded = []
        self.permalinks = {}
        self.reactions_added = []
        self.reactions_removed = []
        self.deleted = []
        self.files_info_calls = []
        self.files_info_result = None

    async def chat_postMessage(self, **kwargs):
        self.posted.append(kwargs)
        return {"ts": "111.222", "channel": kwargs.get("channel")}

    async def chat_update(self, **kwargs):
        self.updated.append(kwargs)
        return {"ts": kwargs.get("ts"), "channel": kwargs.get("channel")}

    async def files_upload_v2(self, **kwargs):
        self.uploaded.append(kwargs)
        return {"ok": True}

    async def chat_getPermalink(self, **kwargs):
        ts = kwargs.get("message_ts")
        if ts in self.permalinks:
            return {"permalink": self.permalinks[ts]}
        return {"permalink": f"https://example.slack.com/archives/{kwargs.get('channel')}/p{ts}"}

    async def reactions_add(self, **kwargs):
        self.reactions_added.append(kwargs)
        return {"ok": True}

    async def reactions_remove(self, **kwargs):
        self.reactions_removed.append(kwargs)
        return {"ok": True}

    async def chat_delete(self, **kwargs):
        self.deleted.append(kwargs)
        return {"ok": True}

    async def files_info(self, **kwargs):
        self.files_info_calls.append(kwargs)
        if self.files_info_result is not None:
            return self.files_info_result
        return {"file": {"shares": {}}}


@pytest.fixture(autouse=True)
def _isolated_message_store(tmp_path, monkeypatch):
    monkeypatch.setattr(message_store, "DB_PATH", tmp_path / "bot_store.db")
    monkeypatch.setattr(message_store, "_TITLES_FILE", tmp_path / "session_titles.json")
    message_store.init_db()
    yield


def test_slack_adapter_satisfies_adapter_protocol():
    adapter = SlackAdapter(FakeClient())
    assert isinstance(adapter, Adapter)


@pytest.mark.asyncio
async def test_send_posts_via_chat_post_message_and_includes_thread_ts():
    client = FakeClient()
    adapter = SlackAdapter(client)
    conversation_id = message_store.get_or_create_conversation("C1:100.1")

    result, message_id = await deliver(adapter,
        {"channel": "C1", "thread_ts": "100.1"},
        "hello world",
        conversation_id=conversation_id,
        role="assistant",
    )

    assert len(client.posted) == 1
    assert client.posted[0]["channel"] == "C1"
    assert client.posted[0]["thread_ts"] == "100.1"
    assert client.posted[0]["text"] == "hello world"
    assert result["ts"] == "111.222"
    assert message_id is not None  # recorded into message_store


@pytest.mark.asyncio
async def test_send_omits_thread_ts_when_not_provided():
    client = FakeClient()
    adapter = SlackAdapter(client)

    await deliver(adapter, {"channel": "C1"}, "hi", conversation_id=None)

    assert "thread_ts" not in client.posted[0]


@pytest.mark.asyncio
async def test_send_explicit_kwarg_thread_ts_not_overridden_by_thread_key():
    """setdefault semantics: an explicit override in kwargs wins over the
    thread_key's own thread_ts, matching existing call-site behavior."""
    client = FakeClient()
    adapter = SlackAdapter(client)

    await deliver(adapter,
        {"channel": "C1", "thread_ts": "100.1"},
        "hi",
        conversation_id=None,
        thread_ts="999.9",
    )

    assert client.posted[0]["thread_ts"] == "999.9"


@pytest.mark.asyncio
async def test_update_edits_via_chat_update():
    client = FakeClient()
    adapter = SlackAdapter(client)
    conversation_id = message_store.get_or_create_conversation("C1:100.1")

    result, message_id = await deliver_update(adapter,
        {"channel": "C1"},
        "100.1",
        "edited text",
        conversation_id=conversation_id,
        role="assistant",
    )

    assert len(client.updated) == 1
    assert client.updated[0]["channel"] == "C1"
    assert client.updated[0]["ts"] == "100.1"
    assert client.updated[0]["text"] == "edited text"
    assert result["ts"] == "100.1"


@pytest.mark.asyncio
async def test_upload_file_passes_channel_and_thread_ts():
    client = FakeClient()
    adapter = SlackAdapter(client)

    await adapter.upload_file(
        {"channel": "C1", "thread_ts": "100.1"}, b"filedata", "report.png"
    )

    assert len(client.uploaded) == 1
    assert client.uploaded[0]["channel"] == "C1"
    assert client.uploaded[0]["thread_ts"] == "100.1"
    assert client.uploaded[0]["file"] == b"filedata"
    assert client.uploaded[0]["filename"] == "report.png"


@pytest.mark.asyncio
async def test_upload_file_omits_thread_ts_when_absent():
    client = FakeClient()
    adapter = SlackAdapter(client)

    await adapter.upload_file({"channel": "C1"}, b"x", "f.txt")

    assert "thread_ts" not in client.uploaded[0]


def test_format_markdown_converts_and_splits_for_slack():
    adapter = SlackAdapter(FakeClient())

    chunks = adapter.format_markdown("**bold text**")

    assert isinstance(chunks, list)
    assert len(chunks) == 1
    # Slack mrkdwn uses single asterisks for bold, not Markdown's double
    assert chunks[0] == "*bold text*"


def test_format_markdown_splits_long_text_into_multiple_chunks():
    adapter = SlackAdapter(FakeClient())
    long_text = "x" * 5000  # exceeds SLACK_MAX_CHARS (3900)

    chunks = adapter.format_markdown(long_text)

    assert len(chunks) > 1
    assert all(len(c) <= 3900 for c in chunks)


@pytest.mark.asyncio
async def test_permalink_returns_url_for_thread_root():
    client = FakeClient()
    client.permalinks["100.1"] = "https://example.slack.com/archives/C1/p1001"
    adapter = SlackAdapter(client)

    url = await adapter.permalink({"channel": "C1", "thread_ts": "100.1"})

    assert url == "https://example.slack.com/archives/C1/p1001"


@pytest.mark.asyncio
async def test_permalink_returns_none_when_no_thread_ts():
    adapter = SlackAdapter(FakeClient())

    url = await adapter.permalink({"channel": "C1"})

    assert url is None


@pytest.mark.asyncio
async def test_permalink_returns_none_on_client_error_non_fatal():
    class FailingClient(FakeClient):
        async def chat_getPermalink(self, **kwargs):
            raise RuntimeError("boom")

    adapter = SlackAdapter(FailingClient())

    url = await adapter.permalink({"channel": "C1", "thread_ts": "100.1"})

    assert url is None


@pytest.mark.asyncio
async def test_send_record_false_posts_without_recording():
    client = FakeClient()
    adapter = SlackAdapter(client)
    conversation_id = message_store.get_or_create_conversation("C1:100.1")

    result, message_id = await deliver(adapter,
        {"channel": "C1", "thread_ts": "100.1"},
        "overflow chunk",
        conversation_id=conversation_id,
        role="assistant",
        record=False,
    )

    assert len(client.posted) == 1
    assert client.posted[0]["text"] == "overflow chunk"
    assert result["ts"] == "111.222"
    assert message_id is None
    assert message_store.get_messages(conversation_id) == []


@pytest.mark.asyncio
async def test_upload_file_with_file_uploads_forwards_list_as_is():
    client = FakeClient()
    adapter = SlackAdapter(client)

    await adapter.upload_file(
        {"channel": "C1", "thread_ts": "100.1"},
        file_uploads=[{"file": b"a", "filename": "a.md"}, {"file": b"b", "filename": "b.md"}],
    )

    assert len(client.uploaded) == 1
    assert client.uploaded[0]["channel"] == "C1"
    assert client.uploaded[0]["thread_ts"] == "100.1"
    assert client.uploaded[0]["file_uploads"] == [
        {"file": b"a", "filename": "a.md"}, {"file": b"b", "filename": "b.md"},
    ]


@pytest.mark.asyncio
async def test_upload_file_wait_posted_returns_once_share_registers():
    client = FakeClient()
    client.files_info_result = {"file": {"id": "F1", "shares": {"public": {"C1": [{}]}}}}
    adapter = SlackAdapter(client)

    async def fake_files_upload_v2(**kwargs):
        return {"file": {"id": "F1"}}
    client.files_upload_v2 = fake_files_upload_v2

    await adapter.upload_file(
        {"channel": "C1"}, b"data", "table.png", wait_posted=True, poll_interval=0.01,
    )

    assert len(client.files_info_calls) == 1
    assert client.files_info_calls[0]["file"] == "F1"


@pytest.mark.asyncio
async def test_upload_file_wait_posted_gives_up_after_timeout():
    client = FakeClient()
    adapter = SlackAdapter(client)

    async def fake_files_upload_v2(**kwargs):
        return {"file": {"id": "F1"}}
    client.files_upload_v2 = fake_files_upload_v2
    # files_info_result stays default (empty shares) -> never registers

    await adapter.upload_file(
        {"channel": "C1"}, b"data", "table.png", wait_posted=True,
        timeout=0.05, poll_interval=0.02,
    )

    assert len(client.files_info_calls) >= 1


@pytest.mark.asyncio
async def test_add_reaction_forwards_to_client():
    client = FakeClient()
    adapter = SlackAdapter(client)

    await adapter.add_reaction({"channel": "C1"}, "eyes", "100.1")

    assert client.reactions_added == [{"channel": "C1", "name": "eyes", "timestamp": "100.1"}]


@pytest.mark.asyncio
async def test_remove_reaction_forwards_to_client():
    client = FakeClient()
    adapter = SlackAdapter(client)

    await adapter.remove_reaction({"channel": "C1"}, "eyes", "100.1")

    assert client.reactions_removed == [{"channel": "C1", "name": "eyes", "timestamp": "100.1"}]


@pytest.mark.asyncio
async def test_delete_message_forwards_to_client():
    client = FakeClient()
    adapter = SlackAdapter(client)

    await adapter.delete_message({"channel": "C1"}, "100.1")

    assert client.deleted == [{"channel": "C1", "ts": "100.1"}]


def test_web_adapter_satisfies_adapter_protocol():
    assert isinstance(WebAdapter(), Adapter)


@pytest.mark.asyncio
async def test_web_adapter_send_records_message_and_returns_message_id():
    conversation_id = message_store.get_or_create_conversation(None)
    adapter = WebAdapter()

    result, message_id = await deliver(adapter,
        {"conversation_id": conversation_id},
        "hello from web",
        conversation_id=conversation_id,
        role="user",
    )

    assert result is None
    assert message_id is not None
    stored = message_store.get_messages(conversation_id)
    assert len(stored) == 1
    assert stored[0]["content"] == "hello from web"
    assert stored[0]["role"] == "user"


@pytest.mark.asyncio
async def test_web_adapter_send_forwards_external_ref_kwarg():
    conversation_id = message_store.get_or_create_conversation(None)
    adapter = WebAdapter()

    await deliver(adapter,
        {"conversation_id": conversation_id},
        "mirrored from slack",
        conversation_id=conversation_id,
        role="user",
        external_ref="C1:100.1",
    )

    stored = message_store.get_messages(conversation_id)
    assert stored[0]["external_ref"] == "C1:100.1"


@pytest.mark.asyncio
async def test_web_adapter_send_uses_thread_key_conversation_id_when_kwarg_omitted():
    conversation_id = message_store.get_or_create_conversation(None)
    adapter = WebAdapter()

    await deliver(adapter, {"conversation_id": conversation_id}, "text")

    stored = message_store.get_messages(conversation_id)
    assert len(stored) == 1


@pytest.mark.asyncio
async def test_web_adapter_send_no_op_when_no_conversation_id_available():
    adapter = WebAdapter()

    result, message_id = await deliver(adapter, {}, "orphan text")

    assert result is None
    assert message_id is None


@pytest.mark.asyncio
async def test_web_adapter_send_drops_slack_only_kwargs_instead_of_raising():
    """Status posts pass Slack-only kwargs (mrkdwn/blocks) to every adapter;
    WebAdapter must silently drop kwargs it has no use for instead of raising,
    so the live status message exists for Web-only conversations."""
    conversation_id = message_store.get_or_create_conversation(None)
    adapter = WebAdapter()

    result, message_id = await deliver(adapter,
        {"conversation_id": conversation_id},
        ":thinking_face: _Working..._",
        conversation_id=conversation_id,
        role="status",
        mrkdwn=True,
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "x"}}],
    )

    assert message_id is not None
    stored = message_store.get_messages(conversation_id)
    assert len(stored) == 1
    assert stored[0]["role"] == "status"


@pytest.mark.asyncio
async def test_web_adapter_update_drops_slack_only_kwargs_instead_of_raising():
    """Like send(), update() (used for every "Working..." status refresh)
    drops Slack-only kwargs instead of raising."""
    conversation_id = message_store.get_or_create_conversation(None)
    adapter = WebAdapter()
    _, message_id = await deliver(adapter,
        {"conversation_id": conversation_id}, "original", conversation_id=conversation_id, role="status"
    )

    result, stored_id = await deliver_update(adapter,
        {"conversation_id": conversation_id},
        message_id,
        "edited status",
        conversation_id=conversation_id,
        role="status",
        message_id=message_id,
        mrkdwn=True,
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "edited"}}],
    )

    assert stored_id == message_id
    stored = message_store.get_messages(conversation_id)
    assert len(stored) == 1
    assert stored[0]["content"] == "edited status"


@pytest.mark.asyncio
async def test_web_adapter_update_edits_existing_message_in_place():
    conversation_id = message_store.get_or_create_conversation(None)
    adapter = WebAdapter()
    _, message_id = await deliver(adapter,
        {"conversation_id": conversation_id}, "original", conversation_id=conversation_id, role="assistant"
    )

    await deliver_update(adapter,
        {"conversation_id": conversation_id},
        message_id,
        "edited",
        conversation_id=conversation_id,
        role="assistant",
        message_id=message_id,
    )

    stored = message_store.get_messages(conversation_id)
    assert len(stored) == 1
    assert stored[0]["content"] == "edited"


@pytest.mark.asyncio
async def test_web_adapter_upload_file_not_supported():
    adapter = WebAdapter()

    with pytest.raises(NotImplementedError):
        await adapter.upload_file({"conversation_id": "c1"}, b"x", "f.txt")


@pytest.mark.asyncio
async def test_web_adapter_send_record_false_still_records():
    """WebAdapter ignores `record` — sending IS recording for the Web UI,
    there's no separate remote-platform call to skip."""
    conversation_id = message_store.get_or_create_conversation(None)
    adapter = WebAdapter()

    result, message_id = await deliver(adapter,
        {"conversation_id": conversation_id}, "chunk", conversation_id=conversation_id,
        role="assistant", record=False,
    )

    assert message_id is not None
    assert len(message_store.get_messages(conversation_id)) == 1


@pytest.mark.asyncio
async def test_web_adapter_add_reaction_not_supported():
    adapter = WebAdapter()

    with pytest.raises(NotImplementedError):
        await adapter.add_reaction({"conversation_id": "c1"}, "eyes", "100.1")


@pytest.mark.asyncio
async def test_web_adapter_remove_reaction_not_supported():
    adapter = WebAdapter()

    with pytest.raises(NotImplementedError):
        await adapter.remove_reaction({"conversation_id": "c1"}, "eyes", "100.1")


@pytest.mark.asyncio
async def test_web_adapter_delete_message_not_supported():
    adapter = WebAdapter()

    with pytest.raises(NotImplementedError):
        await adapter.delete_message({"conversation_id": "c1"}, "100.1")


def test_web_adapter_format_markdown_returns_single_unsplit_chunk():
    adapter = WebAdapter()

    chunks = adapter.format_markdown("**bold text**\n" + "x" * 5000)

    assert chunks == ["**bold text**\n" + "x" * 5000]


@pytest.mark.asyncio
async def test_web_adapter_permalink_always_none():
    adapter = WebAdapter()

    assert await adapter.permalink({"conversation_id": "c1"}) is None


@pytest.mark.asyncio
async def test_broadcaster_skips_origin_binding_and_mirrors_to_other_bound_adapters():
    """Broadcaster: a Web-origin logical message should fan out to
    the bound Slack thread, but must not be delivered back into the same Web
    binding that originated it (which would duplicate the DB row)."""
    conversation_id = message_store.get_or_create_conversation(None)
    message_store.bind_conversation(conversation_id, message_store.WEB_PLATFORM, conversation_id)
    message_store.bind_conversation(conversation_id, message_store.SLACK_PLATFORM, "C1:100.1")

    client = FakeClient()
    broadcaster = ConversationBroadcaster(
        {
            message_store.WEB_PLATFORM: WebAdapter(),
            message_store.SLACK_PLATFORM: SlackAdapter(client),
        }
    )

    deliveries = await broadcaster.fanout_send(
        conversation_id,
        "hello from web",
        role="user",
        origin=BroadcastOrigin(message_store.WEB_PLATFORM, conversation_id),
        record_by_platform={message_store.SLACK_PLATFORM: False},
    )

    assert list(deliveries) == [message_store.SLACK_PLATFORM]
    assert client.posted[0]["channel"] == "C1"
    assert client.posted[0]["thread_ts"] == "100.1"
    assert message_store.get_messages(conversation_id) == []
