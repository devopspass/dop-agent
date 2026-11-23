"""session_turn watches must fire INSTANTLY when the target's turn ends.

Chained phases (e.g. four-phase-plan self-watches) must not wait for the
WATCHER_POLL_INTERVAL (60s) loop. CopilotConversation.send()'s finally: notifies turn-complete listeners;
watchers._dispatch_turn_complete polls the matching watches right away
(after a short grace so the final reply is posted first), guarded against
double fire with the periodic loop and skipped if the target is busy again.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import copilot_session
import message_store
import watchers


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(watchers, "DB_PATH", tmp_path / "watchers.db")
    monkeypatch.setattr(watchers, "TURN_COMPLETE_GRACE_SECS", 0.01)
    watchers.init_db()
    cid = message_store.get_or_create_conversation(None)
    fired = []

    async def fake_inject(conversation_id, text):
        fired.append((conversation_id, text))

    monkeypatch.setattr(watchers, "_inject_watch_message", fake_inject)
    convo = copilot_session.get_convo(cid)
    convo._turn_generation = 1
    convo._last_completed_turn_generation = 0
    convo.turn_started_at = 1.0
    wid = watchers.add_watch("session_turn", cid, key="self", label="next", announce=False)
    return cid, convo, wid, fired


def _finish_turn(convo):
    convo._last_completed_turn_generation = 1
    convo.turn_started_at = None


def test_session_turn_watch_fires_immediately_on_turn_completion_without_waiting_for_poll_tick(setup):
    cid, convo, wid, fired = setup
    _finish_turn(convo)
    asyncio.run(watchers._dispatch_turn_complete(cid))
    assert len(fired) == 1
    assert watchers.get_watch(wid)["status"] == "done"


def test_concurrent_loop_and_instant_dispatch_never_double_fire(setup):
    cid, convo, wid, fired = setup
    _finish_turn(convo)

    async def run():
        w = watchers.get_watch(wid)
        await asyncio.gather(
            watchers._poll_session_turn_guarded(w),
            watchers._poll_session_turn_guarded(w),
            watchers._dispatch_turn_complete(cid),
        )

    asyncio.run(run())
    assert len(fired) == 1


def test_instant_dispatch_skipped_when_target_is_busy_again_after_grace(setup, monkeypatch):
    cid, convo, wid, fired = setup
    _finish_turn(convo)
    monkeypatch.setattr(watchers, "TURN_COMPLETE_MAX_WAIT_SECS", 0.05)
    monkeypatch.setattr(convo, "is_busy", lambda: True)
    asyncio.run(watchers._dispatch_turn_complete(cid))
    assert fired == []
    assert watchers.get_watch(wid)["status"] == "active"  # periodic loop is the fallback


def test_turn_complete_listener_exception_does_not_break_turn_and_is_idempotent(monkeypatch):
    calls = []
    monkeypatch.setattr(copilot_session, "_turn_complete_listeners", [])

    def bad(cid):
        calls.append(cid)
        raise RuntimeError("boom")

    copilot_session.add_turn_complete_listener(bad)
    copilot_session.add_turn_complete_listener(bad)
    copilot_session._notify_turn_complete("c1")
    assert calls == ["c1"]


def test_on_turn_complete_is_sync_and_only_schedules_task(setup):
    cid, convo, wid, fired = setup
    _finish_turn(convo)

    async def run():
        watchers._on_turn_complete(cid)
        assert fired == []  # nothing awaited inline
        await asyncio.gather(*list(watchers._turn_complete_tasks))

    asyncio.run(run())
    assert len(fired) == 1


def test_real_send_finally_block_notifies_listeners_after_generation_persisted(tmp_path, monkeypatch):
    """The listener hook must be wired into send()'s finally: (not just
    callable in isolation): drive the real send() with a fake session and
    check the listener sees the already-advanced completed generation."""
    monkeypatch.setattr(watchers, "DB_PATH", tmp_path / "watchers.db")
    cid = message_store.get_or_create_conversation(None)
    convo = copilot_session.CopilotConversation(cid)
    seen = []
    monkeypatch.setattr(copilot_session, "_turn_complete_listeners", [])
    copilot_session.add_turn_complete_listener(
        lambda c: seen.append((c, convo.last_completed_turn_generation()))
    )

    async def fake_ensure():
        class S:
            def on(self, *a, **k):
                return lambda: None

            async def send_and_wait(self, *a, **k):
                raise RuntimeError("boom")

        convo._session = S()

    monkeypatch.setattr(convo, "_ensure_session", fake_ensure)
    try:
        asyncio.run(convo.send("hi"))
    except Exception:
        pass
    assert seen == [(cid, 1)]
