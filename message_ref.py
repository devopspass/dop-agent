"""Platform-neutral identity of one posted chat message.

Dependency-free so both ``adapters`` and ``persistence`` can use it (the
side files that survive a restart - ``active_status.json``,
``pending_transcriptions.json`` - are keyed by ``MessageRef.key()``).
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass


@dataclass(frozen=True)
class MessageRef:
    """``platform`` + the chat/conversation the message lives in + its id
    within that chat (Slack: channel + ts; Telegram: chat_id + message_id)."""

    platform: str
    conversation_key: str
    id: str

    def key(self) -> str:
        return f"{self.platform}:{self.conversation_key}:{self.id}"

    @classmethod
    def parse(cls, key: str) -> "MessageRef":
        platform, _, rest = key.partition(":")
        conversation_key, _, msg_id = rest.rpartition(":")
        return cls(platform=platform, conversation_key=conversation_key, id=msg_id)


def first_sighting(seen: "OrderedDict", key: tuple, max_size: int) -> bool:
    """True the first time ``key`` is seen, False on a repeat; ``seen`` is a
    bounded LRU-ish OrderedDict. Shared by every inbound dedup (Slack event
    redelivery, any platform's ``IncomingMessage.ref``)."""
    if key in seen:
        return False
    seen[key] = None
    if len(seen) > max_size:
        seen.popitem(last=False)
    return True
