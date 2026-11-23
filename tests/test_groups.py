"""
Tests for groups.py: status rollups, overview aggregate and new-session
group suggestion (session-groups feature). Pure functions over session
payload dicts + a throwaway message_store.
"""

from __future__ import annotations

import groups
import message_store
from test_message_store import _use_tmp_store


def test_session_status_priority_errored_running_unread_idle():
    assert groups.session_status({"errored": True, "running": True}) == "errored"
    assert groups.session_status({"running": True}) == "running"
    assert groups.session_status({"last_role": "assistant", "updated_at": "2026-01-02", "last_read_at": "2026-01-01"}) == "unread"
    assert groups.session_status({"last_role": "assistant", "updated_at": "2026-01-01", "last_read_at": "2026-01-02"}) == "idle"


def test_rollup_reports_most_attention_worthy_status_and_skips_archived():
    r = groups.rollup([{"running": True}, {"errored": True, "archived": True}, {}])
    assert r["status"] == "running" and r["counts"]["errored"] == 0 and r["counts"]["idle"] == 1


def test_extract_artifacts_finds_mrs_and_tickets_deduplicated():
    a = groups.extract_artifacts([
        "see https://gitlab.example.com/a/b/-/merge_requests/12 and PRJ-123",
        "again https://gitlab.example.com/a/b/-/merge_requests/12 plus https://jira.example.net/browse/PRJ-9",
    ])
    assert a["merge_requests"] == ["https://gitlab.example.com/a/b/-/merge_requests/12"]
    assert set(a["tickets"]) == {"PRJ-123", "PRJ-9"}


def test_overview_puts_master_first_and_aggregates_artifacts(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    g = message_store.create_group("Bot dev")["id"]
    ids = [message_store.get_or_create_conversation(None) for _ in range(2)]
    for cid in ids:
        message_store.assign_session_group(cid, g)
    message_store.set_group_master(g, ids[1])
    message_store.record_message(ids[0], "assistant", "opened PRJ-77 https://gl.x/a/-/merge_requests/5")
    sessions = message_store.list_conversations()
    ov = groups.overview(g, sessions, [])
    assert ov["master"]["conversation_id"] == ids[1]
    assert ov["sessions"][0]["conversation_id"] == ids[1]
    assert ov["group"]["title"] == "Bot dev"
    assert ov["artifacts"]["tickets"] == ["PRJ-77"]
    assert ov["artifacts"]["merge_requests"] == ["https://gl.x/a/-/merge_requests/5"]


def test_suggest_group_matches_shared_ticket_and_ignores_weak_overlap(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    g = message_store.create_group("PRJ-555 proxy rollout")
    gl = message_store.list_groups()
    assert groups.suggest_group("please look at PRJ-555 again", gl, [])["group_id"] == g["id"]
    assert groups.suggest_group("something totally unrelated", gl, []) is None
