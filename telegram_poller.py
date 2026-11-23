"""Telegram long-poll inbound loop (``getUpdates`` over outbound HTTPS, no webhook).

Safety design (autopilot agent behind this - the allow-list is the only gate):

- **At-most-once**: the offset is persisted *before* an update is dispatched, so a
  crash never replays an old instruction into the agent. Dispatch is fire-and-forget
  (a turn can run for minutes; awaiting it would stall polling and break the
  "new message interrupts the running turn" semantics).
- **Stale drop**: updates older than ``TELEGRAM_MAX_UPDATE_AGE`` (default 600 s) are
  ignored (Telegram redelivers up to 24 h of backlog after downtime).
- **Silent for strangers**: updates from non-allowed ``from.id`` or foreign chats are
  dropped without any API call (callbacks still get an empty ``answerCallbackQuery``
  so no client spinner hangs). ``edited_message`` is ignored (its ref would collide
  with the original).
- **Resilience**: 409 (restart overlap or leftover webhook) -> backoff 5..60 s and
  ``deleteWebhook`` again, never a silent exit; network errors back off 1..60 s;
  401/404 (bad token) stops the poller with a clear log line. The task never kills
  Slack or the Web UI.
- **Routing**: DM = one active session per chat (``/new`` starts the next; replying to
  an older bot message continues that one via ``telegram_msgmap``); forum chat = one
  topic per session; a message in *General* creates a topic, copies the text into
  it, links back, and is processed there.

Commands handled here (never forwarded to the agent): ``/start /help /new /stop``,
including ``/cmd@botname`` (addressed to another bot -> ignored). Other ``/xyz``
texts (e.g. ``/md file``) pass through to the normal turn engine.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Awaitable, Callable

import message_store
import telegram_store
from adapters import IncomingMessage
from telegram_adapter import PLATFORM, TelegramAdapter, clean_topic_name
from telegram_api import TelegramApi, TelegramError, TelegramNetworkError, redact

log = logging.getLogger("tg.poller")

COMMANDS = {"start", "help", "new", "stop"}
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
HELP_TEXT = (
    "DOP Agent. Send a message to talk to the agent.\n"
    "/new - start a new session\n/stop - interrupt the running turn\n/help - this text"
)


def _safe_name(name: str) -> str:
    base = os.path.basename(name or "file")
    return re.sub(r"[^\w.\- ]", "_", base)[:100] or "file"


async def _default_handle_incoming(msg: IncomingMessage) -> None:
    import handlers

    await handlers.handle_incoming(msg)


async def _default_interrupt(conversation_id: str) -> bool:
    from copilot_session import _convos

    convo = _convos.get(conversation_id)
    return bool(convo and await convo.interrupt())


async def _default_transcribe(raw: bytes, filename: str) -> str:
    import transcription

    return await transcription.transcribe_audio_bytes(raw, filename)


class TelegramPoller:
    def __init__(self, api: TelegramApi, adapter: TelegramAdapter, *, allowed_user_ids: list[str],
                 forum_chat_id: int | None = None,
                 handle_incoming: Callable[[IncomingMessage], Awaitable[None]] | None = None,
                 interrupt: Callable[[str], Awaitable[bool]] | None = None,
                 transcribe: Callable[[bytes, str], Awaitable[str]] | None = None,
                 tstore: Any = telegram_store, max_update_age: float = 600.0, poll_timeout: int = 50,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 files_dir: Path | None = None) -> None:
        self.api, self.adapter = api, adapter
        self.allowed = {str(u) for u in allowed_user_ids}
        self.forum_chat_id = forum_chat_id
        self.handle_incoming = handle_incoming or _default_handle_incoming
        self.interrupt = interrupt or _default_interrupt
        self.transcribe = transcribe or _default_transcribe
        self.tstore = tstore
        self.max_age, self.poll_timeout = max_update_age, poll_timeout
        self._clock, self._sleep = clock, sleep
        self.files_dir = files_dir or Path.home() / ".dop-agent" / "telegram_files"
        self._offset: int | None = None
        self._stopping = False
        self._backoff = 0.0
        self._tasks: set[asyncio.Task] = set()
        self._general_lock = asyncio.Lock()
        self._general_done: "OrderedDict[str, str]" = OrderedDict()
        self._recent_taps: "OrderedDict[tuple, float]" = OrderedDict()
        self.fatal: str | None = None

    # -- lifecycle -------------------------------------------------------------
    def stop(self) -> None:
        self._stopping = True

    async def start(self) -> bool:
        """getMe + deleteWebhook (long polling needs no webhook; a leftover one
        gives a permanent 409) + rights self-check. False = fatal (bad token)."""
        delay = 1.0
        while not self._stopping:
            try:
                me = await self.api.get_me()
                self.adapter.bot_username = me.get("username")
                self.adapter.bot_id = me.get("id")
                await self.api.delete_webhook(drop_pending_updates=False)
                break
            except TelegramError as exc:
                if exc.code in (401, 404):
                    self.fatal = "Telegram rejected the bot token (401/404) - poller NOT started"
                    log.error(self.fatal)
                    return False
                log.warning("Telegram startup call failed: %s", exc)
            except TelegramNetworkError as exc:
                log.warning("Telegram startup network error: %s", exc)
            await self._sleep(delay)
            delay = min(delay * 2, 60.0)
        await self.self_check()
        self._offset = self.tstore.get_offset()
        return not self._stopping

    async def self_check(self) -> dict[str, Any]:
        """Actionable diagnostics for forum setup; never raises."""
        report: dict[str, Any] = {"forum": None, "can_manage_topics": None}
        if not self.forum_chat_id:
            return report
        try:
            chat = await self.api.get_chat(self.forum_chat_id)
            report["forum"] = bool(chat.get("is_forum"))
            if not report["forum"]:
                log.error("TELEGRAM_FORUM_CHAT_ID %s is not a forum (enable Topics in group settings) - "
                          "falling back to DM mode", self.forum_chat_id)
                self.forum_chat_id = self.adapter.forum_chat_id = None
                return report
            member = await self.api.get_chat_member(self.forum_chat_id, self.adapter.bot_id or 0)
            report["can_manage_topics"] = member.get("status") == "creator" or bool(member.get("can_manage_topics"))
            if not report["can_manage_topics"]:
                log.error("Bot lacks the 'Manage Topics' admin right in chat %s - topic creation will fail",
                          self.forum_chat_id)
            if not member.get("can_delete_messages") and member.get("status") != "creator":
                log.warning("Bot lacks 'Delete messages' right (optional)")
        except (TelegramError, TelegramNetworkError) as exc:
            log.error("Telegram self-check failed: %s", exc)
        return report

    async def run(self) -> None:
        if not await self.start():
            return
        log.info("Telegram long polling started (forum=%s, allowed users=%d)", bool(self.forum_chat_id), len(self.allowed))
        while not self._stopping:
            await self.poll_once()
            await self._sleep(0)

    async def poll_once(self) -> int:
        """One getUpdates round; returns the number of updates received."""
        try:
            updates = await self.api.get_updates(offset=self._offset, timeout=self.poll_timeout)
        except TelegramError as exc:
            if exc.code in (401, 404):
                self.fatal = "Telegram rejected the bot token (401/404) - poller stopped"
                log.error(self.fatal)
                self._stopping = True
                return 0
            if exc.code == 409:
                self._backoff = min(max(self._backoff * 2, 5.0), 60.0)
                log.warning("Telegram 409 conflict (another poller or a webhook); retrying in %.0fs", self._backoff)
                try:
                    await self.api.delete_webhook(drop_pending_updates=False)
                except (TelegramError, TelegramNetworkError):
                    pass
            elif exc.code == 429:
                self._backoff = float(exc.retry_after or 5.0)
            else:
                self._backoff = min(max(self._backoff * 2, 1.0), 60.0)
                log.warning("getUpdates failed: %s", exc)
            await self._sleep(self._backoff)
            return 0
        except TelegramNetworkError as exc:
            self._backoff = min(max(self._backoff * 2, 1.0), 60.0)
            log.warning("getUpdates network error (%s); retrying in %.0fs", exc, self._backoff)
            await self._sleep(self._backoff)
            return 0
        self._backoff = 0.0
        for upd in updates:
            self._offset = upd["update_id"] + 1
            self.tstore.set_offset(self._offset)  # persist BEFORE dispatch: at-most-once
            self._spawn(self.dispatch(upd))
        return len(updates)

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # -- dispatch ----------------------------------------------------------------
    async def dispatch(self, update: dict) -> None:
        try:
            if "callback_query" in update:
                await self.handle_callback(update["callback_query"])
            elif "message" in update:
                await self.handle_message(update["message"])
            elif "my_chat_member" in update:
                log.info("my_chat_member update in chat %s", (update["my_chat_member"].get("chat") or {}).get("id"))
        except Exception as exc:  # noqa: BLE001 - one bad update must never stop polling
            log.exception("Error handling Telegram update %s: %s", update.get("update_id"), redact(exc))

    def _chat_allowed(self, chat: dict, uid: int) -> bool:
        if chat.get("type") == "private":
            return chat.get("id") == uid
        return bool(self.forum_chat_id) and chat.get("id") == self.forum_chat_id

    def _authorized(self, uid: Any) -> bool:
        ok = str(uid) in self.allowed and message_store.is_allowed_identity(PLATFORM, str(uid))
        if not ok:
            log.warning("Ignoring Telegram update from non-allowed user id=%s", uid)
        return ok

    async def handle_message(self, m: dict) -> None:
        sender, chat = m.get("from") or {}, m.get("chat") or {}
        uid = sender.get("id")
        if uid is None or sender.get("is_bot") or not self._chat_allowed(chat, uid):
            return
        if not self._authorized(uid):
            return
        if self._clock() - float(m.get("date") or 0) > self.max_age:
            log.info("Dropping stale Telegram message (age > %ss)", self.max_age)
            return
        chat_id = chat["id"]
        text = (m.get("text") or m.get("caption") or "").strip()
        cmd = self._command(text)
        if cmd:
            await self._run_command(cmd[0], cmd[1], m)
            return
        text = await self._with_attachments(m, text)
        if not text:
            return
        key = await self._route(m, text)
        if key is None:
            return
        await self.handle_incoming(IncomingMessage(
            platform=PLATFORM, user_id=str(uid), conversation_key=key, text=text,
            ref=f"{chat_id}:{m['message_id']}"))

    def _command(self, text: str) -> tuple[str, str] | None:
        if not text.startswith("/"):
            return None
        head, _, args = text.partition(" ")
        name, _, target = head[1:].partition("@")
        name = name.lower()
        if name not in COMMANDS:
            return None
        if target and self.adapter.bot_username and target.lower() != self.adapter.bot_username.lower():
            return ("", "")  # addressed to another bot
        return name, args.strip()

    # -- routing -----------------------------------------------------------------
    def _binding_key(self, m: dict) -> str | None:
        """Existing-session key for this message (no creation)."""
        chat = m["chat"]
        chat_id = chat["id"]
        if chat.get("type") == "private":
            reply = m.get("reply_to_message")
            key = self.tstore.mapped_key(chat_id, reply["message_id"]) if reply else None
            if key:
                return key
            slot = int(self.tstore.kv_get(f"dm_slot:{chat_id}", "0") or 0)
            return str(chat_id) if slot == 0 else f"{chat_id}:{slot}"
        topic = m.get("message_thread_id") if m.get("is_topic_message") else None
        return f"{chat_id}:{topic}" if topic else None

    async def _route(self, m: dict, text: str) -> str | None:
        key = self._binding_key(m)
        if key is None:
            return await self._redirect_general(m, text)
        tk = self.adapter.thread_key_from_binding(key)
        if tk and tk.get("topic_id"):
            conv = message_store.get_or_create_bound_conversation(PLATFORM, key)
            self.adapter.adopt_topic(conv, tk)
        return key

    async def _redirect_general(self, m: dict, text: str) -> str | None:
        chat_id, mid = m["chat"]["id"], m["message_id"]
        ref = f"{chat_id}:{mid}"
        async with self._general_lock:
            if ref in self._general_done:  # redelivery / concurrent duplicate: same topic
                return self._general_done[ref]
            title = clean_topic_name(text.splitlines()[0][:60] if text else None)
            tk = await self.adapter.create_topic(title)
            key = self.adapter.external_key(tk)
            conv = message_store.get_or_create_bound_conversation(PLATFORM, key)
            self.tstore.upsert_topic(tk["chat_id"], tk["topic_id"], conv, title_synced=title)
            self.adapter._topic_convs.add(conv)
            self._general_done[ref] = key
            while len(self._general_done) > 500:
                self._general_done.popitem(last=False)
        link = await self.adapter.permalink(tk)
        try:
            await self.adapter.send(tk, f"💬 {text}")
            if link:
                await self.api.send_message(
                    chat_id, f'↪ Continued in <a href="{link}">{html.escape(title, quote=False)}</a>',
                    parse_mode="HTML", reply_to_message_id=mid)
        except TelegramError as exc:
            log.warning("Could not post General redirect messages: %s", exc)
        return key

    # -- commands ----------------------------------------------------------------
    async def _reply(self, m: dict, text: str) -> None:
        topic = m.get("message_thread_id") if m.get("is_topic_message") else None
        await self.api.send_message(m["chat"]["id"], text, message_thread_id=topic)

    async def _run_command(self, name: str, args: str, m: dict) -> None:
        if not name:
            return
        chat = m["chat"]
        try:
            if name in ("start", "help"):
                await self._reply(m, HELP_TEXT)
            elif name == "stop":
                key = self._binding_key(m)
                conv = message_store.resolve_conversation_binding(PLATFORM, key) if key else None
                stopped = bool(conv and await self.interrupt(conv))
                await self._reply(m, "⏹ Stopping the running turn." if stopped else "Nothing is running here.")
            elif name == "new":
                if chat.get("type") == "private":
                    slot = int(self.tstore.kv_get(f"dm_slot:{chat['id']}", "0") or 0) + 1
                    self.tstore.kv_set(f"dm_slot:{chat['id']}", str(slot))
                    await self._reply(m, f"🆕 New session #{slot} started. Your next message goes there.")
                else:
                    title = clean_topic_name(args or "New session")
                    tk = await self.adapter.create_topic(title)
                    conv = message_store.get_or_create_bound_conversation(PLATFORM, self.adapter.external_key(tk))
                    self.tstore.upsert_topic(tk["chat_id"], tk["topic_id"], conv, title_synced=title)
                    self.adapter._topic_convs.add(conv)
                    await self.adapter.send(tk, "New session - send your first message here.")
                    link = await self.adapter.permalink(tk)
                    await self._reply(m, f"🆕 Created topic: {link}" if link else "🆕 Created a new topic.")
        except TelegramError as exc:
            log.warning("Command /%s failed: %s", name, exc)

    # -- media --------------------------------------------------------------
    @staticmethod
    def _media(m: dict) -> list[dict[str, Any]]:
        out = []
        if m.get("photo"):
            best = max(m["photo"], key=lambda p: p.get("file_size") or p.get("width", 0) * p.get("height", 0))
            out.append({"kind": "image", "file_id": best["file_id"], "name": f"photo_{m['message_id']}.jpg",
                        "size": best.get("file_size")})
        for field, kind in (("document", "document"), ("voice", "audio"), ("audio", "audio")):
            obj = m.get(field)
            if obj:
                default = {"voice": f"voice_{m['message_id']}.ogg"}.get(field, "file")
                out.append({"kind": kind, "file_id": obj["file_id"], "name": obj.get("file_name") or default,
                            "size": obj.get("file_size")})
        return out

    async def _with_attachments(self, m: dict, text: str) -> str:
        items = self._media(m)
        if not items:
            return text
        notes: list[str] = []
        for it in items:
            if (it["size"] or 0) > MAX_DOWNLOAD_BYTES:
                await self._reply(m, f"⚠️ {it['name']} is larger than 20 MB (Telegram bot download limit); not received.")
                continue
            try:
                info = await self.api.get_file(it["file_id"])
                if (info.get("file_size") or 0) > MAX_DOWNLOAD_BYTES:
                    raise TelegramError(400, "file is too big")
                raw = await self.api.download_file(info["file_path"])
            except TelegramError as exc:
                if exc.has("too big"):
                    await self._reply(m, f"⚠️ {it['name']} is larger than 20 MB (Telegram bot download limit); not received.")
                else:
                    log.warning("Attachment download failed: %s", exc)
                continue
            if it["kind"] == "audio":
                try:
                    notes.append("🎤 " + (await self.transcribe(raw, _safe_name(it["name"]))).strip())
                except Exception as exc:  # noqa: BLE001
                    log.warning("Voice transcription failed: %s", redact(exc))
                    await self._reply(m, "⚠️ Could not transcribe the voice message.")
                continue
            path = self._save(m, it["name"], raw)
            notes.append(f"[Attachment ({it['kind']}): {path}]")
        return "\n".join(p for p in [text, *notes] if p).strip()

    def _save(self, m: dict, name: str, raw: bytes) -> Path:
        folder = self.files_dir / str(m["chat"]["id"])
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = folder / f"{m['message_id']}_{_safe_name(name)}"
        path.write_bytes(raw)
        return path

    # -- callbacks ---------------------------------------------------------------
    async def handle_callback(self, cq: dict) -> None:
        cid = cq["id"]
        answered = False

        async def answer(text: str | None = None) -> None:
            nonlocal answered
            if answered:
                return
            answered = True
            try:
                await self.api.answer_callback_query(cid, text)
            except (TelegramError, TelegramNetworkError) as exc:
                log.debug("answerCallbackQuery failed: %s", exc)

        try:
            sender = cq.get("from") or {}
            msg = cq.get("message") or {}
            chat = msg.get("chat") or {}
            if not chat or not self._chat_allowed(chat, sender.get("id")) or not self._authorized(sender.get("id")):
                await answer()
                return
            data = cq.get("data") or ""
            if data.startswith("st:"):
                conv_id = data[3:]
                bound = message_store.list_conversation_bindings(conv_id, PLATFORM)
                if not any(b["external_key"].split(":", 1)[0] == str(chat["id"]) for b in bound):
                    log.warning("Ignoring st: callback for a conversation not bound to chat %s", chat["id"])
                    await answer("Not available in this chat")
                    return
                stopped = await self.interrupt(conv_id)
                await answer("Stopping…" if stopped else "Already finished")
                if not stopped and msg.get("message_id"):
                    tk = {"chat_id": chat["id"], "topic_id": msg.get("message_thread_id") if msg.get("is_topic_message") else None,
                          "slot": None}
                    try:
                        await self.adapter.update(tk, msg["message_id"], "_(already finished)_", buttons=[])
                    except (TelegramError, ValueError) as exc:
                        log.debug("Could not clear stale Stop message: %s", exc)
            elif data.startswith("ab:"):
                row = self.tstore.get_callback(data[3:])
                if not row or not row.get("text"):
                    await answer("This button has expired")
                    return
                if str(row.get("external_key") or "").split(":", 1)[0] != str(chat["id"]):
                    log.warning("Ignoring ab: callback created for another chat (tapped in %s)", chat["id"])
                    await answer("Not available in this chat")
                    return
                tap = (chat["id"], msg.get("message_id"), data)
                now = self._clock()
                if now - self._recent_taps.get(tap, 0) < 10:
                    await answer()
                    return
                self._recent_taps[tap] = now
                while len(self._recent_taps) > 200:
                    self._recent_taps.popitem(last=False)
                await answer("Sent")
                try:
                    await self.api.edit_message_reply_markup(chat["id"], msg["message_id"], {"inline_keyboard": []})
                except (TelegramError, KeyError) as exc:
                    log.debug("Could not strip keyboard after tap: %s", exc)
                await self.handle_incoming(IncomingMessage(
                    platform=PLATFORM, user_id=str(sender["id"]), conversation_key=row["external_key"],
                    text=row["text"], ref=f"cb:{cid}"))
            elif data.startswith("bt:"):
                await answer("Not supported on Telegram yet")
            else:
                await answer("Unknown action")
        finally:
            await answer()
