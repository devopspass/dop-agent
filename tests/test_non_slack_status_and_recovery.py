"""A non-Slack adapter drives the REAL turn engine end to end.

Platform-neutral behaviour: the live status message handle comes from
``adapter.message_ref`` (not a Slack-shaped ``result["ts"]``, so e.g. Telegram's
``{"message_id": ...}`` works), the active-status marker is recorded with its
platform, and restart recovery goes through that platform's adapter.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "adapter_contract"))

import handlers  # noqa: E402
import message_store  # noqa: E402
import persistence  # noqa: E402
from adapters import AdapterRegistry, IncomingMessage  # noqa: E402
from contract_harness import make_fake  # noqa: E402
from test_handlers import FakeConversation  # noqa: E402


def _setup(monkeypatch, tmp_path, fake):
    monkeypatch.setattr(persistence, "ACTIVE_STATUS_FILE", tmp_path / "active_status.json")
    message_store.add_allowed_identity(fake.platform, "tg-user-1")
    monkeypatch.setattr(handlers, "_is_new_conversation", lambda cid: False)


async def test_non_slack_turn_edits_status_message_then_replaces_it_with_the_answer(monkeypatch, tmp_path):
    """The 'Working...' message must be edited by live status updates and then
    replaced by the final answer (not left behind), and the crash-recovery
    marker must exist during the turn and be cleared afterwards."""
    fake = make_fake().adapter
    _setup(monkeypatch, tmp_path, fake)
    seen_during_turn = {}
    convo = FakeConversation()

    async def send(text, on_status=None):
        await on_status("step one")
        seen_during_turn["active"] = persistence._load_active_status()
        return "final answer"

    convo.send = send
    monkeypatch.setattr(handlers, "get_convo", lambda cid: convo)

    await handlers.handle_incoming(
        IncomingMessage(platform=fake.platform, user_id="tg-user-1", conversation_key="42", text="hi", ref="f:7"),
        registry=AdapterRegistry([fake]),
    )

    edit_texts = [e["text"] for e in fake.edits]
    assert any("step one" in t for t in edit_texts)
    assert "final answer" in edit_texts[-1]
    assert len(seen_during_turn["active"]) == 1
    entry = next(iter(seen_during_turn["active"].values()))
    assert entry["platform"] == fake.platform and entry["conversation_id"]
    assert persistence._load_active_status() == {}


async def test_restart_recovery_marks_non_slack_status_interrupted_via_its_own_adapter(monkeypatch, tmp_path):
    """After a crash the active-status sweep must edit a NON-Slack status
    message through that platform's adapter (not assume a Slack channel) and
    schedule the auto-resume for its conversation."""
    import adapters
    import dop_agent

    fake = make_fake().adapter
    _setup(monkeypatch, tmp_path, fake)
    monkeypatch.setattr(adapters, "REGISTRY", AdapterRegistry([fake]))
    monkeypatch.setattr(message_store, "DB_PATH", tmp_path / "bot_store.db")
    message_store.init_db()
    conv = message_store.get_or_create_conversation(None)
    persistence._register_active_status(
        "42", "9", None, msg_ts="8", platform=fake.platform,
        thread_key=fake.thread_key if isinstance(fake.thread_key, dict) else {"chat_id": 42},
        conversation_id=conv, message_id=None,
    )
    resumed = []

    async def fake_inject(conversation_id, text):
        resumed.append(conversation_id)

    monkeypatch.setattr(dop_agent, "_inject_watch_message", fake_inject)
    monkeypatch.setattr(dop_agent, "_recover_lost_response", lambda cid: None)

    await dop_agent._resume_interrupted_turns()
    import asyncio
    await asyncio.sleep(0.05)

    assert any("Interrupted" in e["text"] for e in fake.edits)
    assert resumed == [conv]
    assert persistence._load_active_status() == {}
