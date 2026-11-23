"""
Unit tests for crash_recovery.py — reading back a lost final answer from
the Copilot session's own durable events.jsonl log, and the
message_store-backed lookup that determines the "after" boundary.
"""

from __future__ import annotations

import json

import crash_recovery
import message_store


def _use_tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(message_store, "DB_PATH", tmp_path / "bot_store.db")
    monkeypatch.setattr(message_store, "_TITLES_FILE", tmp_path / "session_titles.json")
    message_store.init_db()


def _write_events(path, events):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


# --- _find_last_assistant_message_after -------------------------------------

def test_find_last_assistant_message_after_no_session_id():
    assert crash_recovery._find_last_assistant_message_after(None, None) is None


def test_find_last_assistant_message_after_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(crash_recovery, "SESSION_STATE_DIR", tmp_path)
    assert crash_recovery._find_last_assistant_message_after("no-such-session", None) is None


def test_find_last_assistant_message_after_returns_latest_content(monkeypatch, tmp_path):
    monkeypatch.setattr(crash_recovery, "SESSION_STATE_DIR", tmp_path)
    events_path = tmp_path / "9483f12e-25a0-5472-ad91-b55b35598e8e" / "events.jsonl"
    _write_events(events_path, [
        {"type": "assistant.message", "timestamp": "2024-01-01T00:00:00Z", "data": {"content": "first"}},
        {"type": "assistant.message", "timestamp": "2024-01-01T00:01:00Z", "data": {"content": "second"}},
        {"type": "tool.call", "timestamp": "2024-01-01T00:02:00Z", "data": {}},
    ])
    result = crash_recovery._find_last_assistant_message_after("9483f12e-25a0-5472-ad91-b55b35598e8e", None)
    assert result == "second"


def test_find_last_assistant_message_after_respects_boundary(monkeypatch, tmp_path):
    monkeypatch.setattr(crash_recovery, "SESSION_STATE_DIR", tmp_path)
    events_path = tmp_path / "49a6797e-877c-51c6-aeef-76564bf031a3" / "events.jsonl"
    _write_events(events_path, [
        {"type": "assistant.message", "timestamp": "2024-01-01T00:00:00Z", "data": {"content": "old"}},
        {"type": "assistant.message", "timestamp": "2024-01-01T00:01:00Z", "data": {"content": "new"}},
    ])
    # A boundary equal to an entry's timestamp excludes entries at or before it;
    # only strictly-newer content is kept.
    result = crash_recovery._find_last_assistant_message_after("49a6797e-877c-51c6-aeef-76564bf031a3", "2024-01-01T00:00:00Z")
    assert result == "new"


def test_find_last_assistant_message_after_boundary_after_latest_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(crash_recovery, "SESSION_STATE_DIR", tmp_path)
    events_path = tmp_path / "975afadd-66b6-5ce2-a740-b89120a17302" / "events.jsonl"
    _write_events(events_path, [
        {"type": "assistant.message", "timestamp": "2024-01-01T00:00:00Z", "data": {"content": "old"}},
    ])
    result = crash_recovery._find_last_assistant_message_after("975afadd-66b6-5ce2-a740-b89120a17302", "2024-01-02T00:00:00Z")
    assert result is None


def test_find_last_assistant_message_after_ignores_corrupt_lines(monkeypatch, tmp_path):
    monkeypatch.setattr(crash_recovery, "SESSION_STATE_DIR", tmp_path)
    events_path = tmp_path / "1a8c7b26-090f-514f-9357-699183c19c3b" / "events.jsonl"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    with events_path.open("w", encoding="utf-8") as f:
        f.write("not valid json\n")
        f.write(json.dumps({"type": "assistant.message", "timestamp": "t", "data": {"content": "ok"}}) + "\n")
        f.write("\n")  # blank line skipped
    result = crash_recovery._find_last_assistant_message_after("1a8c7b26-090f-514f-9357-699183c19c3b", None)
    assert result == "ok"


def test_find_last_assistant_message_after_empty_content_ignored(monkeypatch, tmp_path):
    monkeypatch.setattr(crash_recovery, "SESSION_STATE_DIR", tmp_path)
    events_path = tmp_path / "15434d46-c6bd-5dbc-bf83-9e7a7872e898" / "events.jsonl"
    _write_events(events_path, [
        {"type": "assistant.message", "timestamp": "t1", "data": {"content": ""}},
        {"type": "assistant.message", "timestamp": "t2", "data": {}},
    ])
    result = crash_recovery._find_last_assistant_message_after("15434d46-c6bd-5dbc-bf83-9e7a7872e898", None)
    assert result is None


def test_find_last_assistant_message_after_strips_whitespace(monkeypatch, tmp_path):
    monkeypatch.setattr(crash_recovery, "SESSION_STATE_DIR", tmp_path)
    events_path = tmp_path / "9f7bcde7-774d-588f-88db-b949b9503b9b" / "events.jsonl"
    _write_events(events_path, [
        {"type": "assistant.message", "timestamp": "t1", "data": {"content": "  padded text  "}},
    ])
    result = crash_recovery._find_last_assistant_message_after("9f7bcde7-774d-588f-88db-b949b9503b9b", None)
    assert result == "padded text"


# --- _recover_lost_response ---------------------------------------------------

def test_recover_lost_response_no_conversation_id():
    assert crash_recovery._recover_lost_response(None) is None


def test_recover_lost_response_no_session_or_messages(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv = message_store.get_or_create_conversation(None)
    # No copilot session id set, no messages recorded.
    assert crash_recovery._recover_lost_response(conv) is None


def test_recover_lost_response_returns_recovered_text(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    monkeypatch.setattr(crash_recovery, "SESSION_STATE_DIR", tmp_path)

    conv = message_store.get_or_create_conversation(None, copilot_session_id="45da0cab-d2bc-59de-9acd-27782425ee2a")
    message_store.record_message(conv, "status", "Working...")

    messages = message_store.get_messages(conv)
    boundary = messages[-1]["created_at"]

    events_path = tmp_path / "45da0cab-d2bc-59de-9acd-27782425ee2a" / "events.jsonl"
    _write_events(events_path, [
        {"type": "assistant.message", "timestamp": "9999-01-01T00:00:00Z", "data": {"content": "recovered answer"}},
    ])

    result = crash_recovery._recover_lost_response(conv)
    assert result == "recovered answer"


def test_recover_lost_response_message_store_exception_returns_none(monkeypatch):
    def boom(_conv_id):
        raise RuntimeError("db error")

    monkeypatch.setattr(message_store, "get_copilot_session_id", boom)
    assert crash_recovery._recover_lost_response("some-conv") is None


def test_slack_bot_inject_watch_message_calls_match_signature():
    """Every _inject_watch_message() call (e.g. from _resume_interrupted_turns) matches its signature,
    so auto-resume after restarts does not break."""
    import ast
    import inspect
    from pathlib import Path

    import watchers

    sig = inspect.signature(watchers._inject_watch_message)
    tree = ast.parse((Path(__file__).parent.parent / "dop_agent.py").read_text())
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_inject_watch_message"
    ]
    assert calls, "expected at least one _inject_watch_message call in dop_agent.py"
    for c in calls:
        sig.bind(*[None] * len(c.args), **{k.arg: None for k in c.keywords})


def test_find_last_assistant_message_after_rejects_non_uuid_and_traversal_session_ids(monkeypatch, tmp_path):
    """Security: a session id that is not a UUID (e.g. '../x') must never be turned
    into a path under SESSION_STATE_DIR, even if an events.jsonl exists there."""
    monkeypatch.setattr(crash_recovery, "SESSION_STATE_DIR", tmp_path / "state")
    evil = tmp_path / "evil"
    evil.mkdir()
    (evil / "events.jsonl").write_text('{"type":"assistant.message","data":{"content":"x"}}\n')
    (tmp_path / "state").mkdir()
    assert crash_recovery._find_last_assistant_message_after("../evil", None) is None
