"""Strict in-memory Telegram Bot API (a ``TelegramTransport``) for offline tests.

The dev host cannot reach api.telegram.org, so every Telegram code path is
exercised against this fake. It is deliberately *stricter than convenient* so
formatting/protocol bugs surface here instead of in production:

- text limit 4096 UTF-16 units measured after entity parsing; HTML validated
  independently of ``telegram_format`` (allowed tags/attrs, nesting, ``<pre>``
  content, raw ``<``/``&``) -> "can't parse entities" 400;
- ``callback_data`` <= 64 bytes, caption <= 1024, topic name 1..128;
- ``editMessageText`` without ``reply_markup`` REMOVES the keyboard (as real
  Telegram does); an identical edit -> "message is not modified";
- closed topic -> TOPIC_CLOSED, deleted topic -> "message thread not found";
  createForumTopic needs a forum chat and the bot's ``can_manage_topics``;
- reactions limited to the standard emoji set, one per bot message;
- sendPhoto 10 MB / dimension limits, mediagroup 2-10 items with no
  photo+document mixing, getFile 20 MB, upload 50 MB;
- getUpdates honours the confirmed ``offset`` and answers 409 while a webhook is
  set or a conflicting poller is simulated.

Test helpers: ``push_message``, ``push_callback``, ``fail_next``, ``sent`` etc.
"""

from __future__ import annotations

import json
import re
import struct
import time
from html.parser import HTMLParser
from typing import Any

from telegram_api import TelegramError

ALLOWED_TAGS = {"b", "i", "u", "s", "a", "code", "pre", "blockquote", "tg-spoiler", "strong", "em", "ins", "del"}
REACTIONS = set(
    "👍 👎 ❤ 🔥 🥰 👏 😁 🤔 🤯 😱 🤬 😢 🎉 🤩 🤮 💩 🙏 👌 🕊 🤡 🥱 🥴 😍 🐳 ❤‍🔥 🌚 🌭 💯 🤣 ⚡ 🍌 🏆 💔 🤨 😐 🍓 "
    "🍾 💋 🖕 😈 😴 😭 🤓 👻 👨‍💻 👀 🎃 🙈 😇 😨 🤝 ✍ 🤗 🫡 🎅 🎄 ☃ 💅 🤪 🗿 🆒 💘 🙉 🦄 😘 💊 🙊 😎 👾 "
    "🤷‍♂ 🤷 🤷‍♀ 😡".split()
)
_VALID_ENTITY = re.compile(r"&(amp|lt|gt|quot|#\d+|#x[0-9a-fA-F]+);")
_TAG = re.compile(r"</?[a-zA-Z][\w-]*(?:\s[^<>]*)?>")


def u16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


class _Checker(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.text: list[str] = []
        self.problem: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED_TAGS:
            self.problem = f"Unsupported start tag \"{tag}\""
        elif self.stack and self.stack[-1] == "pre" and tag != "code":
            self.problem = "Entities inside pre are not supported"
        elif tag == "a":
            href = dict(attrs).get("href", "")
            if not href.lower().startswith(("http://", "https://", "tg://", "mailto:")):
                self.problem = "Wrong URL scheme"
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.problem = f"Unmatched end tag \"{tag}\""
        else:
            self.stack.pop()

    def handle_data(self, data):
        self.text.append(data)


def check_html(markup: str) -> str:
    """Return the visible text or raise the Bot API's parse error."""
    stripped = _TAG.sub("", markup)
    stripped = _VALID_ENTITY.sub("", stripped)
    if "<" in stripped or ">" in stripped or "&" in stripped:
        raise TelegramError(400, "Bad Request: can't parse entities: unescaped special character")
    c = _Checker()
    c.feed(markup)
    c.close()
    if c.problem or c.stack:
        raise TelegramError(400, f"Bad Request: can't parse entities: {c.problem or 'unclosed tag'}")
    return "".join(c.text)


def png(width: int, height: int, pad: int = 0) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00" + b"\x00" * (4 + pad)


class FakeTelegram:
    def __init__(self, *, bot_id: int = 999, username: str = "cb_test_bot", forum_chat_id: int | None = None,
                 clock=time.time) -> None:
        self.bot_id, self.username, self.clock = bot_id, username, clock
        self.calls: list[tuple[str, dict]] = []
        self.chats: dict[int, dict] = {}
        self.messages: dict[tuple[int, int], dict] = {}
        self.topics: dict[tuple[int, int], dict] = {}
        self.updates: list[dict] = []
        self.answered: list[tuple[str, str | None]] = []
        self.files: dict[str, tuple[str, bytes]] = {}
        self.webhook_url: str | None = None
        self.confirmed_offset = 0
        self.conflicts = 0
        self.rights = {"status": "administrator", "can_manage_topics": True, "can_delete_messages": True}
        self._injected: dict[str, list[Exception]] = {}
        self._next_msg: dict[int, int] = {}
        self._next_update = 100
        self._next_topic = 10
        self._cb_seq = 0
        self.forum_chat_id = forum_chat_id
        if forum_chat_id is not None:
            self.chats[forum_chat_id] = {"id": forum_chat_id, "type": "supergroup", "is_forum": True, "title": "Forum"}

    # -- test helpers --------------------------------------------------------
    def fail_next(self, method: str, error: Exception, times: int = 1) -> None:
        self._injected.setdefault(method, []).extend([error] * times)

    def calls_to(self, method: str) -> list[dict]:
        return [p for m, p in self.calls if m == method]

    def _mid(self, chat_id: int) -> int:
        self._next_msg[chat_id] = self._next_msg.get(chat_id, 0) + 1
        return self._next_msg[chat_id]

    def _push(self, **body) -> dict:
        self._next_update += 1
        upd = {"update_id": self._next_update, **body}
        self.updates.append(upd)
        return upd

    def push_message(self, chat_id: int, user_id: int, text: str | None = None, *, thread_id: int | None = None,
                     date: float | None = None, chat_type: str | None = None, reply_to: int | None = None,
                     **extra) -> dict:
        ctype = chat_type or ("private" if chat_id > 0 else "supergroup")
        msg = {"message_id": self._mid(chat_id), "date": int(date if date is not None else self.clock()),
               "chat": {"id": chat_id, "type": ctype}, "from": {"id": user_id, "is_bot": False, "first_name": "U"}}
        if ctype == "supergroup" and self.chats.get(chat_id, {}).get("is_forum"):
            msg["chat"]["is_forum"] = True
        if text is not None:
            msg["text"] = text
        if thread_id:
            msg["message_thread_id"] = thread_id
            msg["is_topic_message"] = True
        if reply_to:
            msg["reply_to_message"] = {"message_id": reply_to, "chat": {"id": chat_id}}
        msg.update(extra)
        return self._push(message=msg)

    def push_edit(self, chat_id: int, user_id: int, message_id: int, text: str) -> dict:
        return self._push(edited_message={
            "message_id": message_id, "date": int(self.clock()), "edit_date": int(self.clock()), "text": text,
            "chat": {"id": chat_id, "type": "private" if chat_id > 0 else "supergroup"},
            "from": {"id": user_id, "is_bot": False, "first_name": "U"}})

    def push_callback(self, chat_id: int, user_id: int, data: str, message_id: int, *,
                      thread_id: int | None = None) -> dict:
        self._cb_seq += 1
        message = {"message_id": message_id, "date": int(self.clock()), "chat": {"id": chat_id, "type": "private" if chat_id > 0 else "supergroup"}}
        if thread_id:
            message["message_thread_id"] = thread_id
            message["is_topic_message"] = True
        return self._push(callback_query={"id": f"cb{self._cb_seq}", "from": {"id": user_id, "is_bot": False, "first_name": "U"},
                                          "message": message, "data": data, "chat_instance": "x"})

    def add_file(self, file_id: str, path: str, data: bytes) -> None:
        self.files[file_id] = (path, data)

    def sent(self, chat_id: int | None = None) -> list[dict]:
        return [m for (c, _i), m in sorted(self.messages.items()) if (chat_id is None or c == chat_id) and not m.get("deleted")]

    def keyboard(self, chat_id: int, message_id: int) -> list | None:
        return (self.messages[(chat_id, message_id)].get("reply_markup") or {}).get("inline_keyboard") or None

    # -- transport -----------------------------------------------------------
    async def request(self, method, params=None, files=None, *, read_timeout: float = 30.0):
        params = dict(params or {})
        json.dumps(params)  # the real transport JSON-encodes params
        self.calls.append((method, {**params, **({"_files": {k: (v[0], len(v[1])) for k, v in files.items()}} if files else {})}))
        queue = self._injected.get(method)
        if queue:
            raise queue.pop(0)
        handler = getattr(self, f"_m_{method}", None)
        if handler is None:
            raise TelegramError(404, "Not Found", method=method)
        try:
            return await handler(params, files or {})
        except TelegramError as exc:
            exc.method = method
            raise

    async def download(self, file_path: str) -> bytes:
        for path, data in self.files.values():
            if path == file_path:
                return data
        raise TelegramError(404, "Not Found")

    # -- methods ---------------------------------------------------------------
    async def _m_getMe(self, p, f):
        return {"id": self.bot_id, "is_bot": True, "username": self.username, "first_name": "Bot"}

    async def _m_getChat(self, p, f):
        chat = self.chats.get(p["chat_id"])
        if chat is None:
            raise TelegramError(400, "Bad Request: chat not found")
        return dict(chat)

    async def _m_getChatMember(self, p, f):
        if p["chat_id"] not in self.chats:
            raise TelegramError(400, "Bad Request: chat not found")
        if p["user_id"] == self.bot_id:
            return {"user": {"id": self.bot_id}, **self.rights}
        return {"user": {"id": p["user_id"]}, "status": "member"}

    async def _m_deleteWebhook(self, p, f):
        self.webhook_url = None
        return True

    async def _m_getUpdates(self, p, f):
        if "allowed_updates" not in p:
            raise TelegramError(400, "test strictness: always pass allowed_updates explicitly")
        if self.webhook_url:
            raise TelegramError(409, "Conflict: can't use getUpdates method while webhook is active")
        if self.conflicts > 0:
            self.conflicts -= 1
            raise TelegramError(409, "Conflict: terminated by other getUpdates request")
        offset = p.get("offset")
        if offset is not None:
            self.confirmed_offset = max(self.confirmed_offset, offset)
            self.updates = [u for u in self.updates if u["update_id"] >= offset]
        return [dict(u) for u in self.updates[:100]]

    def _check_thread(self, chat_id: int, thread_id: int | None) -> None:
        if thread_id is None:
            return
        topic = self.topics.get((chat_id, thread_id))
        if topic is None or topic.get("deleted"):
            raise TelegramError(400, "Bad Request: message thread not found")
        if topic["closed"]:
            raise TelegramError(400, "Bad Request: TOPIC_CLOSED")

    @staticmethod
    def _check_markup(markup) -> None:
        if markup is None:
            return
        rows = markup.get("inline_keyboard")
        if not isinstance(rows, list):
            raise TelegramError(400, "Bad Request: reply markup is invalid")
        for row in rows:
            if not isinstance(row, list):
                raise TelegramError(400, "Bad Request: keyboard row must be a list")
            for btn in row:
                if not btn.get("text"):
                    raise TelegramError(400, "Bad Request: button text empty")
                data = btn.get("callback_data")
                if data is not None and len(data.encode()) > 64:
                    raise TelegramError(400, "Bad Request: BUTTON_DATA_INVALID")
                if data is None and "url" not in btn:
                    raise TelegramError(400, "Bad Request: button must have data or url")

    def _visible(self, p) -> str:
        text = p.get("text")
        if not isinstance(text, str) or not text.strip():
            raise TelegramError(400, "Bad Request: message text is empty")
        visible = check_html(text) if p.get("parse_mode") == "HTML" else text
        if u16(visible) > 4096:
            raise TelegramError(400, "Bad Request: message is too long")
        return visible

    def _store(self, chat_id, **fields) -> dict:
        msg = {"message_id": self._mid(chat_id), "chat": {"id": chat_id}, "date": int(self.clock()), **fields}
        self.messages[(chat_id, msg["message_id"])] = msg
        return {k: v for k, v in msg.items() if k in ("message_id", "chat", "date", "message_thread_id")}

    async def _m_sendMessage(self, p, f):
        chat = p["chat_id"]
        if chat not in self.chats and chat < 0:
            raise TelegramError(400, "Bad Request: chat not found")
        self._visible(p)
        self._check_thread(chat, p.get("message_thread_id"))
        self._check_markup(p.get("reply_markup"))
        return self._store(chat, text=p["text"], parse_mode=p.get("parse_mode"), reply_markup=p.get("reply_markup"),
                           message_thread_id=p.get("message_thread_id"))

    async def _m_editMessageText(self, p, f):
        key = (p["chat_id"], p["message_id"])
        msg = self.messages.get(key)
        if msg is None or msg.get("deleted"):
            raise TelegramError(400, "Bad Request: message to edit not found")
        self._visible(p)
        self._check_markup(p.get("reply_markup"))
        self._check_thread(p["chat_id"], msg.get("message_thread_id"))
        new_markup = p.get("reply_markup")
        norm = lambda m: (m or {}).get("inline_keyboard") or None  # noqa: E731
        if msg["text"] == p["text"] and norm(msg.get("reply_markup")) == norm(new_markup) and msg.get("parse_mode") == p.get("parse_mode"):
            raise TelegramError(400, "Bad Request: message is not modified")
        msg.update(text=p["text"], parse_mode=p.get("parse_mode"), reply_markup=new_markup)  # absent markup => stripped
        return {"message_id": msg["message_id"], "chat": {"id": p["chat_id"]}}

    async def _m_editMessageReplyMarkup(self, p, f):
        msg = self.messages.get((p["chat_id"], p["message_id"]))
        if msg is None:
            raise TelegramError(400, "Bad Request: message to edit not found")
        self._check_markup(p.get("reply_markup"))
        if not (msg.get("reply_markup") or {}).get("inline_keyboard") and not (p.get("reply_markup") or {}).get("inline_keyboard"):
            raise TelegramError(400, "Bad Request: message is not modified")
        msg["reply_markup"] = p.get("reply_markup")
        return True

    async def _m_deleteMessage(self, p, f):
        msg = self.messages.get((p["chat_id"], p["message_id"]))
        if msg is None or msg.get("deleted"):
            raise TelegramError(400, "Bad Request: message to delete not found")
        msg["deleted"] = True
        return True

    async def _m_setMessageReaction(self, p, f):
        msg = self.messages.get((p["chat_id"], p["message_id"]))
        if msg is None:
            raise TelegramError(400, "Bad Request: message not found")
        emojis = [r["emoji"] for r in p.get("reaction", [])]
        if len(emojis) > 1 or any(e not in REACTIONS for e in emojis):
            raise TelegramError(400, "Bad Request: REACTION_INVALID")
        msg["reactions"] = emojis
        return True

    async def _m_answerCallbackQuery(self, p, f):
        self.answered.append((p["callback_query_id"], p.get("text")))
        return True

    # -- topics ----------------------------------------------------------------
    async def _m_createForumTopic(self, p, f):
        chat = self.chats.get(p["chat_id"])
        if chat is None or not chat.get("is_forum"):
            raise TelegramError(400, "Bad Request: the chat is not a forum")
        if not self.rights.get("can_manage_topics"):
            raise TelegramError(400, "Bad Request: not enough rights to create a topic")
        name = p.get("name", "")
        if not 1 <= len(name) <= 128:
            raise TelegramError(400, "Bad Request: TOPIC_NAME_INVALID")
        self._next_topic += 1
        self.topics[(p["chat_id"], self._next_topic)] = {"name": name, "closed": False}
        return {"message_thread_id": self._next_topic, "name": name}

    def _topic(self, p):
        t = self.topics.get((p["chat_id"], p["message_thread_id"]))
        if t is None or t.get("deleted"):
            raise TelegramError(400, "Bad Request: message thread not found")
        return t

    async def _m_editForumTopic(self, p, f):
        t = self._topic(p)
        if not 1 <= len(p.get("name", "")) <= 128:
            raise TelegramError(400, "Bad Request: TOPIC_NAME_INVALID")
        if t["name"] == p["name"]:
            raise TelegramError(400, "Bad Request: TOPIC_NOT_MODIFIED")
        t["name"] = p["name"]
        return True

    async def _m_closeForumTopic(self, p, f):
        t = self._topic(p)
        if t["closed"]:
            raise TelegramError(400, "Bad Request: TOPIC_NOT_MODIFIED")
        t["closed"] = True
        return True

    async def _m_reopenForumTopic(self, p, f):
        t = self._topic(p)
        if not t["closed"]:
            raise TelegramError(400, "Bad Request: TOPIC_NOT_MODIFIED")
        t["closed"] = False
        return True

    def delete_topic(self, chat_id: int, thread_id: int) -> None:
        self.topics[(chat_id, thread_id)]["deleted"] = True

    # -- files -----------------------------------------------------------------
    def _check_upload(self, p, f, field):
        name, data = f[field]
        if len(data) > 50 * 1024 * 1024:
            raise TelegramError(413, "Request Entity Too Large")
        if p.get("caption") and u16(p["caption"]) > 1024:
            raise TelegramError(400, "Bad Request: message caption is too long")
        self._check_thread(p["chat_id"], p.get("message_thread_id"))
        return name, data

    async def _m_sendDocument(self, p, f):
        name, data = self._check_upload(p, f, "document")
        return self._store(p["chat_id"], document=name, size=len(data), caption=p.get("caption"),
                           message_thread_id=p.get("message_thread_id"))

    async def _m_sendPhoto(self, p, f):
        name, data = self._check_upload(p, f, "photo")
        if len(data) > 10 * 1024 * 1024:
            raise TelegramError(400, "Bad Request: PHOTO_SAVE_FILE_INVALID")
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = struct.unpack(">II", data[16:24])
            if w + h > 10000 or max(w, h) / max(1, min(w, h)) > 20:
                raise TelegramError(400, "Bad Request: PHOTO_INVALID_DIMENSIONS")
        return self._store(p["chat_id"], photo=name, size=len(data), caption=p.get("caption"),
                           message_thread_id=p.get("message_thread_id"))

    async def _m_sendMediaGroup(self, p, f):
        media = p["media"]
        if not 2 <= len(media) <= 10:
            raise TelegramError(400, "Bad Request: media group must have 2-10 items")
        kinds = {m["type"] for m in media}
        if "document" in kinds and kinds != {"document"}:
            raise TelegramError(400, "Bad Request: documents can only be grouped with documents")
        self._check_thread(p["chat_id"], p.get("message_thread_id"))
        out = []
        for m in media:
            name, data = f[m["media"].removeprefix("attach://")]
            if m["type"] == "photo" and data[:8] == b"\x89PNG\r\n\x1a\n":
                w, h = struct.unpack(">II", data[16:24])
                if w + h > 10000 or max(w, h) / max(1, min(w, h)) > 20:
                    raise TelegramError(400, "Bad Request: PHOTO_INVALID_DIMENSIONS")
            if m.get("caption") and u16(m["caption"]) > 1024:
                raise TelegramError(400, "Bad Request: message caption is too long")
            out.append(self._store(p["chat_id"], **{m["type"]: name}, size=len(data), message_thread_id=p.get("message_thread_id")))
        return out

    async def _m_getFile(self, p, f):
        entry = self.files.get(p["file_id"])
        if entry is None:
            raise TelegramError(400, "Bad Request: invalid file_id")
        path, data = entry
        if len(data) > 20 * 1024 * 1024:
            raise TelegramError(400, "Bad Request: file is too big")
        return {"file_id": p["file_id"], "file_path": path, "file_size": len(data)}
