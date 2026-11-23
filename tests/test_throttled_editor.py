"""ThrottledEditor guarantees beyond the spec's coalescing test."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "adapter_contract"))

from adapters import MessageRef, ThrottledEditor  # noqa: E402
from contract_harness import FAKE_PLATFORM, make_fake  # noqa: E402


def _ref():
    return MessageRef(platform=FAKE_PLATFORM, conversation_key="42", id="1")


async def test_close_drops_pending_status_so_terminal_edit_is_never_overtaken():
    """A throttled 'Working' edit still pending when the turn ends must be
    discarded by close(); otherwise it could land after the final answer."""
    now = [0.0]
    fake = make_fake()
    ed = ThrottledEditor(fake.adapter, fake.thread_key, clock=lambda: now[0])
    await ed.edit(_ref(), "first", conversation_id=fake.conversation_id)
    now[0] += 0.1
    await ed.edit(_ref(), "stale", conversation_id=fake.conversation_id)
    await ed.close()
    await ed.flush()
    await asyncio.sleep(0)
    assert [e["text"] for e in fake.adapter.edits] == ["first"]


async def test_trailing_pending_edit_is_sent_by_timer_after_interval(monkeypatch):
    """The newest coalesced status must eventually appear even if no further
    edit arrives (trailing-edge send)."""
    fake = make_fake()
    fake.adapter.capabilities = type(fake.adapter.capabilities)(
        **{**fake.adapter.capabilities.__dict__, "edit_min_interval": 0.05})
    ed = ThrottledEditor(fake.adapter, fake.thread_key)
    await ed.edit(_ref(), "a", conversation_id=fake.conversation_id)
    await ed.edit(_ref(), "b", conversation_id=fake.conversation_id)
    await asyncio.sleep(0.2)
    assert [e["text"] for e in fake.adapter.edits] == ["a", "b"]


async def test_zero_interval_adapters_send_every_edit_immediately():
    """Slack/Web have interval 0: no coalescing, behaviour unchanged."""
    fake = make_fake()
    fake.adapter.capabilities = type(fake.adapter.capabilities)(
        **{**fake.adapter.capabilities.__dict__, "edit_min_interval": 0.0})
    ed = ThrottledEditor(fake.adapter, fake.thread_key)
    for t in "abc":
        await ed.edit(_ref(), t, conversation_id=fake.conversation_id)
    assert [e["text"] for e in fake.adapter.edits] == ["a", "b", "c"]
