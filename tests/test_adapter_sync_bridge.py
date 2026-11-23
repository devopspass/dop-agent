"""Sync->async bridge used by synchronous callers (MCP tool, CLI) that must
send through an async adapter (``adapters.run_sync`` and the immediate watch
announcement in ``watchers._post_watch_announcement``)."""

import asyncio
import json
import threading

import pytest

import adapters
import message_store
import watchers
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "adapter_contract"))

from fake_adapter import FAKE_PLATFORM, FakeAdapter


def test_run_sync_from_a_worker_thread_executes_on_the_bot_loop():
    """A sync caller on another thread gets its coroutine run on the bot's
    own loop (where the platform HTTP client lives) and receives the result."""
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    adapters.set_main_loop(loop)
    try:
        async def where():
            return threading.current_thread()

        assert adapters.run_sync(where()) is t
    finally:
        adapters.set_main_loop(None)
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=2)


async def test_run_sync_inside_a_running_loop_raises_instead_of_deadlocking():
    """Blocking on the loop's own thread would deadlock; it must fail fast."""
    async def noop():
        return 1

    with pytest.raises(RuntimeError):
        adapters.run_sync(noop())


@pytest.fixture
def fake_bound_watch():
    fake = FakeAdapter()
    adapters.REGISTRY.register(fake)
    conv = message_store.get_or_create_conversation(None)
    message_store.bind_conversation(conv, FAKE_PLATFORM, "42:7")
    watch_id = watchers.add_watch("time", conv, "cron:mon 09:30", "t", announce=False)
    yield fake, conv, watch_id
    adapters.REGISTRY.unregister(FAKE_PLATFORM)


def _announced(watch_id):
    return bool(json.loads(watchers.get_watch(watch_id).get("meta") or "{}").get("announced"))


async def test_announcement_on_the_bot_loop_claims_announced_after_a_successful_send(fake_bound_watch):
    """On the bot loop the immediate announcement is a background task: the
    watch ends up ``announced`` and the text reached the bound platform."""
    fake, conv, watch_id = fake_bound_watch

    assert watchers._post_watch_announcement(watch_id, "time", "k", "l", conv, "", None, "2026-01-01T00:00:00") is False
    await asyncio.gather(*watchers._BACKGROUND_TASKS)

    assert _announced(watch_id) and fake.sent


async def test_announcement_failure_on_the_bot_loop_releases_the_claim_for_tick_retry(fake_bound_watch, monkeypatch):
    """If the background send fails, ``announced`` is cleared again so the
    watcher tick's ``_announce_new_watches`` retries instead of losing it."""
    fake, conv, watch_id = fake_bound_watch

    async def boom(*a, **k):
        raise RuntimeError("platform down")

    monkeypatch.setattr(fake, "send", boom)
    watchers._post_watch_announcement(watch_id, "time", "k", "l", conv, "", None, "2026-01-01T00:00:00")
    await asyncio.gather(*watchers._BACKGROUND_TASKS)

    assert not _announced(watch_id)
