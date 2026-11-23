"""Neutral message model: the core speaks ``Button``/``footnote``/plain
markdown; each adapter renders it natively. ``_handle`` gates reactions and
the post-turn delivery flow on adapter capabilities, not on
``platform == slack``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "adapter_contract"))

import adapters  # noqa: E402
import handlers  # noqa: E402
import message_store  # noqa: E402
from adapters import Button, SlackAdapter, WebAdapter, deliver, deliver_update  # noqa: E402
from contract_harness import make_slack, make_web  # noqa: E402
from fake_adapter import FakeAdapter  # noqa: E402


async def test_slack_stop_button_block_kit_payload_matches_legacy_status_blocks():
    """SlackAdapter renders the neutral Stop Button as the exact Block Kit
    payload of the live "Working..." message."""
    h = make_slack()
    stop = Button(label=":black_square_for_stop: Stop", action_id="stop_working_turn",
                  value="conv-1", style="danger", native_only=True)
    await deliver(h.adapter, h.thread_key, ":thinking_face: _Working..._",
                         conversation_id=h.conversation_id, role="status", buttons=[stop])
    posted = h.extra["client"].posted[0]
    assert posted["mrkdwn"] is True
    assert posted["text"] == ":thinking_face: _Working..._"
    assert posted["blocks"] == [
        {"type": "section", "text": {"type": "mrkdwn", "text": ":thinking_face: _Working..._"}},
        {"type": "actions", "elements": [{
            "type": "button",
            "text": {"type": "plain_text", "text": ":black_square_for_stop: Stop", "emoji": True},
            "action_id": "stop_working_turn", "value": "conv-1", "style": "danger"}]},
    ]


async def test_slack_empty_buttons_list_strips_controls_and_footnote_becomes_context_block():
    """buttons=[] must still send a section block (so chat_update replaces old
    controls); footnote renders as a context block and is appended to the
    fallback text that is stored."""
    h = make_slack()
    await deliver_update(h.adapter, h.thread_key, "1.1", "body", conversation_id=h.conversation_id,
                           footnote="note", buttons=[])
    upd = h.extra["client"].updated[0]
    assert upd["text"] == "body\n\nnote"
    assert upd["blocks"] == [
        {"type": "section", "text": {"type": "mrkdwn", "text": "body"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "note"}]},
    ]


async def test_slack_section_text_is_truncated_to_block_limit_but_fallback_text_is_full():
    """Slack caps section text (SLACK_BLOCK_MAX_CHARS, below the 3000-char
    API limit); the adapter (not the core) owns that limit."""
    h = make_slack()
    long_text = "x" * (adapters.SLACK_BLOCK_MAX_CHARS + 50)
    await deliver(h.adapter, h.thread_key, long_text, conversation_id=h.conversation_id, buttons=[])
    posted = h.extra["client"].posted[0]
    section = posted["blocks"][0]["text"]["text"]
    assert len(section) == adapters.SLACK_BLOCK_MAX_CHARS and section.endswith("…")
    assert posted["text"] == long_text


async def test_web_adapter_skips_native_only_buttons_but_renders_others_as_actions():
    """The Web UI has its own Stop control in the live bubble, so the Stop
    button (native_only) must not show up as a message action; transcript
    buttons still must."""
    h = make_web()
    stop = Button(label="Stop", action_id="stop_working_turn", native_only=True)
    send = Button(label="Send", action_id="send_transcript_to_copilot")
    await deliver(h.adapter, h.thread_key, "working", conversation_id=h.conversation_id, buttons=[stop])
    await deliver(h.adapter, h.thread_key, "tx", conversation_id=h.conversation_id, buttons=[send])
    msgs = message_store.get_messages(h.conversation_id)
    assert not msgs[0]["actions"]
    assert msgs[1]["actions"] == [{"id": "send_transcript_to_copilot", "label": "Send"}]


async def test_web_adapter_appends_footnote_to_stored_text_and_ignores_unknown_kwargs():
    """Callers may pass platform extras; Web ignores them instead of
    raising."""
    h = make_web()
    await deliver(h.adapter, h.thread_key, "hello", conversation_id=h.conversation_id,
                         footnote="fn", mrkdwn=True, blocks=[{"x": 1}])
    assert message_store.get_messages(h.conversation_id)[0]["content"] == "hello\n\nfn"
    assert not hasattr(WebAdapter, "_filter_kwargs")


def test_slack_action_message_text_reads_first_section_or_falls_back():
    """Slack interaction payload parsing lives in SlackAdapter, not in handlers."""
    body = {"message": {"blocks": [{"type": "section", "text": {"text": "T"}}]}}
    assert SlackAdapter.action_message_text(body, "d") == "T"
    assert SlackAdapter.action_message_text({}, "d") == "d"


class _Convo:
    session_id = "s"
    model_name = None

    async def ensure_ready(self):
        pass

    async def send(self, text, on_status=None):
        return "answer"

    @property
    def is_alive(self):
        return True


async def test_handle_on_platform_without_reactions_never_calls_add_reaction(monkeypatch):
    """Reactions are gated on adapter capabilities, not on
    ``adapter.platform == slack``: a platform (Telegram) without reaction
    support gets none, and the turn must still deliver."""
    monkeypatch.setattr(handlers, "ALLOWED_USER_ID", "U_OWNER")
    monkeypatch.setattr(handlers, "get_convo", lambda cid: _Convo())
    monkeypatch.setattr(handlers, "_is_new_conversation", lambda cid: False)
    fake = FakeAdapter()
    called = []

    async def _boom(*a, **k):
        called.append(a)

    fake.add_reaction = _boom
    fake.remove_reaction = _boom
    conv = message_store.get_or_create_conversation("fake-chat")
    await handlers._handle(user_id="U_OWNER", text="hi", ts="1.1",
                           adapter=fake,
                           thread_key=FakeAdapter.thread_key(), conversation_id=conv)
    assert called == []
    delivered = [s["text"] for s in fake.sent] + [e["text"] for e in fake.edits]
    assert "answer" in delivered
