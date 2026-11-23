"""SQLite state for the Telegram adapter (lives in ``bot_store.db``).

Tables are created lazily and idempotently (``CREATE IF NOT EXISTS``), keyed
off ``message_store.DB_PATH`` so tests that point the store at a temp file get
fresh tables. ``conversation_bindings`` stays the source of truth for the
conversation <-> chat/topic mapping; the tables here only hold Telegram state:

- ``telegram_kv``: update offset, per-DM "active session" slot counters.
- ``telegram_topics``: forum topic state (title last synced, closed flag) so a
  rename/archive is applied once and a deleted topic is detectable.
- ``telegram_msgmap``: bot message id -> binding key (DM reply routing).
- ``telegram_callbacks``: inline-button token -> text to send (callback_data is
  limited to 64 bytes, so the payload is persisted and survives restarts).
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import message_store as ms

_ready: set[str] = set()


def _cutoff(days: int = 30) -> str:
    when = datetime.now(timezone.utc) - timedelta(days=days)
    return when.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _c():
    key = str(ms.DB_PATH)
    if key not in _ready:
        ms.init_db()
        with ms._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS telegram_kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS telegram_topics (
                    chat_id INTEGER NOT NULL, thread_id INTEGER NOT NULL, conversation_id TEXT NOT NULL,
                    title_synced TEXT, closed INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                    PRIMARY KEY (chat_id, thread_id)
                );
                CREATE INDEX IF NOT EXISTS idx_telegram_topics_conv ON telegram_topics(conversation_id);
                CREATE TABLE IF NOT EXISTS telegram_msgmap (
                    chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, external_key TEXT NOT NULL,
                    created_at TEXT NOT NULL, PRIMARY KEY (chat_id, message_id)
                );
                CREATE TABLE IF NOT EXISTS telegram_callbacks (
                    token TEXT PRIMARY KEY, external_key TEXT, conversation_id TEXT,
                    text TEXT, action_id TEXT, created_at TEXT NOT NULL
                );
                """
            )
        _ready.add(key)
    return ms._conn()


# -- kv ------------------------------------------------------------------------
def kv_get(key: str, default: str | None = None) -> str | None:
    with _c() as conn:
        row = conn.execute("SELECT value FROM telegram_kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(key: str, value: str) -> None:
    with _c() as conn:
        conn.execute("INSERT INTO telegram_kv(key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))


def get_offset() -> int | None:
    raw = kv_get("update_offset")
    return int(raw) if raw is not None else None


def set_offset(offset: int) -> None:
    kv_set("update_offset", str(offset))


# -- topics --------------------------------------------------------------------
def upsert_topic(chat_id: int, thread_id: int, conversation_id: str, *, title_synced: str | None,
                 closed: bool = False) -> None:
    with _c() as conn:
        conn.execute(
            "INSERT INTO telegram_topics(chat_id, thread_id, conversation_id, title_synced, closed, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(chat_id, thread_id) DO UPDATE SET "
            "conversation_id = excluded.conversation_id, title_synced = excluded.title_synced, closed = excluded.closed",
            (chat_id, thread_id, conversation_id, title_synced, 1 if closed else 0, ms._now()))


def get_topic(chat_id: int, thread_id: int) -> dict[str, Any] | None:
    with _c() as conn:
        row = conn.execute("SELECT * FROM telegram_topics WHERE chat_id = ? AND thread_id = ?",
                           (chat_id, thread_id)).fetchone()
    return dict(row) if row else None


def topics_for_conversation(conversation_id: str) -> list[dict[str, Any]]:
    with _c() as conn:
        rows = conn.execute("SELECT * FROM telegram_topics WHERE conversation_id = ?",
                            (conversation_id,)).fetchall()
    return [dict(r) for r in rows]


def list_topics() -> list[dict[str, Any]]:
    with _c() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM telegram_topics").fetchall()]


def update_topic(chat_id: int, thread_id: int, **fields: Any) -> None:
    allowed = {k: v for k, v in fields.items() if k in ("title_synced", "closed")}
    if not allowed:
        return
    sets = ", ".join(f"{k} = ?" for k in allowed)
    with _c() as conn:
        conn.execute(f"UPDATE telegram_topics SET {sets} WHERE chat_id = ? AND thread_id = ?",
                     (*[int(v) if k == "closed" else v for k, v in allowed.items()], chat_id, thread_id))


def delete_topic(chat_id: int, thread_id: int) -> None:
    with _c() as conn:
        conn.execute("DELETE FROM telegram_topics WHERE chat_id = ? AND thread_id = ?", (chat_id, thread_id))


# -- DM reply routing ----------------------------------------------------------
def map_message(chat_id: int, message_id: int, external_key: str) -> None:
    with _c() as conn:
        conn.execute("INSERT OR REPLACE INTO telegram_msgmap(chat_id, message_id, external_key, created_at) "
                     "VALUES (?, ?, ?, ?)", (chat_id, message_id, external_key, ms._now()))
        conn.execute("DELETE FROM telegram_msgmap WHERE created_at < ?",
                     (_cutoff(),))


def mapped_key(chat_id: int, message_id: int) -> str | None:
    with _c() as conn:
        row = conn.execute("SELECT external_key FROM telegram_msgmap WHERE chat_id = ? AND message_id = ?",
                           (chat_id, message_id)).fetchone()
    return row["external_key"] if row else None


# -- callback tokens -----------------------------------------------------------
def put_callback(*, text: str | None = None, action_id: str | None = None, external_key: str | None = None,
                 conversation_id: str | None = None) -> str:
    token = secrets.token_hex(6)
    with _c() as conn:
        conn.execute("INSERT INTO telegram_callbacks(token, external_key, conversation_id, text, action_id, created_at) "
                     "VALUES (?, ?, ?, ?, ?, ?)", (token, external_key, conversation_id, text, action_id, ms._now()))
        conn.execute("DELETE FROM telegram_callbacks WHERE created_at < ?",
                     (_cutoff(),))
    return token


def get_callback(token: str) -> dict[str, Any] | None:
    with _c() as conn:
        row = conn.execute("SELECT * FROM telegram_callbacks WHERE token = ?", (token,)).fetchone()
    return dict(row) if row else None
