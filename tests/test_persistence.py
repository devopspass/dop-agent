"""
Unit tests for persistence.py — session-id persistence (via message_store),
visible-message recording helpers, and the crash-safe active-status /
pending-transcription JSON sweep files.
"""

from __future__ import annotations

import asyncio
import json

import message_store
import persistence


def _use_tmp_store(tmp_path, monkeypatch):
    """Point message_store at a fresh DB + titles file inside tmp_path."""
    monkeypatch.setattr(message_store, "DB_PATH", tmp_path / "bot_store.db")
    monkeypatch.setattr(message_store, "_TITLES_FILE", tmp_path / "session_titles.json")
    message_store.init_db()


def _run(coro):
    return asyncio.run(coro)


def test_get_and_set_saved_session_id_roundtrip(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv = message_store.get_or_create_conversation(None)

    assert persistence._get_saved_session_id(conv) is None
    persistence._set_saved_session_id(conv, "session-abc")
    assert persistence._get_saved_session_id(conv) == "session-abc"


def test_slack_external_ref_returns_none_without_channel_or_ts():
    assert persistence._slack_external_ref("", "123.456") is None
    assert persistence._slack_external_ref("C1", None) is None


def test_slack_external_ref_delegates_to_message_store():
    assert persistence._slack_external_ref("C1", "123.456") == "slack:C1:123.456"


def test_message_id_for_slack_message_roundtrip(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv = message_store.get_or_create_conversation(None)

    message_id = persistence._record_visible_message(
        conv, "assistant", "hello", channel="C1", ts="111.222",
    )
    assert message_id is not None

    found = persistence._message_id_for_slack_message(conv, "C1", "111.222")
    assert found == message_id


def test_message_id_for_slack_message_returns_none_without_conversation_id():
    assert persistence._message_id_for_slack_message(None, "C1", "111.222") is None


def test_record_visible_message_returns_none_without_conversation_id():
    assert persistence._record_visible_message(None, "user", "hi") is None


def test_record_visible_message_persists_to_store(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv = message_store.get_or_create_conversation(None)

    persistence._record_visible_message(conv, "user", "hello there")

    messages = message_store.get_messages(conv)
    assert [m["content"] for m in messages] == ["hello there"]


def test_active_status_register_load_clear_roundtrip(tmp_path, monkeypatch):
    status_file = tmp_path / "active_status.json"
    monkeypatch.setattr(persistence, "ACTIVE_STATUS_FILE", status_file)

    persistence._register_active_status("C1", "100.1", thread_ts="100.1", msg_ts="101.1")

    mapping = persistence._load_active_status()
    assert mapping["slack:C1:100.1"] == {
        "channel": "C1", "ts": "100.1", "thread_ts": "100.1", "msg_ts": "101.1",
    }
    assert json.loads(status_file.read_text()) == mapping

    persistence._clear_active_status("C1", "100.1")
    assert persistence._load_active_status() == {}


def test_active_status_load_returns_empty_dict_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(persistence, "ACTIVE_STATUS_FILE", tmp_path / "does-not-exist.json")
    assert persistence._load_active_status() == {}


def test_active_status_load_returns_empty_dict_on_corrupt_json(tmp_path, monkeypatch):
    bad_file = tmp_path / "active_status.json"
    bad_file.write_text("{not valid json")
    monkeypatch.setattr(persistence, "ACTIVE_STATUS_FILE", bad_file)
    assert persistence._load_active_status() == {}


def test_clear_active_status_noop_when_key_absent(tmp_path, monkeypatch):
    status_file = tmp_path / "active_status.json"
    monkeypatch.setattr(persistence, "ACTIVE_STATUS_FILE", status_file)
    # No file exists yet — clearing a nonexistent key must not raise or create one.
    persistence._clear_active_status("C1", "999.999")
    assert not status_file.exists()


def test_pending_transcription_register_load_clear_roundtrip(tmp_path, monkeypatch):
    pending_file = tmp_path / "pending_transcriptions.json"
    monkeypatch.setattr(persistence, "PENDING_TRANSCRIPTIONS_FILE", pending_file)

    persistence._register_pending_transcription(
        "C1", "500.1", thread_ts="500.1", conversation_id="conv-1",
        user_id="U1", file_info={"id": "F1", "url_private": "https://x"},
        message_id="m1",
    )

    mapping = persistence._load_pending_transcriptions()
    assert mapping["slack:C1:500.1"]["conversation_id"] == "conv-1"
    assert mapping["slack:C1:500.1"]["file"]["id"] == "F1"
    assert mapping["slack:C1:500.1"]["message_id"] == "m1"

    persistence._clear_pending_transcription("C1", "500.1")
    assert persistence._load_pending_transcriptions() == {}


def test_pending_transcription_load_returns_empty_dict_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(persistence, "PENDING_TRANSCRIPTIONS_FILE", tmp_path / "nope.json")
    assert persistence._load_pending_transcriptions() == {}
