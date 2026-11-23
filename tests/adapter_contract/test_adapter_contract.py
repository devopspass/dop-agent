"""Shared adapter contract — what EVERY chat adapter must do.

Runs the same assertions against every harness in ``HARNESS_FACTORIES``
(SlackAdapter, WebAdapter, the in-memory FakeAdapter, TelegramAdapter); a
new platform is "done" when it is added there and passes unchanged.

Scope: the base ``Adapter`` protocol. Registry/capability/neutral-message
properties are covered by ``test_adapter_target_interface.py``.
"""

from __future__ import annotations

import pytest

import message_store
from adapters import deliver, deliver_update
from adapters import Adapter
from contract_harness import HARNESS_FACTORIES


@pytest.fixture(params=sorted(HARNESS_FACTORIES))
def h(request):
    return HARNESS_FACTORIES[request.param]()


async def test_adapter_satisfies_the_adapter_protocol(h):
    """Every adapter exposes the full Adapter protocol surface."""
    assert isinstance(h.adapter, Adapter)


async def test_send_delivers_the_text_and_returns_result_and_message_id_pair(h):
    result_and_id = await deliver(h.adapter,
        h.thread_key, "hello", conversation_id=h.conversation_id, role="assistant",
    )

    assert isinstance(result_and_id, tuple) and len(result_and_id) == 2
    assert "hello" in h.delivered()


async def test_send_tolerates_slack_only_kwargs_without_raising(h):
    """Callers still pass Block Kit kwargs to every adapter (mrkdwn/blocks, and
    the Web-only ``actions``); a non-Slack adapter must ignore them, not
    raise, so the live status message is never lost."""
    await deliver(h.adapter,
        h.thread_key, "with blocks", conversation_id=h.conversation_id,
        mrkdwn=True, blocks=[{"type": "section"}], actions=[],
    )

    assert "with blocks" in h.delivered()


async def test_update_edits_a_previously_sent_message(h):
    result, message_id = await deliver(h.adapter,
        h.thread_key, "first", conversation_id=h.conversation_id,
    )
    ref = h.ref_for(result, message_id)

    await deliver_update(h.adapter,
        h.thread_key, ref, "second", conversation_id=h.conversation_id, message_id=message_id,
    )

    assert "second" in h.delivered()


async def test_format_markdown_returns_chunks_within_the_platform_limit(h):
    chunks = h.adapter.format_markdown("word " * 2000)

    assert isinstance(chunks, list) and chunks and all(isinstance(c, str) for c in chunks)
    if h.max_chars is not None:
        assert all(len(c) <= h.max_chars for c in chunks)


async def test_format_markdown_of_short_text_is_a_single_chunk(h):
    assert len(h.adapter.format_markdown("hi")) == 1


async def test_permalink_never_raises_and_is_a_string_or_none(h):
    link = await h.adapter.permalink(h.thread_key)

    assert link is None or isinstance(link, str)


async def test_upload_file_either_works_or_raises_not_implemented(h):
    """Optional ops: supported -> works; unsupported -> NotImplementedError."""
    if "upload" in h.supports:
        assert await h.adapter.upload_file(h.thread_key, b"x", "a.png") is not None
    else:
        with pytest.raises(NotImplementedError):
            await h.adapter.upload_file(h.thread_key, b"x", "a.png")


@pytest.mark.parametrize("op", ["add_reaction", "remove_reaction", "delete_message"])
async def test_optional_message_ops_either_work_or_raise_not_implemented(h, op):
    args = (h.thread_key, "eyes", "1.000") if op != "delete_message" else (h.thread_key, "1.000")
    needed = "delete" if op == "delete_message" else "reactions"
    if needed in h.supports:
        await getattr(h.adapter, op)(*args)
    else:
        with pytest.raises(NotImplementedError):
            await getattr(h.adapter, op)(*args)
