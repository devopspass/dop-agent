"""M4: MessageRef-keyed side files and single-write recording."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "adapter_contract"))

import message_store  # noqa: E402
import persistence  # noqa: E402
from adapters import deliver  # noqa: E402
from contract_harness import make_slack  # noqa: E402


def test_legacy_slack_shaped_active_status_keys_are_migrated_on_load(tmp_path, monkeypatch):
    """Active-status entries keyed '<channel>:<ts>' are re-keyed on load to
    'slack:<channel>:<ts>' so crash recovery still finds them."""
    f = tmp_path / "active_status.json"
    f.write_text(json.dumps({"C1:100.1": {"channel": "C1", "ts": "100.1", "thread_ts": "100.1", "msg_ts": "101.1"}}))
    monkeypatch.setattr(persistence, "ACTIVE_STATUS_FILE", f)

    mapping = persistence._load_active_status()

    assert list(mapping) == ["slack:C1:100.1"]
    assert mapping["slack:C1:100.1"]["channel"] == "C1"


async def test_record_false_continuation_chunk_posts_without_recording(monkeypatch):
    """Continuation chunks of an already-recorded reply must not add a row."""
    h = make_slack()
    calls = []
    monkeypatch.setattr(message_store, "record_message", lambda *a, **k: calls.append(a))

    await deliver(h.adapter, h.thread_key, "chunk 2", conversation_id=h.conversation_id, record=False)

    assert calls == [] and len(h.extra["client"].posted) == 1


async def test_slack_deliver_stores_legacy_external_ref_for_dedup():
    """Slack dedup relies on the 'slack:<channel>:<ts>' external_ref format."""
    h = make_slack()

    _, mid = await deliver(h.adapter, h.thread_key, "hi", conversation_id=h.conversation_id)

    msgs = message_store.get_messages(h.conversation_id)
    assert mid and len(msgs) == 1


def test_legacy_pending_transcription_keys_are_migrated_on_load(tmp_path, monkeypatch):
    """pending_transcriptions.json entries keyed '<channel>:<msg_ts>' are re-keyed
    by MessageRef on load so the restart-time retry still sees them."""
    f = tmp_path / "pending_transcriptions.json"
    f.write_text(json.dumps({"C1:5.5": {"channel": "C1", "msg_ts": "5.5", "thread_ts": "5.5"}}))
    monkeypatch.setattr(persistence, "PENDING_TRANSCRIPTIONS_FILE", f)

    mapping = persistence._load_pending_transcriptions()

    assert list(mapping) == ["slack:C1:5.5"]


async def test_web_deliver_update_edits_the_existing_row_in_place():
    """Updating a Web message by its message_id must not insert a second row."""
    from adapters import deliver_update
    from contract_harness import make_web

    h = make_web()
    result, mid = await deliver(h.adapter, h.thread_key, "first", conversation_id=h.conversation_id)
    ref = h.adapter.message_ref(h.thread_key, result, mid)

    await deliver_update(h.adapter, h.thread_key, ref, "second", conversation_id=h.conversation_id,
                         message_id=mid)

    msgs = message_store.get_messages(h.conversation_id)
    assert [m["content"] for m in msgs] == ["second"]


async def test_slack_deliver_update_keeps_the_legacy_external_ref():
    """Editing a Slack message re-records under the same 'slack:<ch>:<ts>' ref,
    so dedup/lookup by external_ref keeps finding the one row."""
    from adapters import deliver_update

    h = make_slack()
    result, mid = await deliver(h.adapter, h.thread_key, "first", conversation_id=h.conversation_id)
    ref = h.adapter.message_ref(h.thread_key, result)
    await deliver_update(h.adapter, h.thread_key, ref, "edited", conversation_id=h.conversation_id,
                         message_id=mid)

    found = message_store.find_message_id_by_external_ref(
        h.conversation_id, message_store.slack_external_ref(ref.conversation_key, ref.id))
    assert found == mid
    assert [m["content"] for m in message_store.get_messages(h.conversation_id)] == ["edited"]
