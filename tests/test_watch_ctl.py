"""
Direct unit tests for watchers.py's registry/helper functions.

These tests deliberately exercise the real parser, scheduler, validation,
boot-id, and broken-watch behavior against an isolated sqlite database and
boot-id file, without monkeypatching the helpers out.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import copilot_session
import message_store
import watchers


@pytest.fixture
def isolated_watch_registry(tmp_path, monkeypatch):
    """Point watchers.py at throwaway db/boot-id files so tests never touch the
    developer's real watchers.db or .boot_id, and disable live Slack
    announcements from add_watch()."""

    monkeypatch.setattr(message_store, "DB_PATH", tmp_path / "bot_store.db")
    monkeypatch.setattr(message_store, "_TITLES_FILE", tmp_path / "session_titles.json")
    message_store.init_db()
    monkeypatch.setattr(watchers, "DB_PATH", tmp_path / "watchers.db")
    monkeypatch.setattr(watchers, "BOOT_ID_FILE", tmp_path / ".boot_id")
    monkeypatch.setattr(watchers, "_post_watch_announcement", lambda *a, **kw: False)
    monkeypatch.setattr(watchers, "check_watch_resource_exists", lambda *a, **kw: None)
    watchers.init_db()
    return tmp_path


def test_parse_time_key_accepts_relative_minutes():
    assert watchers.parse_time_key("in:5m") == {"kind": "in", "amount": 5, "unit": "m"}


def test_parse_time_key_accepts_relative_hours():
    assert watchers.parse_time_key("in:2h") == {"kind": "in", "amount": 2, "unit": "h"}


def test_parse_time_key_accepts_weekly_cron():
    assert watchers.parse_time_key("cron:mon 09:30") == {
        "kind": "cron_weekly",
        "dow": "mon",
        "hour": 9,
        "minute": 30,
    }


@pytest.mark.parametrize(
    "key, pattern",
    [
        ("in:5x", "time watch key must be one of"),
        ("cron:mon 24:00", "invalid time"),
        ("at:2026-02-30T09:00", "invalid date/time"),
    ],
)
def test_parse_time_key_rejects_invalid_formats(key, pattern):
    with pytest.raises(ValueError, match=pattern):
        watchers.parse_time_key(key)


def test_compute_next_fire_for_relative_watch():
    now = datetime(2026, 9, 21, 12, 0, 0)
    parsed = watchers.parse_time_key("in:2h")
    assert watchers.compute_next_fire(parsed, now=now) == datetime(2026, 9, 21, 14, 0, 0)


def test_compute_next_fire_for_weekly_cron_watch():
    now = datetime(2026, 9, 21, 8, 0, 0)  # Monday
    parsed = watchers.parse_time_key("cron:mon 09:30")
    assert watchers.compute_next_fire(parsed, now=now) == datetime(2026, 9, 21, 9, 30, 0)


def test_advance_recurring_daily_watch_skips_missed_occurrences():
    parsed = watchers.parse_time_key("cron:daily 09:30")
    previous_fire = datetime(2026, 9, 20, 9, 30, 0)
    now = datetime(2026, 9, 23, 8, 0, 0)
    assert watchers.advance_recurring(parsed, previous_fire, now=now) == datetime(2026, 9, 23, 9, 30, 0)


def test_is_recurring_time_kind_distinguishes_one_shot_and_recurring():
    assert watchers.is_recurring_time_kind("cron_daily") is True
    assert watchers.is_recurring_time_kind("cron_weekly") is True
    assert watchers.is_recurring_time_kind("in") is False
    assert watchers.is_recurring_time_kind("at") is False


@pytest.mark.parametrize(
    "type_, key",
    [
        ("time", "in:5m"),
        ("script", "cron:*/10 * * * *"),
        ("self_restart", None),
    ],
)
def test_validate_key_accepts_supported_watch_types(type_, key):
    watchers.validate_key(type_, key)


@pytest.mark.parametrize(
    "type_, key, pattern",
    [
        ("time", "later today", "time watch key must be one of"),
        ("mr", "group/project#42", "Unknown watch type"),
    ],
)
def test_validate_key_rejects_invalid_keys(type_, key, pattern):
    with pytest.raises(ValueError, match=pattern):
        watchers.validate_key(type_, key)


def _slack_conversation_id(channel: str, thread_ts: str) -> str:
    return copilot_session._conversation_id_for(channel, thread_ts)


def test_rotate_boot_id_and_due_self_restart_watches_detect_restart(isolated_watch_registry):
    """self_restart watches should wake only after a new boot generation, not
    within the same boot id they were created under."""

    old_boot_id, first_boot_id = watchers.rotate_boot_id()
    assert old_boot_id == ""
    assert first_boot_id == watchers.current_boot_id()

    conversation_id = _slack_conversation_id("C123", "111.222")
    due_immediately = watchers.add_watch(
        "self_restart",
        conversation_id,
        label="resume me",
        announce=False,
    )
    same_boot_due = watchers.due_self_restart_watches(first_boot_id)
    assert same_boot_due == []

    previous_boot_id, second_boot_id = watchers.rotate_boot_id()
    assert previous_boot_id == first_boot_id
    due_after_restart = watchers.due_self_restart_watches(second_boot_id)
    assert [w["id"] for w in due_after_restart] == [due_immediately]
    assert due_after_restart[0]["status"] == "active"


def test_mark_broken_persists_reason_and_hides_watch_from_active_listing(isolated_watch_registry):
    """    Poller contract: once a malformed watch is
    marked broken, watcher_loop()'s default active listing does not include
    it, but include_inactive=True should still surface it for diagnosis."""

    conversation_id = _slack_conversation_id("C123", "111.222")
    watch_id = watchers.add_watch(
        "time",
        conversation_id,
        key="in:5m",
        label="broken later",
        announce=False,
    )

    assert watchers.mark_broken(watch_id, "bad key format") is True

    broken = watchers.get_watch(watch_id)
    assert broken is not None
    assert broken["status"] == "broken"
    assert "bad key format" in broken["meta"]

    listed_ids = [w["id"] for w in watchers.list_watches()]
    active_ids = [w["id"] for w in watchers.list_watches() if w["status"] == "active"]
    all_ids = [w["id"] for w in watchers.list_watches(include_inactive=True)]
    assert watch_id in listed_ids
    assert watch_id not in active_ids
    assert watch_id in all_ids
