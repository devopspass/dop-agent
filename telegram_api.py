"""Telegram Bot API client: one injectable transport seam + typed wrappers.

Everything the bot sends to Telegram goes through ``TelegramTransport.request``
so the whole integration can be exercised offline against
``tests/fakes/fake_telegram.py`` (the dev host cannot reach api.telegram.org).
Long polling only (``getUpdates`` over outbound HTTPS) - no webhooks.

Security notes
- The token lives in the request URL (``/bot<TOKEN>/method``). ``HttpxTransport``
  never puts it into exceptions or logs: transport errors are re-raised as
  ``TelegramNetworkError`` carrying only the exception class name, and the
  ``httpx``/``httpcore`` loggers (which log full URLs at INFO) are forced to
  WARNING by ``quiet_http_loggers()``.
- ``redact()`` scrubs any ``bot<digits>:<secret>`` pattern from arbitrary text.

Timeouts: ``getUpdates`` uses ``poll_timeout + 15`` s read timeout (a plain 30 s
client would raise ReadTimeout on every empty 50 s poll), uploads 120 s, other
calls 30 s.

Rate limits: a per-chat bucket (about 1 message/s, and 18/min in groups) gates
message *sends*; edits are throttled by ``adapters.ThrottledEditor``. A 429 is
retried once after ``retry_after`` (capped at ``MAX_429_SLEEP``) for sends and
raised immediately for ``on_429="raise"`` callers (status edits drop rather
than block the terminal edit).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Awaitable, Callable, Protocol

log = logging.getLogger("tg.api")

API_BASE = "https://api.telegram.org"
MAX_429_SLEEP = 30.0
ALLOWED_UPDATES = ["message", "edited_message", "callback_query", "my_chat_member"]

_TOKEN_RE = re.compile(r"bot\d{3,}:[A-Za-z0-9_-]{8,}")


def redact(text: Any, token: str | None = None) -> str:
    out = _TOKEN_RE.sub("bot<redacted>", str(text))
    if token:
        out = out.replace(token, "<redacted>")
    return out


def quiet_http_loggers() -> None:
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


class TelegramError(Exception):
    """Bot API returned ok=false (or an HTTP error status)."""

    def __init__(self, code: int, description: str, retry_after: float | None = None,
                 method: str | None = None) -> None:
        self.code = code
        self.description = redact(description)
        self.retry_after = retry_after
        self.method = method
        super().__init__(f"Telegram {method or 'API'} error {code}: {self.description}")

    def has(self, *needles: str) -> bool:
        d = self.description.lower()
        return any(n.lower() in d for n in needles)


class TelegramNetworkError(Exception):
    """Transport-level failure (timeout, DNS, connection reset). Message never
    contains the request URL."""


class TelegramTransport(Protocol):
    async def request(self, method: str, params: dict[str, Any] | None = None,
                      files: dict[str, tuple[str, bytes]] | None = None, *,
                      read_timeout: float = 30.0) -> Any:
        """Call a Bot API method; return the ``result`` payload or raise
        ``TelegramError`` / ``TelegramNetworkError``."""
        ...


class HttpxTransport:
    def __init__(self, token: str, *, base_url: str = API_BASE, client: Any = None) -> None:
        import httpx

        quiet_http_loggers()
        self._token = token
        self._base = base_url.rstrip("/")
        self._httpx = httpx
        self._client = client or httpx.AsyncClient()

    async def request(self, method, params=None, files=None, *, read_timeout: float = 30.0):
        httpx = self._httpx
        url = f"{self._base}/bot{self._token}/{method}"
        timeout = httpx.Timeout(connect=10.0, read=read_timeout, write=read_timeout, pool=10.0)
        try:
            if files:
                data = {k: (v if isinstance(v, str) else json.dumps(v)) for k, v in (params or {}).items()}
                resp = await self._client.post(
                    url, data=data, files={k: (fn, raw) for k, (fn, raw) in files.items()}, timeout=timeout)
            else:
                resp = await self._client.post(url, json=params or {}, timeout=timeout)
        except httpx.HTTPError as exc:
            raise TelegramNetworkError(f"{method}: {type(exc).__name__}") from None
        try:
            body = resp.json()
        except ValueError:
            raise TelegramError(resp.status_code, "non-JSON response", method=method) from None
        if body.get("ok"):
            return body.get("result")
        raise TelegramError(
            int(body.get("error_code") or resp.status_code), str(body.get("description") or ""),
            retry_after=(body.get("parameters") or {}).get("retry_after"), method=method)

    async def download(self, file_path: str, *, read_timeout: float = 120.0) -> bytes:
        httpx = self._httpx
        url = f"{self._base}/file/bot{self._token}/{file_path.lstrip('/')}"
        try:
            resp = await self._client.get(url, timeout=httpx.Timeout(
                connect=10.0, read=read_timeout, write=read_timeout, pool=10.0))
        except httpx.HTTPError as exc:
            raise TelegramNetworkError(f"download: {type(exc).__name__}") from None
        if resp.status_code != 200:
            raise TelegramError(resp.status_code, "file download failed", method="download")
        return resp.content

    async def aclose(self) -> None:
        await self._client.aclose()


class ChatRateLimiter:
    """Per-chat send pacing: >= ``min_gap`` between sends in a chat, and for
    groups (negative ids) at most ``group_per_minute`` in any 60 s window."""

    def __init__(self, *, min_gap: float = 1.0, group_per_minute: int = 18,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self._gap, self._per_min = min_gap, group_per_minute
        self._clock, self._sleep = clock, sleep
        self._last: dict[int, float] = {}
        self._window: dict[int, list[float]] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    async def acquire(self, chat_id: int) -> None:
        lock = self._locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            now = self._clock()
            wait = 0.0
            last = self._last.get(chat_id)
            if last is not None:
                wait = max(wait, self._gap - (now - last))
            if chat_id < 0:
                win = [t for t in self._window.get(chat_id, []) if now - t < 60.0]
                if len(win) >= self._per_min:
                    wait = max(wait, 60.0 - (now - win[0]))
                self._window[chat_id] = win
            if wait > 0:
                await self._sleep(wait)
            now = self._clock()
            self._last[chat_id] = now
            if chat_id < 0:
                self._window.setdefault(chat_id, []).append(now)


class TelegramApi:
    def __init__(self, transport: TelegramTransport, *, poll_timeout: int = 50,
                 limiter: ChatRateLimiter | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.transport = transport
        self.poll_timeout = poll_timeout
        self.limiter = limiter or ChatRateLimiter()
        self._sleep = sleep

    async def call(self, method: str, params: dict[str, Any] | None = None, *,
                   files: dict[str, tuple[str, bytes]] | None = None,
                   read_timeout: float | None = None, on_429: str = "sleep") -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if read_timeout is None:
            read_timeout = 120.0 if files else 30.0
        for attempt in (0, 1):
            try:
                return await self.transport.request(method, params, files, read_timeout=read_timeout)
            except TelegramError as exc:
                if exc.code != 429 or on_429 == "raise" or attempt == 1:
                    raise
                delay = float(exc.retry_after or 1.0)
                if delay > MAX_429_SLEEP:
                    raise
                log.warning("Telegram 429 on %s; sleeping %.1fs", method, delay)
                await self._sleep(delay)

    # -- bot / chat info ----------------------------------------------------
    async def get_me(self) -> dict:
        return await self.call("getMe")

    async def get_chat(self, chat_id: int) -> dict:
        return await self.call("getChat", {"chat_id": chat_id})

    async def get_chat_member(self, chat_id: int, user_id: int) -> dict:
        return await self.call("getChatMember", {"chat_id": chat_id, "user_id": user_id})

    async def delete_webhook(self, drop_pending_updates: bool = False) -> bool:
        return await self.call("deleteWebhook", {"drop_pending_updates": drop_pending_updates})

    async def get_updates(self, offset: int | None = None, timeout: int | None = None) -> list[dict]:
        timeout = self.poll_timeout if timeout is None else timeout
        return await self.call(
            "getUpdates",
            {"offset": offset, "timeout": timeout, "allowed_updates": ALLOWED_UPDATES},
            read_timeout=timeout + 15.0) or []

    # -- messages -----------------------------------------------------------
    async def send_message(self, chat_id: int, text: str, *, parse_mode: str | None = None,
                           message_thread_id: int | None = None, reply_markup: dict | None = None,
                           reply_to_message_id: int | None = None, on_429: str = "sleep") -> dict:
        await self.limiter.acquire(chat_id)
        return await self.call("sendMessage", {
            "chat_id": chat_id, "text": text, "parse_mode": parse_mode,
            "message_thread_id": message_thread_id, "reply_markup": reply_markup,
            "reply_to_message_id": reply_to_message_id,
            "link_preview_options": {"is_disabled": True},
        }, on_429=on_429)

    async def edit_message_text(self, chat_id: int, message_id: int, text: str, *,
                                parse_mode: str | None = None, reply_markup: dict | None = None,
                                on_429: str = "sleep") -> dict | bool:
        try:
            return await self.call("editMessageText", {
                "chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": parse_mode,
                "reply_markup": reply_markup, "link_preview_options": {"is_disabled": True},
            }, on_429=on_429)
        except TelegramError as exc:
            if exc.has("message is not modified"):
                return True
            raise

    async def edit_message_reply_markup(self, chat_id: int, message_id: int,
                                        reply_markup: dict | None = None) -> Any:
        try:
            return await self.call("editMessageReplyMarkup", {
                "chat_id": chat_id, "message_id": message_id,
                "reply_markup": reply_markup if reply_markup is not None else {"inline_keyboard": []}})
        except TelegramError as exc:
            if exc.has("message is not modified"):
                return True
            raise

    async def delete_message(self, chat_id: int, message_id: int) -> bool:
        return await self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    async def set_message_reaction(self, chat_id: int, message_id: int, emojis: list[str]) -> bool:
        return await self.call("setMessageReaction", {
            "chat_id": chat_id, "message_id": message_id,
            "reaction": [{"type": "emoji", "emoji": e} for e in emojis]})

    async def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> bool:
        return await self.call("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text})

    # -- files --------------------------------------------------------------
    async def send_document(self, chat_id: int, data: bytes, filename: str, *, caption: str | None = None,
                            message_thread_id: int | None = None) -> dict:
        await self.limiter.acquire(chat_id)
        return await self.call("sendDocument", {
            "chat_id": chat_id, "caption": caption, "message_thread_id": message_thread_id},
            files={"document": (filename, data)})

    async def send_photo(self, chat_id: int, data: bytes, filename: str, *, caption: str | None = None,
                         message_thread_id: int | None = None) -> dict:
        await self.limiter.acquire(chat_id)
        return await self.call("sendPhoto", {
            "chat_id": chat_id, "caption": caption, "message_thread_id": message_thread_id},
            files={"photo": (filename, data)})

    async def send_media_group(self, chat_id: int, items: list[tuple[str, str, bytes, str | None]], *,
                               message_thread_id: int | None = None) -> list[dict]:
        """``items``: (kind 'photo'|'document', filename, data, caption)."""
        await self.limiter.acquire(chat_id)
        media, files = [], {}
        for i, (kind, filename, data, caption) in enumerate(items):
            name = f"f{i}"
            entry: dict[str, Any] = {"type": kind, "media": f"attach://{name}"}
            if caption:
                entry["caption"] = caption
            media.append(entry)
            files[name] = (filename, data)
        return await self.call("sendMediaGroup", {
            "chat_id": chat_id, "media": media, "message_thread_id": message_thread_id}, files=files)

    async def get_file(self, file_id: str) -> dict:
        return await self.call("getFile", {"file_id": file_id})

    async def download_file(self, file_path: str) -> bytes:
        dl = getattr(self.transport, "download", None)
        if dl is None:
            raise TelegramNetworkError("transport cannot download files")
        return await dl(file_path)

    # -- forum topics -------------------------------------------------------
    async def create_forum_topic(self, chat_id: int, name: str) -> dict:
        return await self.call("createForumTopic", {"chat_id": chat_id, "name": name[:128] or "Session"})

    async def edit_forum_topic(self, chat_id: int, thread_id: int, name: str) -> bool:
        try:
            return await self.call("editForumTopic", {
                "chat_id": chat_id, "message_thread_id": thread_id, "name": name[:128] or "Session"})
        except TelegramError as exc:
            if exc.has("TOPIC_NOT_MODIFIED", "not modified"):
                return True
            raise

    async def close_forum_topic(self, chat_id: int, thread_id: int) -> bool:
        try:
            return await self.call("closeForumTopic", {"chat_id": chat_id, "message_thread_id": thread_id})
        except TelegramError as exc:
            if exc.has("TOPIC_NOT_MODIFIED"):
                return True
            raise

    async def reopen_forum_topic(self, chat_id: int, thread_id: int) -> bool:
        try:
            return await self.call("reopenForumTopic", {"chat_id": chat_id, "message_thread_id": thread_id})
        except TelegramError as exc:
            if exc.has("TOPIC_NOT_MODIFIED"):
                return True
            raise
