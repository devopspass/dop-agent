"""In-memory ``FakeAdapter`` — a deliberately *unlike-Slack* chat platform.

Why it exists: a "unified interface" claim tested only against platforms the
interface was grown around proves little. This adapter differs from Slack on
purpose in every way a future platform is likely to differ:

- message ids are **ints scoped per chat** (not Slack's ``"ts"`` strings);
- a thread key is ``{"chat_id": int, "topic_id": int | None}`` (no
  ``channel``/``thread_ts``);
- tiny per-message length limit (``MAX_CHARS``) and a stricter edit rate;
- no reactions, no message deletion (raises ``NotImplementedError``);
- ignores Slack-only kwargs (``blocks``/``mrkdwn``) *itself* instead of
  relying on callers to drop them.

If the core (handlers/watchers/broadcaster) and the shared contract suite
pass with Slack + Web + this fake, the seam is genuinely reusable; a real
Telegram adapter then only has to pass the same suite.

It records to ``message_store`` exactly like the real adapters do
(each adapter records its own sends), so broadcaster/mirroring tests behave
the same way.
"""

from __future__ import annotations

from typing import Any

from adapters import Capabilities

import message_store

FAKE_PLATFORM = "fake"
MAX_CHARS = 40


class FakeAdapter:
    platform = FAKE_PLATFORM

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []
        self.files: list[dict[str, Any]] = []
        self._next_id = 1000

    # -- helpers used by tests only -------------------------------------
    @staticmethod
    def thread_key(chat_id: int = 42, topic_id: int | None = None) -> dict[str, Any]:
        return {"chat_id": chat_id, "topic_id": topic_id}

    @staticmethod
    def external_key_for(thread_key: dict[str, Any]) -> str:
        topic = thread_key.get("topic_id")
        return f"{thread_key['chat_id']}:{topic}" if topic else str(thread_key["chat_id"])

    capabilities = Capabilities(
        max_chars=MAX_CHARS, supports_edit=True, supports_reactions=False,
        supports_uploads=True, supports_delete=False, supports_buttons=False,
        edit_min_interval=1.0,
    )

    # -- Adapter protocol -------------------------------------------------
    def thread_key_from_binding(self, external_key):
        chat, _sep, topic = external_key.partition(":")
        if not chat.lstrip("-").isdigit():
            return None
        return {"chat_id": int(chat), "topic_id": int(topic) if topic else None}

    def external_key(self, thread_key):
        return self.external_key_for(thread_key)

    @staticmethod
    def message_ref(thread_key, native, message_id=None):
        from message_ref import MessageRef
        raw = native.get("message_id") if hasattr(native, "get") else native
        return MessageRef(FAKE_PLATFORM, str(thread_key["chat_id"]), str(raw))

    @staticmethod
    def external_ref(ref):
        return ref.key()

    async def send(self, thread_key, text, *, buttons=None, footnote=None, **kwargs):
        self._next_id += 1
        msg_id = self._next_id
        self.sent.append({"thread_key": thread_key, "text": text, "id": msg_id})
        return {"message_id": msg_id}

    async def update(self, thread_key, message_ref, text, *, buttons=None, footnote=None, **kwargs):
        ref = getattr(message_ref, "id", message_ref)
        self.edits.append({"thread_key": thread_key, "ref": ref, "text": text})
        return {"message_id": ref}

    async def upload_file(self, thread_key, data=None, filename=None, *,
                          file_uploads=None, wait_posted=False, **kwargs):
        self.files.append({"thread_key": thread_key, "filename": filename, "data": data,
                           "file_uploads": file_uploads})
        return {"ok": True}

    async def add_reaction(self, thread_key, name, ts):
        raise NotImplementedError("FakeAdapter has no reactions")

    async def remove_reaction(self, thread_key, name, ts):
        raise NotImplementedError("FakeAdapter has no reactions")

    async def delete_message(self, thread_key, ts):
        raise NotImplementedError("FakeAdapter cannot delete messages")

    def format_markdown(self, text: str) -> list[str]:
        return [text[i:i + MAX_CHARS] for i in range(0, len(text), MAX_CHARS)] or [""]

    async def permalink(self, thread_key):
        return None
