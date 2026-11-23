"""``TelegramAdapter``: the Telegram implementation of ``adapters.Adapter``.

Pure transport like Slack/Web: it sends, edits, uploads, reacts and deletes via
``TelegramApi``; recording is ``adapters.deliver``'s job. Design points (see
README "Telegram" for the user-facing view):

- **thread_key** = ``{"chat_id": int, "topic_id": int | None, "slot": int | None}``.
  Binding key ``"<chat>:<topic>"`` for a forum topic (negative chat id),
  ``"<chat>"`` / ``"<chat>:<n>"`` for DM sessions (positive chat id; ``n`` is the
  session slot bumped by ``/new``).
- **Text pipeline**: ``format_markdown`` returns plain-markdown chunks (fence-aware,
  <= 4000 UTF-16 units); ``send``/``update`` convert to HTML at send time and fall
  back to plain text if Telegram rejects the markup. The core's Slack-isms
  (``:shortcodes:``, ``_italics_``) are mapped by ``telegram_format``.
- **Buttons**: neutral ``Button`` -> inline keyboard. Stop is stateless
  (``st:<conversation_id>``); other buttons and ``actions``/``ask`` fences use
  sqlite tokens (``ab:<token>``). ``buttons=None`` on ``update`` re-sends the
  previous keyboard (Telegram strips it otherwise); ``buttons=[]`` strips it.
- **Failure handling**: a deleted topic is unbound and recreated (stale keys are
  redirected); a closed topic is reopened for the send and re-closed; status edits
  (carrying the Stop button) never sleep on 429 - they are dropped so the
  terminal edit is not blocked; other edits fall back to a new message.
- **Topics**: ``ensure_topic`` binds BEFORE the first send (the mirroring code only
  binds when the key changes, which never happens for Telegram). Title/archive
  changes are synced through ``message_store.register_change_listener`` (cheap,
  thread-safe, debounced).
"""

from __future__ import annotations

import asyncio
import logging
import struct
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import message_store
import telegram_store
from adapters import Button, Capabilities
from message_ref import MessageRef
from telegram_api import TelegramApi, TelegramError
from telegram_format import (
    HARD_LIMIT, TEXT_BUDGET, extract_fences, md_to_telegram_html, plain_text, replace_shortcodes,
    split_caption, split_markdown, truncate_visible, validate_html, visible_len,
)

log = logging.getLogger("tg.adapter")

PLATFORM = message_store.TELEGRAM_PLATFORM
PHOTO_MAX_BYTES = 10 * 1024 * 1024
UPLOAD_MAX_BYTES = 50 * 1024 * 1024
STOP_ACTION = "stop_working_turn"

ALLOWED_REACTIONS = frozenset(
    "👍 👎 ❤ 🔥 🥰 👏 😁 🤔 🤯 😱 🤬 😢 🎉 🤩 🤮 💩 🙏 👌 🕊 🤡 🥱 🥴 😍 🐳 ❤‍🔥 🌚 🌭 💯 🤣 ⚡ 🍌 🏆 💔 🤨 😐 🍓 "
    "🍾 💋 🖕 😈 😴 😭 🤓 👻 👨‍💻 👀 🎃 🙈 😇 😨 🤝 ✍ 🤗 🫡 🎅 🎄 ☃ 💅 🤪 🗿 🆒 💘 🙉 🦄 😘 💊 🙊 😎 👾 "
    "🤷‍♂ 🤷 🤷‍♀ 😡".split()
)
# Slack names the core uses -> allowed Telegram reactions (anything else is ignored).
REACTION_MAP = {
    "eyes": "👀", "hourglass_flowing_sand": "👀", "hourglass": "👀", "thumbsup": "👍", "+1": "👍",
    "white_check_mark": "👍", "heavy_check_mark": "👍", "thumbsdown": "👎", "-1": "👎", "x": "👎",
    "fire": "🔥", "tada": "🎉", "zap": "⚡", "heart": "❤", "thinking_face": "🤔", "100": "💯",
    "ok_hand": "👌", "pray": "🙏", "clap": "👏",
}


def parse_ref(value: Any, default_chat: int | None = None) -> tuple[int, int] | None:
    """Tolerant ``ts`` parser: ``MessageRef``, ``"chat:msgid"`` (incl. negative chat
    ids), a bare message id (needs ``default_chat``) or int. Garbage / empty
    (watch fires pass ``ts=""``; the contract suite passes ``"1.000"``) -> None."""
    if isinstance(value, MessageRef):
        value = f"{value.conversation_key}:{value.id}"
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return (default_chat, value) if default_chat is not None else None
    text = str(value).strip()
    if ":" in text:
        chat, _, mid = text.rpartition(":")
        if chat.lstrip("-").isdigit() and mid.isdigit():
            return int(chat), int(mid)
        return None
    if text.isdigit() and default_chat is not None:
        return default_chat, int(text)
    return None


def png_dimensions(data: bytes) -> tuple[int, int] | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    return None


def photo_ok(data: bytes, filename: str) -> bool:
    """sendPhoto limits: 10 MB, width+height <= 10000, aspect ratio <= 20."""
    if not filename.lower().endswith((".png", ".jpg", ".jpeg")) or len(data) > PHOTO_MAX_BYTES:
        return False
    dims = png_dimensions(data)
    if dims:
        w, h = dims
        return bool(w and h) and w + h <= 10000 and max(w, h) / min(w, h) <= 20
    return True


def clean_topic_name(title: str | None) -> str:
    name = replace_shortcodes((title or "").replace("\n", " ")).strip()
    return (name or "New session")[:128]


@dataclass
class _Prepared:
    html: str
    plain: str
    markup: dict | None


class TelegramAdapter:
    platform = PLATFORM

    def __init__(self, api: TelegramApi, *, forum_chat_id: int | None = None,
                 allowed_user_ids: list[str] | None = None, tstore: Any = telegram_store,
                 sync_delay: float = 2.0, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.api = api
        self.forum_chat_id = forum_chat_id
        self.allowed_user_ids = list(allowed_user_ids or [])
        self.tstore = tstore
        self.bot_username: str | None = None
        self.bot_id: int | None = None
        self._sync_delay = sync_delay
        self._sleep = sleep
        self._loop: asyncio.AbstractEventLoop | None = None
        self._locks: dict[str, asyncio.Lock] = {}
        self._redirects: dict[str, dict[str, Any]] = {}
        self._markups: "OrderedDict[tuple[int, int], dict | None]" = OrderedDict()
        self._topic_convs: set[str] = set()
        self._sync_pending: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        self._listener_registered = False

    # -- capabilities (edit interval depends on group vs DM) -----------------
    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            max_chars=TEXT_BUDGET, max_rich_chars=TEXT_BUDGET, supports_edit=True, supports_reactions=True,
            supports_uploads=True, supports_delete=True, supports_buttons=True,
            edit_min_interval=3.0 if self.forum_chat_id else 1.5)

    # -- keys ----------------------------------------------------------------
    def thread_key_from_binding(self, external_key: str) -> dict[str, Any] | None:
        chat, _sep, rest = (external_key or "").partition(":")
        if not chat.lstrip("-").isdigit() or chat in ("", "-"):
            return None
        chat_id = int(chat)
        if rest and not rest.isdigit():
            return None
        if chat_id < 0:
            return {"chat_id": chat_id, "topic_id": int(rest) if rest else None, "slot": None}
        return {"chat_id": chat_id, "topic_id": None, "slot": int(rest) if rest else None}

    def external_key(self, thread_key: dict[str, Any]) -> str:
        chat = thread_key.get("chat_id")
        if chat is None:
            return ""
        if thread_key.get("topic_id"):
            return f"{chat}:{thread_key['topic_id']}"
        if thread_key.get("slot"):
            return f"{chat}:{thread_key['slot']}"
        return str(chat)

    @staticmethod
    def message_ref(thread_key: dict[str, Any], native: Any, message_id: str | None = None) -> MessageRef:
        if isinstance(native, MessageRef):
            return native
        raw = native.get("message_id") if hasattr(native, "get") else native
        chat = thread_key.get("chat_id", "")
        if isinstance(raw, str) and ":" in raw:
            parsed = parse_ref(raw)
            if parsed:
                chat, raw = parsed
        return MessageRef(PLATFORM, str(chat), str(raw or ""))

    @staticmethod
    def external_ref(ref: MessageRef) -> str | None:
        return f"{ref.conversation_key}:{ref.id}" if ref.conversation_key and ref.id else None

    def new_conversation_thread_key(self) -> None:
        """Telegram cannot offer a key synchronously (a topic must be created);
        use ``ensure_new_conversation_target`` instead."""
        return None

    def _redirect(self, thread_key: dict[str, Any]) -> dict[str, Any]:
        return self._redirects.get(self.external_key(thread_key), thread_key)

    # -- rendering -------------------------------------------------------------
    def format_markdown(self, text: str) -> list[str]:
        return split_markdown(text, TEXT_BUDGET)

    def _keyboard(self, thread_key: dict[str, Any], buttons: list[Button] | None,
                  fence_rows: list) -> dict | None:
        rows: list[list[dict]] = []
        for b in buttons or []:
            label = replace_shortcodes(b.label)[:60]
            if b.action_id == STOP_ACTION and b.value:
                data = f"st:{b.value}"
            else:
                token = self.tstore.put_callback(action_id=b.action_id, text=b.value,
                                                external_key=self.external_key(thread_key))
                data = f"bt:{token}"
            rows.append([{"text": label, "callback_data": data[:64]}])
        for row in fence_rows[:20]:
            line = []
            for choice in row:
                token = self.tstore.put_callback(text=choice.text, external_key=self.external_key(thread_key))
                line.append({"text": ("★ " if choice.primary else "") + choice.label[:55],
                             "callback_data": f"ab:{token}"})
            rows.append(line)
        return {"inline_keyboard": rows} if rows else None

    def _prepare(self, thread_key: dict[str, Any], text: str, buttons: list[Button] | None,
                 footnote: str | None) -> list[_Prepared]:
        full = f"{text}\n\n{footnote}" if footnote else text
        fence_rows: list = []
        if not buttons:  # status messages (Stop button) never carry choice fences
            res = extract_fences(full)
            full, fence_rows = res.text, res.rows
        markup = self._keyboard(thread_key, buttons, fence_rows)
        limit = TEXT_BUDGET
        while True:
            chunks = split_markdown(full or "_(no output)_", limit)
            htmls = [md_to_telegram_html(c) for c in chunks]
            if all(visible_len(h) <= HARD_LIMIT for h in htmls) or limit <= 200:
                break
            limit //= 2
        out = [_Prepared(h, plain_text(c), None) for h, c in zip(htmls, chunks)]
        out[-1].markup = markup
        return out

    # -- recovery wrapper ----------------------------------------------------
    async def _guard(self, thread_key: dict[str, Any], op: Callable[[dict[str, Any]], Awaitable[Any]]) -> Any:
        tk = self._redirect(thread_key)
        reopened = False
        rebound = False
        while True:
            try:
                result = await op(tk)
            except TelegramError as exc:
                topic = tk.get("topic_id")
                if topic and exc.has("message thread not found") and not rebound:
                    rebound = True
                    tk = await self._rebind_topic(tk)
                    continue
                if topic and exc.has("TOPIC_CLOSED", "topic is closed") and not reopened:
                    reopened = True
                    await self.api.reopen_forum_topic(tk["chat_id"], topic)
                    self.tstore.update_topic(tk["chat_id"], topic, closed=False)
                    continue
                raise
            if reopened:
                await self._reclose_if_archived(tk)
            return result

    async def _reclose_if_archived(self, tk: dict[str, Any]) -> None:
        conv = message_store.resolve_conversation_binding(PLATFORM, self.external_key(tk))
        convo = message_store.get_conversation(conv) if conv else None
        if convo and convo.get("archived"):
            try:
                await self.api.close_forum_topic(tk["chat_id"], tk["topic_id"])
                self.tstore.update_topic(tk["chat_id"], tk["topic_id"], closed=True)
            except TelegramError as exc:
                log.warning("Could not re-close topic after send: %s", exc)

    async def _rebind_topic(self, tk: dict[str, Any]) -> dict[str, Any]:
        old_key = self.external_key(tk)
        conv = message_store.resolve_conversation_binding(PLATFORM, old_key)
        if not conv:
            raise TelegramError(400, "message thread not found (no binding to recover)")
        log.warning("Topic %s no longer exists - unbinding and recreating for %s", old_key, conv)
        message_store.unbind_conversation(conv, PLATFORM, old_key)
        self.tstore.delete_topic(tk["chat_id"], tk["topic_id"])
        new_tk = await self.ensure_topic(conv)
        self._redirects[old_key] = new_tk
        return new_tk

    # -- send / update ---------------------------------------------------------
    async def _post_one(self, tk: dict[str, Any], prep: _Prepared, *, status: bool) -> dict:
        on_429 = "raise" if status else "sleep"
        kwargs = dict(message_thread_id=tk.get("topic_id"), reply_markup=prep.markup, on_429=on_429)
        try:
            msg = await self.api.send_message(tk["chat_id"], prep.html, parse_mode="HTML", **kwargs)
        except TelegramError as exc:
            if not exc.has("can't parse entities", "can't find end", "unsupported start tag"):
                raise
            msg = await self.api.send_message(tk["chat_id"], truncate_visible(prep.plain), **kwargs)
        self._remember(tk["chat_id"], msg, prep.markup)
        if tk["chat_id"] > 0 and isinstance(msg, dict) and msg.get("message_id"):
            self.tstore.map_message(tk["chat_id"], msg["message_id"], self.external_key(tk))
        return msg

    def _remember(self, chat_id: int, msg: Any, markup: dict | None) -> None:
        if isinstance(msg, dict) and msg.get("message_id"):
            self._markups[(chat_id, msg["message_id"])] = markup
            while len(self._markups) > 500:
                self._markups.popitem(last=False)

    @staticmethod
    def _is_status(buttons: list[Button] | None) -> bool:
        return any(b.action_id == STOP_ACTION for b in buttons or [])

    async def send(self, thread_key, text, *, buttons=None, footnote=None, **kwargs):
        """Slack-only kwargs (``blocks``/``mrkdwn``/``thread_ts``/...) are ignored."""
        preps = self._prepare(self._redirect(thread_key), text, buttons, footnote)
        status = self._is_status(buttons)
        first = None
        for prep in preps:
            msg = await self._guard(thread_key, lambda tk, p=prep: self._post_one(tk, p, status=status))
            first = first or msg
        return first

    async def update(self, thread_key, message_ref, text, *, buttons=None, footnote=None, **kwargs):
        tk = self._redirect(thread_key)
        parsed = parse_ref(message_ref, tk.get("chat_id"))
        if parsed is None:
            raise ValueError(f"unusable Telegram message ref: {message_ref!r}")
        chat_id, message_id = parsed
        prep = self._prepare(tk, text, buttons, footnote)[0]
        status = self._is_status(buttons)
        if buttons is None and prep.markup is None:
            prep.markup = self._markups.get((chat_id, message_id))  # keep existing keyboard
        elif buttons is not None and prep.markup is None:
            prep.markup = {"inline_keyboard": []}  # explicit strip
        if visible_len(prep.html) > HARD_LIMIT:
            prep.html = md_to_telegram_html(truncate_visible(prep.plain, HARD_LIMIT - 1))
        on_429 = "raise" if status else "sleep"

        async def op(_tk: dict[str, Any]) -> Any:
            try:
                return await self.api.edit_message_text(chat_id, message_id, prep.html, parse_mode="HTML",
                                                        reply_markup=prep.markup, on_429=on_429)
            except TelegramError as exc:
                if exc.has("can't parse entities", "can't find end"):
                    return await self.api.edit_message_text(
                        chat_id, message_id, truncate_visible(prep.plain), reply_markup=prep.markup, on_429=on_429)
                raise

        try:
            result = await self._guard(tk, op)
        except TelegramError as exc:
            if exc.code == 429 and not status:
                log.warning("Edit rate-limited beyond cap; posting as a new message instead")
                return await self.send(tk, text, buttons=buttons, footnote=footnote)
            raise
        self._markups[(chat_id, message_id)] = prep.markup if prep.markup and prep.markup["inline_keyboard"] else None
        return result if isinstance(result, dict) else {"message_id": message_id, "chat": {"id": chat_id}}

    # -- files -----------------------------------------------------------------
    async def upload_file(self, thread_key, data=None, filename=None, *, file_uploads=None,
                          wait_posted=False, **kwargs):
        """``wait_posted`` is a no-op: Telegram delivery is ordered."""
        caption = kwargs.get("title") or kwargs.get("initial_comment")
        caption = split_caption(caption) if caption else None
        items: list[tuple[str, bytes, str | None]] = []
        if file_uploads is not None:
            for fu in file_uploads:
                items.append((fu.get("filename") or "file", fu["file"], fu.get("title")))
        elif data is not None:
            items.append((filename or "file", data, caption))
        for name, raw, _cap in items:
            if len(raw) > UPLOAD_MAX_BYTES:
                raise ValueError(f"{name} is {len(raw)} bytes; Telegram bots can upload at most 50 MB")
        if not items:
            return None
        if len(items) == 1:
            name, raw, cap = items[0]
            return await self._guard(thread_key, lambda tk: self._send_file(tk, name, raw, cap))
        results: list[Any] = []
        for i in range(0, len(items), 10):
            batch = items[i:i + 10]
            if len(batch) == 1:
                name, raw, cap = batch[0]
                results.append(await self._guard(thread_key, lambda tk, a=(name, raw, cap): self._send_file(tk, *a)))
                continue
            all_photos = all(photo_ok(raw, name) for name, raw, _ in batch)  # albums can't mix photo + document
            media = [("photo" if all_photos else "document", name, raw,
                      split_caption(cap) if cap else None) for name, raw, cap in batch]
            try:
                results.append(await self._guard(
                    thread_key, lambda tk, m=media: self.api.send_media_group(
                        tk["chat_id"], m, message_thread_id=tk.get("topic_id"))))
            except TelegramError as exc:
                if not all_photos or exc.code != 400:
                    raise
                media = [("document", n, r, c) for _k, n, r, c in media]
                results.append(await self._guard(
                    thread_key, lambda tk, m=media: self.api.send_media_group(
                        tk["chat_id"], m, message_thread_id=tk.get("topic_id"))))
        return results

    async def _send_file(self, tk: dict[str, Any], name: str, raw: bytes, caption: str | None) -> Any:
        if photo_ok(raw, name):
            try:
                return await self.api.send_photo(tk["chat_id"], raw, name, caption=caption,
                                                 message_thread_id=tk.get("topic_id"))
            except TelegramError as exc:
                if exc.code != 400 or exc.has("thread", "TOPIC_CLOSED"):
                    raise
                log.info("sendPhoto rejected (%s); falling back to sendDocument", exc.description)
        return await self.api.send_document(tk["chat_id"], raw, name, caption=caption,
                                            message_thread_id=tk.get("topic_id"))

    # -- reactions / delete ----------------------------------------------------
    async def add_reaction(self, thread_key, name, ts):
        parsed = parse_ref(ts, thread_key.get("chat_id"))
        emoji = REACTION_MAP.get(name)
        if parsed is None or emoji not in ALLOWED_REACTIONS:
            return None
        return await self.api.set_message_reaction(parsed[0], parsed[1], [emoji])

    async def remove_reaction(self, thread_key, name, ts):
        """Bots hold one reaction and ``setMessageReaction`` replaces all of them,
        so removing means setting an empty list."""
        parsed = parse_ref(ts, thread_key.get("chat_id"))
        if parsed is None or REACTION_MAP.get(name) not in ALLOWED_REACTIONS:
            return None
        return await self.api.set_message_reaction(parsed[0], parsed[1], [])

    async def delete_message(self, thread_key, ts):
        parsed = parse_ref(ts, thread_key.get("chat_id"))
        if parsed is None:
            return None
        return await self.api.delete_message(*parsed)

    async def permalink(self, thread_key):
        chat, topic = thread_key.get("chat_id"), thread_key.get("topic_id")
        if chat is None or not topic or not str(chat).startswith("-100"):
            return None
        return f"https://t.me/c/{str(chat)[4:]}/{topic}"

    # -- topics ----------------------------------------------------------------
    def _lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    async def create_topic(self, title: str | None) -> dict[str, Any]:
        """createForumTopic in the forum chat -> thread_key (no DB writes)."""
        if not self.forum_chat_id:
            raise RuntimeError("TELEGRAM_FORUM_CHAT_ID is not configured")
        topic = await self.api.create_forum_topic(self.forum_chat_id, clean_topic_name(title))
        return {"chat_id": self.forum_chat_id, "topic_id": int(topic["message_thread_id"]), "slot": None}

    async def ensure_topic(self, conversation_id: str, title: str | None = None) -> dict[str, Any]:
        """The conversation's Telegram thread_key, creating a forum topic (and its
        binding, BEFORE any send) when it has none."""
        async with self._lock(conversation_id):
            for binding in message_store.list_conversation_bindings(conversation_id, PLATFORM):
                tk = self.thread_key_from_binding(binding["external_key"])
                if tk:
                    return tk
            name = clean_topic_name(title or message_store.get_display_title(conversation_id))
            tk = await self.create_topic(name)
            key = self.external_key(tk)
            existing = message_store.resolve_conversation_binding(PLATFORM, key)
            if existing and existing != conversation_id:
                raise RuntimeError(f"Telegram key {key} already bound to {existing}")
            self._topic_convs.add(conversation_id)
            message_store.bind_conversation(conversation_id, PLATFORM, key)
            self.tstore.upsert_topic(tk["chat_id"], tk["topic_id"], conversation_id, title_synced=name)
            return tk

    def adopt_topic(self, conversation_id: str, tk: dict[str, Any], name: str | None = None) -> None:
        """Record a topic created by hand (first message in it) so later renames
        sync from the session title without clobbering the user's name at once."""
        if tk.get("topic_id") and not self.tstore.get_topic(tk["chat_id"], tk["topic_id"]):
            self.tstore.upsert_topic(tk["chat_id"], tk["topic_id"], conversation_id,
                                    title_synced=name or clean_topic_name(message_store.get_display_title(conversation_id)))
        self._topic_convs.add(conversation_id)

    async def ensure_new_conversation_target(self, conversation_id: str, title: str | None = None):
        """Async counterpart of ``new_conversation_thread_key`` for brand-new Web UI
        sessions: forum topic when configured, else a fresh DM slot."""
        if self.forum_chat_id:
            return self, await self.ensure_topic(conversation_id, title)
        if not self.allowed_user_ids:
            return None
        chat = int(self.allowed_user_ids[0])
        slot = int(self.tstore.kv_get(f"dm_slot:{chat}", "0") or 0) + 1
        self.tstore.kv_set(f"dm_slot:{chat}", str(slot))
        tk = {"chat_id": chat, "topic_id": None, "slot": slot}
        message_store.bind_conversation(conversation_id, PLATFORM, self.external_key(tk))
        return self, tk

    # -- title / archive sync ------------------------------------------------------
    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._topic_convs = {t["conversation_id"] for t in self.tstore.list_topics()}
        if not self._listener_registered:
            message_store.register_change_listener(self._on_change)
            self._listener_registered = True

    def _on_change(self, conversation_id: str) -> None:
        """Sync, any thread, called on every message write: do nothing unless the
        conversation owns a topic, then hop to the loop."""
        loop = self._loop
        if loop is None or conversation_id not in self._topic_convs or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._schedule_sync, conversation_id)
        except RuntimeError:
            pass

    def _schedule_sync(self, conversation_id: str) -> None:
        if conversation_id in self._sync_pending:
            return
        self._sync_pending.add(conversation_id)
        task = asyncio.ensure_future(self._debounced_sync(conversation_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _debounced_sync(self, conversation_id: str) -> None:
        try:
            if self._sync_delay:
                await self._sleep(self._sync_delay)
        finally:
            self._sync_pending.discard(conversation_id)
        try:
            await self.sync_topic(conversation_id)
        except Exception as exc:  # noqa: BLE001 - best effort, never breaks a turn
            log.warning("Topic sync failed for %s: %s", conversation_id, exc)

    async def sync_topic(self, conversation_id: str) -> None:
        convo = message_store.get_conversation(conversation_id)
        if convo is None:
            return
        archived = bool(convo.get("archived"))
        title = clean_topic_name(message_store.get_display_title(conversation_id))
        for row in self.tstore.topics_for_conversation(conversation_id):
            chat, thread = row["chat_id"], row["thread_id"]
            closed = bool(row["closed"])
            try:
                if closed and not archived:
                    await self.api.reopen_forum_topic(chat, thread)
                    self.tstore.update_topic(chat, thread, closed=False)
                    closed = False
                if title != row["title_synced"]:
                    await self.api.edit_forum_topic(chat, thread, title)
                    self.tstore.update_topic(chat, thread, title_synced=title)
                if archived and not closed:
                    await self.api.close_forum_topic(chat, thread)
                    self.tstore.update_topic(chat, thread, closed=True)
            except TelegramError as exc:
                if exc.has("message thread not found", "TOPIC_ID_INVALID"):
                    self.tstore.delete_topic(chat, thread)
                    message_store.unbind_conversation(conversation_id, PLATFORM, f"{chat}:{thread}")
                else:
                    log.warning("Topic sync for %s failed: %s", conversation_id, exc)

    async def drain(self) -> None:
        """Wait for scheduled syncs (tests / shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
