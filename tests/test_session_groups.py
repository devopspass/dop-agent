"""
Tests for session groups ("workstreams") in message_store.

Feature: the user has many sessions belonging to the same task (a master
session spawning dependent sessions). A group is a user-titled bucket: a
session is in at most one group (NULL = Ungrouped), no nesting, one optional
master per group. Archiving a group cascades to its sessions; deleting only
unassigns. Grouping is metadata and must never bump updated_at (that would
reshuffle the most-recent-first session list).
"""

from __future__ import annotations

import pytest

import message_store
from test_message_store import _use_tmp_store


def _two_sessions(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    return message_store.get_or_create_conversation(None), message_store.get_or_create_conversation(None)


def test_assign_and_ungroup_session_roundtrip_in_list(tmp_path, monkeypatch):
    a, b = _two_sessions(tmp_path, monkeypatch)
    g = message_store.create_group("Bot dev")
    message_store.assign_session_group(a, g["id"])
    rows = {r["conversation_id"]: r for r in message_store.list_conversations()}
    assert rows[a]["group_id"] == g["id"] and rows[b]["group_id"] is None
    message_store.assign_session_group(a, None)
    assert message_store.get_session_group_id(a) is None


def test_grouping_does_not_change_updated_at(tmp_path, monkeypatch):
    a, _ = _two_sessions(tmp_path, monkeypatch)
    before = {r["conversation_id"]: r["updated_at"] for r in message_store.list_conversations()}[a]
    message_store.assign_session_group(a, message_store.create_group("X")["id"])
    after = {r["conversation_id"]: r["updated_at"] for r in message_store.list_conversations()}[a]
    assert before == after


def test_only_one_master_per_group_and_must_be_member(tmp_path, monkeypatch):
    a, b = _two_sessions(tmp_path, monkeypatch)
    g = message_store.create_group("G")["id"]
    message_store.assign_session_group(a, g)
    message_store.assign_session_group(b, g)
    message_store.set_group_master(g, a)
    message_store.set_group_master(g, b)
    rows = {r["conversation_id"]: r for r in message_store.list_conversations()}
    assert rows[b]["is_group_master"] and not rows[a]["is_group_master"]
    assert message_store.list_groups()[0]["master_id"] == b
    other = message_store.create_group("Other")["id"]
    with pytest.raises(ValueError):
        message_store.set_group_master(other, a)


def test_moving_session_clears_master_flag(tmp_path, monkeypatch):
    a, _ = _two_sessions(tmp_path, monkeypatch)
    g = message_store.create_group("G")["id"]
    message_store.assign_session_group(a, g)
    message_store.set_group_master(g, a)
    message_store.assign_session_group(a, None)
    assert message_store.list_groups()[0]["master_id"] is None


def test_archiving_group_cascades_to_sessions_and_restores(tmp_path, monkeypatch):
    a, b = _two_sessions(tmp_path, monkeypatch)
    g = message_store.create_group("G")["id"]
    message_store.assign_session_group(a, g)
    assert message_store.set_group_archived(g, True) == 1
    rows = {r["conversation_id"]: r for r in message_store.list_conversations()}
    assert rows[a]["archived"] and not rows[b]["archived"]
    assert message_store.list_groups() == []
    assert len(message_store.list_groups(include_archived=True)) == 1
    message_store.set_group_archived(g, False)
    assert not {r["conversation_id"]: r for r in message_store.list_conversations()}[a]["archived"]


def test_deleting_group_only_unassigns_sessions(tmp_path, monkeypatch):
    a, _ = _two_sessions(tmp_path, monkeypatch)
    g = message_store.create_group("G")["id"]
    message_store.assign_session_group(a, g)
    assert message_store.delete_group(g) == 1
    row = {r["conversation_id"]: r for r in message_store.list_conversations()}[a]
    assert row["group_id"] is None and not row["archived"]


def test_inherit_group_autocreates_group_from_parent_with_parent_as_master(tmp_path, monkeypatch):
    parent, child = _two_sessions(tmp_path, monkeypatch)
    message_store.set_title(parent, "Master task")
    gid = message_store.inherit_group_from_parent(child, parent)
    assert message_store.get_group(gid)["title"] == "Master task"
    rows = {r["conversation_id"]: r for r in message_store.list_conversations()}
    assert rows[parent]["is_group_master"] and rows[child]["group_id"] == gid and not rows[child]["is_group_master"]


def test_inherit_group_reuses_existing_parent_group(tmp_path, monkeypatch):
    parent, child = _two_sessions(tmp_path, monkeypatch)
    g = message_store.create_group("Existing")["id"]
    message_store.assign_session_group(parent, g)
    assert message_store.inherit_group_from_parent(child, parent) == g
    assert len(message_store.list_groups()) == 1


def test_empty_group_title_rejected_and_unknown_ids_raise(tmp_path, monkeypatch):
    a, _ = _two_sessions(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        message_store.create_group("  ")
    with pytest.raises(KeyError):
        message_store.assign_session_group(a, "nope")
    with pytest.raises(KeyError):
        message_store.assign_session_group("nope", None)


def test_group_order_is_persisted_and_exposed_in_session_list(tmp_path, monkeypatch):
    """Grouped view supports drag-and-drop ordering: set_group_order stores each
    session's index, list_conversations exposes it, and unordered ones stay None."""
    a, b = _two_sessions(tmp_path, monkeypatch)
    c = message_store.get_or_create_conversation(None)
    message_store.set_group_order([b, a])
    rows = {r["conversation_id"]: r for r in message_store.list_conversations()}
    assert rows[b]["group_order"] == 0 and rows[a]["group_order"] == 1
    assert rows[c]["group_order"] is None


def test_groups_manual_order_is_persisted_and_returned_by_list_groups(tmp_path, monkeypatch):
    """Grouped view lets the user drag a group header to put a group first:
    set_groups_order stores it and list_groups returns groups in that order
    (new/unordered groups fall back to creation order after the ordered ones)."""
    _use_tmp_store(tmp_path, monkeypatch)
    g1, g2, g3 = (message_store.create_group(t) for t in ("one", "two", "three"))
    message_store.set_groups_order([g3["id"], g1["id"]])
    assert [g["title"] for g in message_store.list_groups()] == ["three", "one", "two"]
