"""Per-adapter *harness* used by the shared contract suite.

The contract tests must not know whether they are talking to Slack, the Web
UI or the fake platform. Each ``Harness`` hides the platform-specific parts:
how to build a thread key, how to observe what was delivered, how to turn a
``send()`` result into the reference ``update()`` needs, and which optional
operations the platform supports.

Every platform-specific shim in here (notably ``ref_for``) is a place where
the *current* protocol leaks platform detail; the target-interface tests
(``test_adapter_target_interface.py``) describe the shape that removes them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import message_store
from adapters import SlackAdapter, WebAdapter

from fake_adapter import FAKE_PLATFORM, MAX_CHARS as FAKE_MAX_CHARS, FakeAdapter


class RecordingSlackClient:
    """Minimal async Slack client double (only what SlackAdapter touches)."""

    def __init__(self) -> None:
        self.posted: list[dict] = []
        self.updated: list[dict] = []
        self.uploaded: list[dict] = []
        self.reactions: list[dict] = []
        self.deleted: list[dict] = []

    async def chat_postMessage(self, **kw):
        self.posted.append(kw)
        return {"ts": f"{len(self.posted)}.000", "channel": kw.get("channel")}

    async def chat_update(self, **kw):
        self.updated.append(kw)
        return {"ts": kw.get("ts"), "channel": kw.get("channel")}

    async def files_upload_v2(self, **kw):
        self.uploaded.append(kw)
        return {"ok": True}

    async def chat_getPermalink(self, **kw):
        return {"permalink": f"https://slack.example/{kw.get('channel')}/{kw.get('message_ts')}"}

    async def reactions_add(self, **kw):
        self.reactions.append(kw)
        return {"ok": True}

    async def reactions_remove(self, **kw):
        return {"ok": True}

    async def chat_delete(self, **kw):
        self.deleted.append(kw)
        return {"ok": True}

    async def files_info(self, **kw):
        return {"file": {"shares": {}}}


@dataclass
class Harness:
    name: str
    platform: str
    adapter: Any
    thread_key: dict
    conversation_id: str
    max_chars: int | None
    supports: frozenset
    delivered: Callable[[], list[str]]
    ref_for: Callable[[Any, str | None], Any]
    extra: dict = field(default_factory=dict)


def _supports(adapter) -> frozenset:
    """Support flags come from the adapter's own ``capabilities``."""
    caps = adapter.capabilities
    return frozenset(
        name for name, on in (
            ("upload", caps.supports_uploads), ("reactions", caps.supports_reactions),
            ("delete", caps.supports_delete),
        ) if on
    )


def _conv() -> str:
    return message_store.get_or_create_conversation(None)


def make_slack() -> Harness:
    client = RecordingSlackClient()
    adapter = SlackAdapter(client)
    return Harness(
        name="slack", platform=message_store.SLACK_PLATFORM, adapter=adapter,
        thread_key={"channel": "C1", "thread_ts": "100.1"}, conversation_id=_conv(),
        max_chars=3900, supports=_supports(adapter),
        delivered=lambda: [p["text"] for p in client.posted] + [u["text"] for u in client.updated],
        ref_for=lambda result, message_id: result["ts"],
        extra={"client": client},
    )


def make_web() -> Harness:
    adapter = WebAdapter()
    conv = _conv()
    return Harness(
        name="web", platform=message_store.WEB_PLATFORM, adapter=adapter,
        thread_key={"conversation_id": conv}, conversation_id=conv,
        max_chars=None, supports=_supports(adapter),
        delivered=lambda: [m["content"] for m in message_store.get_messages(conv)],
        ref_for=lambda result, message_id: message_id,
    )


def make_fake() -> Harness:
    adapter = FakeAdapter()
    return Harness(
        name="fake", platform=FAKE_PLATFORM, adapter=adapter,
        thread_key=FakeAdapter.thread_key(), conversation_id=_conv(),
        max_chars=FAKE_MAX_CHARS, supports=_supports(adapter),
        delivered=lambda: [s["text"] for s in adapter.sent] + [e["text"] for e in adapter.edits],
        ref_for=lambda result, message_id: result["message_id"],
    )


def make_telegram() -> Harness:
    """Real TelegramAdapter against the strict FakeTelegram transport (forum topic thread key)."""
    from fake_telegram import FakeTelegram
    from telegram_adapter import TelegramAdapter
    from telegram_api import ChatRateLimiter, TelegramApi

    async def _no_sleep(_s):
        return None

    chat = -1001234567890
    server = FakeTelegram(forum_chat_id=chat)
    server.topics[(chat, 7)] = {"name": "t", "closed": False}
    api = TelegramApi(server, limiter=ChatRateLimiter(min_gap=0, sleep=_no_sleep), sleep=_no_sleep)
    adapter = TelegramAdapter(api, forum_chat_id=chat, allowed_user_ids=["1"])
    return Harness(
        name="telegram", platform=message_store.TELEGRAM_PLATFORM, adapter=adapter,
        thread_key={"chat_id": chat, "topic_id": 7, "slot": None}, conversation_id=_conv(),
        max_chars=4000, supports=_supports(adapter),
        delivered=lambda: [m["text"] for m in server.sent()],
        ref_for=lambda result, message_id: result["message_id"],
        extra={"server": server},
    )


HARNESS_FACTORIES = {"slack": make_slack, "web": make_web, "fake": make_fake, "telegram": make_telegram}
