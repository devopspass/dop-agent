"""
Unit tests for the watcher-noise reductions in watchers.py.

In a session full of watches, meaningful bot messages must not get lost
among watcher registrations, fires and the agent's one-line replies to them.
These tests cover the backend half:
  * stale guard    - a watch removed while its poll was in flight must not
    fire late;
  * coalescing     - fires for one conversation within a short window become
    ONE agent turn (raw notices still all posted) and carry the ACK/final
    report rules;
  * final-fire report - a flagged-final fire answered with a thin
    one-liner triggers exactly one re-prompt for a real report.
"""

from __future__ import annotations

import asyncio

import pytest

import message_store
import watchers


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(message_store, "DB_PATH", tmp_path / "bot_store.db")
    monkeypatch.setattr(message_store, "_TITLES_FILE", tmp_path / "titles.json")
    message_store.init_db()
    yield


@pytest.fixture
def recorder(monkeypatch):
    calls = []

    async def fake_now(conversation_id, text, *, rules=False, final=False):
        calls.append({"cid": conversation_id, "text": text, "rules": rules, "final": final})

    monkeypatch.setattr(watchers, "_inject_watch_message_now", fake_now)
    monkeypatch.setattr(watchers, "FIRE_COALESCE_SECS", 0.05)
    monkeypatch.setattr(watchers, "get_convo_if_exists", lambda cid: None)
    monkeypatch.setattr(watchers, "_is_coalesced_type", lambda wtype: wtype == "script")
    return calls


def test_stale_watch_fire_is_dropped(monkeypatch, recorder):
    monkeypatch.setattr(watchers, "_watch_is_stale", lambda wid: True)

    async def run():
        token = watchers._fire_ctx.set((9, "mr"))
        try:
            await watchers._inject_watch_message("c1", "late fire")
            await asyncio.sleep(0.2)
        finally:
            watchers._fire_ctx.reset(token)

    asyncio.run(run())
    assert recorder == []


def test_fires_within_window_are_merged_into_one_turn_with_rules(monkeypatch, recorder):
    monkeypatch.setattr(watchers, "_watch_is_stale", lambda wid: False)
    monkeypatch.setattr(watchers, "_watch_is_final", lambda wid: False)

    async def run():
        token = watchers._fire_ctx.set((1, "script"))
        try:
            await watchers._inject_watch_message("c1", "note fire")
            await watchers._inject_watch_message("c1", "pipeline fire")  # same poll, returns at once
            await asyncio.sleep(0.3)
        finally:
            watchers._fire_ctx.reset(token)

    asyncio.run(run())
    assert len(recorder) == 1
    assert recorder[0]["text"] == ["note fire", "pipeline fire"]
    assert recorder[0]["rules"] is True


def test_time_watch_fire_is_never_wrapped_with_reply_rules(monkeypatch, recorder):
    monkeypatch.setattr(watchers, "_watch_is_stale", lambda wid: False)

    async def run():
        token = watchers._fire_ctx.set((2, "time"))
        try:
            await watchers._inject_watch_message("c1", "daily review prompt")
        finally:
            watchers._fire_ctx.reset(token)

    asyncio.run(run())
    assert recorder == [{"cid": "c1", "text": "daily review prompt", "rules": False, "final": False}]


def test_meta_final_flag_marks_batch_final(monkeypatch, recorder):
    monkeypatch.setattr(watchers, "_watch_is_stale", lambda wid: False)
    monkeypatch.setattr(watchers, "_watch_is_final", lambda wid: True)

    async def run():
        token = watchers._fire_ctx.set((3, "script"))
        try:
            await watchers._inject_watch_message("c1", "ticket changed")
            await asyncio.sleep(0.3)
        finally:
            watchers._fire_ctx.reset(token)

    asyncio.run(run())
    assert recorder[0]["final"] is True


def test_no_context_means_plain_delivery(recorder):
    asyncio.run(watchers._inject_watch_message("c1", "restart notice"))
    assert recorder == [{"cid": "c1", "text": "restart notice", "rules": False, "final": False}]


def _final_fire_env(monkeypatch, tmp_path, replies):
    monkeypatch.setattr(message_store, "DB_PATH", tmp_path / "bot_store.db")
    monkeypatch.setattr(message_store, "_TITLES_FILE", tmp_path / "titles.json")
    message_store.init_db()
    conv = message_store.get_or_create_conversation(None)
    prompts = []

    async def fake_handle(**kwargs):
        prompts.append(kwargs["text"])
        message_store.record_message(conv, "assistant", replies[min(len(prompts) - 1, len(replies) - 1)])

    watchers.configure(type("A", (), {"client": None})(), fake_handle)
    monkeypatch.setattr(watchers, "_resolve_delivery", lambda cid: (None, None))
    monkeypatch.setattr(watchers, "get_convo_if_exists", lambda cid: None)
    return conv, prompts


PIPE_OK = ":white_check_mark: *GitLab MR watch fired — its pipeline* — MR !5\nPipeline #77: `success`"


def test_thin_reply_to_final_fire_triggers_one_reprompt(monkeypatch, tmp_path):
    conv, prompts = _final_fire_env(monkeypatch, tmp_path, ["ok, pipeline is green now", "Pipeline green | https://gl/mr/5 | Next: review"])
    try:
        asyncio.run(watchers._inject_watch_message_now(conv, [PIPE_OK], rules=True, final=True))
    finally:
        watchers.configure(None, None)
    assert len(prompts) == 2
    assert prompts[1] == watchers.FINAL_REPORT_REPROMPT


def test_good_report_to_final_fire_is_not_reprompted(monkeypatch, tmp_path):
    good = "Pipeline green for [!5](https://gl/mr/5) - review needed.\nNext: please review."
    conv, prompts = _final_fire_env(monkeypatch, tmp_path, [good])
    try:
        asyncio.run(watchers._inject_watch_message_now(conv, [PIPE_OK], rules=True))
    finally:
        watchers.configure(None, None)
    assert len(prompts) == 1


def test_thin_reply_to_non_final_fire_is_left_alone(monkeypatch, tmp_path):
    conv, prompts = _final_fire_env(monkeypatch, tmp_path, ["ACK"])
    note = ":twisted_rightwards_arrows: *GitLab MR watch fired* — MR !5\n\nNew note(s):\n• a: b"
    try:
        asyncio.run(watchers._inject_watch_message_now(conv, [note], rules=True))
    finally:
        watchers.configure(None, None)
    assert len(prompts) == 1
