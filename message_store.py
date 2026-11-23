"""
Bot-owned SQLite data layer (``bot_store.db``): conversations, messages,
platform bindings, pins/groups/notes/bookmarks, attachments and allowed
identities. Used by both dop_agent.py/handlers.py (writing the durable
conversation <-> Copilot-session mapping and mirrored messages) and
web_server.py (reading the same data to render the Web UI).

This is the lowest-level module in the dependency graph — it has no
project-local imports, so anything can depend on it without circularity.

Gotchas:
  - Foreign keys: the ``messages`` table has a FK to ``conversations``. Any
    code (including tests) that writes a message must first call
    ``get_or_create_conversation(...)`` for that conversation id — using an
    ad-hoc string id that was never registered raises
    ``sqlite3.IntegrityError: FOREIGN KEY constraint failed``.
  - ``_change_listeners``: web_server.py registers a callback here so it can
    push SSE updates when a conversation changes — don't remove writes to
    this list assuming nothing reads them.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import tempfile
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import config

log = logging.getLogger("message_store")

SLACK_PLATFORM = "slack"
WEB_PLATFORM = "web"
TELEGRAM_PLATFORM = "telegram"
WEB_UI_IDENTITY = "web-ui"

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "bot_store.db"
_TITLES_FILE = BASE_DIR / "session_titles.json"

_TITLE_META_RE = __import__("re").compile(r"^\[Slack context.*?\n(?:- .*\n?)+\n?", __import__("re").MULTILINE)

_change_listeners: list[Callable[[str], None]] = []


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


_local = threading.local()
_initialized_paths: set[str] = set()


def _secure_db_files(path: str) -> None:
    """Keep the DB and its WAL/SHM sidecars owner-only (0600); best effort."""
    for suffix in ("", "-wal", "-shm"):
        try:
            os.chmod(path + suffix, 0o600)
        except OSError:
            pass


def _precreate_private(path: str) -> None:
    if not os.path.exists(path):
        try:
            os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
        except OSError:
            pass


def _thread_conn() -> sqlite3.Connection:
    """Per-thread cached connection (a fresh connection per call is costly:
    WAL open + schema read + PRAGMAs + antivirus scanning of each file open)."""
    path = str(DB_PATH)
    conn = getattr(_local, "conn", None)
    if conn is not None and (_local.path != path or not os.path.exists(path)):
        try:
            conn.close()
        except Exception:
            pass
        conn = None
    if conn is None:
        _precreate_private(path)
        conn = sqlite3.connect(path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        _secure_db_files(path)
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn, _local.path, _local.depth = conn, path, 0
    return conn


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    # Connections are reused per thread and are NOT closed here. Nested
    # use shares one connection; only the outermost block commits/rolls back.
    conn = _thread_conn()
    _local.depth += 1
    try:
        yield conn
        if _local.depth == 1:
            conn.commit()
    except Exception:
        if _local.depth == 1:
            conn.rollback()
        raise
    finally:
        _local.depth -= 1


def _ensure_db() -> None:
    """Cheap per-call guard: run init_db() once per DB path per process."""
    if str(DB_PATH) not in _initialized_paths or not os.path.exists(DB_PATH):
        init_db()


def init_db() -> None:
    _initialized_paths.add(str(DB_PATH))
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                copilot_session_id TEXT,
                pinned INTEGER NOT NULL DEFAULT 0,
                pin_order INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(conversations)").fetchall()}
        if "pin_order" not in existing_cols:
            conn.execute("ALTER TABLE conversations ADD COLUMN pin_order INTEGER")
        if "archived" not in existing_cols:
            conn.execute("ALTER TABLE conversations ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
        if "note" not in existing_cols:
            # Private per-session note-to-self, never sent to the agent
            # (see get_note()/set_note()).
            conn.execute("ALTER TABLE conversations ADD COLUMN note TEXT")
        if "last_read_at" not in existing_cols:
            # Server-side read/unread state, durable across browsers/devices
            # (see get_last_read()/mark_read()).
            conn.execute("ALTER TABLE conversations ADD COLUMN last_read_at TEXT")
        if "parent_conversation_id" not in existing_cols:
            # Conversation this one was spawned from (Web UI "Continue in new
            # session", session_create MCP tool), for a backlink. No FK: it
            # can't be added via ALTER TABLE, and the parent may be deleted
            # independently; used for best-effort display only.
            conn.execute("ALTER TABLE conversations ADD COLUMN parent_conversation_id TEXT")
        if "turn_generation" not in existing_cols:
            # Last-completed-turn generation counter, so a session_turn
            # watch's baseline survives a bot restart. Only completed turns
            # are persisted: a freshly-restarted process never has a turn in
            # flight (see CopilotConversation.__init__ and send()).
            conn.execute("ALTER TABLE conversations ADD COLUMN turn_generation INTEGER NOT NULL DEFAULT 0")
        if "group_id" not in existing_cols:
            # A conversation belongs to at most one group; NULL = ungrouped.
            # No FK (ALTER TABLE can't add one) - delete_group() explicitly
            # unassigns members.
            conn.execute("ALTER TABLE conversations ADD COLUMN group_id TEXT")
        if "is_group_master" not in existing_cols:
            conn.execute("ALTER TABLE conversations ADD COLUMN is_group_master INTEGER NOT NULL DEFAULT 0")
        if "group_order" not in existing_cols:
            # Manual order within the group's list (independent of
            # pin_order/updated_at); NULL falls back to the Web UI's default
            # master-first/insertion ordering.
            conn.execute("ALTER TABLE conversations ADD COLUMN group_order INTEGER")
        if "model" not in existing_cols:
            # SDK model the session was created with (NULL = CLI default) and
            # why. Re-applied on every resume_session.
            conn.execute("ALTER TABLE conversations ADD COLUMN model TEXT")
        if "model_reason" not in existing_cols:
            conn.execute("ALTER TABLE conversations ADD COLUMN model_reason TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS session_groups (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                color TEXT,
                emoji TEXT,
                description TEXT,
                archived INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        group_cols = {r["name"] for r in conn.execute("PRAGMA table_info(session_groups)").fetchall()}
        if "sort_order" not in group_cols:
            # Manual drag-and-drop order of groups in the Grouped view; NULL = by created_at.
            conn.execute("ALTER TABLE session_groups ADD COLUMN sort_order INTEGER")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'status', 'system')),
                content TEXT NOT NULL,
                seq INTEGER NOT NULL,
                external_ref TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_conversation_seq ON messages(conversation_id, seq)")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_external_ref ON messages(external_ref) WHERE external_ref IS NOT NULL"
        )
        existing_message_cols = {row["name"] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
        if "actions" not in existing_message_cols:
            # Interactive button row (e.g. voice-transcription buttons):
            # JSON list of {"id", "label"} dicts, NULL when none. A column
            # is needed because message content alone can't tell "button
            # still live" from "already consumed".
            conn.execute("ALTER TABLE messages ADD COLUMN actions TEXT")
        # Bookmarked/starred messages (side table, keeps ``messages`` free of
        # a mutable flag column).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS message_bookmarks (
                conversation_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (conversation_id, message_id),
                FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
            )
            """
        )
        # File attachments captured from Slack file_share events (see
        # handlers.py::maybe_capture_images). ``messages`` only carries the
        # caption text; ON DELETE CASCADE ties attachment rows' lifetime to
        # the conversation (the retention policy, no separate TTL job).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS message_attachments (
                id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                slack_file_id TEXT,
                filename TEXT,
                mimetype TEXT,
                size INTEGER,
                local_path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_message_attachments_message ON message_attachments(message_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_message_attachments_conversation ON message_attachments(conversation_id)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversation_bindings (
                conversation_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                external_key TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (conversation_id, platform, external_key),
                UNIQUE (platform, external_key),
                FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_conversation_bindings_conversation ON conversation_bindings(conversation_id)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS inbox_dismissed (
                conversation_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (conversation_id, message_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS allowed_identities (
                platform TEXT NOT NULL,
                external_user_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (platform, external_user_id)
            )
            """
        )
        # Older databases may carry an inert ``slack_key`` column (SQLite
        # cannot drop a UNIQUE column without a table rebuild). Bindings are
        # the only source of truth; fold any unbound values into them.
        legacy_slack_rows = (
            conn.execute(
                "SELECT id, slack_key, created_at FROM conversations WHERE slack_key IS NOT NULL"
            ).fetchall()
            if "slack_key" in existing_cols
            else []
        )
        for row in legacy_slack_rows:
            conn.execute(
                """
                INSERT OR IGNORE INTO conversation_bindings(conversation_id, platform, external_key, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (row["id"], SLACK_PLATFORM, row["slack_key"], row["created_at"] or _now()),
            )
        for external_user_id in _seeded_allowed_identities_from_env():
            conn.execute(
                """
                INSERT OR IGNORE INTO allowed_identities(platform, external_user_id, created_at)
                VALUES (?, ?, ?)
                """,
                external_user_id,
            )
        _revoke_unlisted_telegram_identities(conn)


def _revoke_unlisted_telegram_identities(conn: sqlite3.Connection) -> None:
    """When ``TELEGRAM_ALLOWED_USER_IDS`` is defined in the environment it is
    authoritative for the telegram allow-list: ids removed from it are revoked
    (INSERT OR IGNORE seeding alone is add-only). Unset = leave rows alone."""
    raw = os.getenv("TELEGRAM_ALLOWED_USER_IDS")
    if raw is None:
        return
    keep = [p.strip() for p in raw.replace(";", ",").split(",") if p.strip().lstrip("-").isdigit()]
    marks = ",".join("?" for _ in keep)
    conn.execute(
        f"DELETE FROM allowed_identities WHERE platform = ?{f' AND external_user_id NOT IN ({marks})' if keep else ''}",
        (TELEGRAM_PLATFORM, *keep),
    )


def register_change_listener(listener: Callable[[str], None]) -> None:
    if listener not in _change_listeners:
        _change_listeners.append(listener)


def _notify_change(conversation_id: str) -> None:
    for listener in list(_change_listeners):
        try:
            listener(conversation_id)
        except Exception:
            log.exception("message_store change listener failed for %s", conversation_id)


def slack_external_ref(channel: str, ts: str) -> str:
    return f"slack:{channel}:{ts}"


def _seeded_allowed_identities_from_env() -> list[tuple[str, str, str]]:
    now = _now()
    seeded: list[tuple[str, str, str]] = []
    raw_slack_ids = config.value("adapters.slack.allowed_user_id") or ""
    for user_id in raw_slack_ids.replace(";", ",").split(","):
        cleaned = user_id.strip()
        if cleaned:
            seeded.append((SLACK_PLATFORM, cleaned, now))
    for user_id in config.value("adapters.telegram.allowed_user_ids") or []:
        cleaned = str(user_id).strip()
        if cleaned.lstrip("-").isdigit():
            seeded.append((TELEGRAM_PLATFORM, cleaned, now))
    if config.secret_value("adapters.web.password").strip():
        seeded.append((WEB_PLATFORM, WEB_UI_IDENTITY, now))
    return seeded


def _bind_conversation(
    conn: sqlite3.Connection,
    conversation_id: str,
    platform: str,
    external_key: str,
    *,
    created_at: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO conversation_bindings(conversation_id, platform, external_key, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (conversation_id, platform, external_key, created_at or _now()),
    )


def add_allowed_identity(platform: str, external_user_id: str) -> None:
    platform = (platform or "").strip()
    external_user_id = (external_user_id or "").strip()
    if not platform or not external_user_id:
        return
    _ensure_db()
    with _conn() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO allowed_identities(platform, external_user_id, created_at)
            VALUES (?, ?, ?)
            """,
            (platform, external_user_id, _now()),
        )


def list_allowed_identities(platform: str | None = None) -> list[dict[str, str]]:
    _ensure_db()
    query = "SELECT platform, external_user_id FROM allowed_identities"
    params: tuple[Any, ...] = ()
    if platform:
        query += " WHERE platform = ?"
        params = (platform,)
    query += " ORDER BY platform ASC, external_user_id ASC"
    with _conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [
        {"platform": str(row["platform"]), "external_user_id": str(row["external_user_id"])}
        for row in rows
    ]


def is_allowed_identity(platform: str, external_user_id: str) -> bool:
    platform = (platform or "").strip()
    external_user_id = (external_user_id or "").strip()
    if not platform or not external_user_id:
        return False
    _ensure_db()
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM allowed_identities
            WHERE platform = ? AND external_user_id = ?
            """,
            (platform, external_user_id),
        ).fetchone()
    return row is not None


def list_all_bindings() -> dict[str, list[dict[str, str]]]:
    """Every binding grouped by conversation_id (one query; used by the
    sessions payload instead of one query per conversation)."""
    _ensure_db()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT conversation_id, platform, external_key FROM conversation_bindings "
            "ORDER BY created_at ASC, external_key ASC"
        ).fetchall()
    out: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        out.setdefault(str(row["conversation_id"]), []).append(
            {"platform": str(row["platform"]), "external_key": str(row["external_key"])}
        )
    return out


def list_conversation_bindings(conversation_id: str, platform: str | None = None) -> list[dict[str, str]]:
    _ensure_db()
    query = "SELECT platform, external_key FROM conversation_bindings WHERE conversation_id = ?"
    params: list[Any] = [conversation_id]
    if platform:
        query += " AND platform = ?"
        params.append(platform)
    query += " ORDER BY created_at ASC, external_key ASC"
    with _conn() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [
        {"platform": str(row["platform"]), "external_key": str(row["external_key"])}
        for row in rows
    ]


def get_primary_binding(conversation_id: str, platform: str) -> str | None:
    _ensure_db()
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT external_key
            FROM conversation_bindings
            WHERE conversation_id = ? AND platform = ?
            ORDER BY created_at ASC, external_key ASC
            LIMIT 1
            """,
            (conversation_id, platform),
        ).fetchone()
    return str(row["external_key"]) if row else None


def bind_conversation(conversation_id: str, platform: str, external_key: str) -> None:
    _ensure_db()
    now = _now()
    with _conn() as conn:
        _bind_conversation(conn, conversation_id, platform, external_key, created_at=now)
        conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (now, conversation_id),
        )
    _notify_change(conversation_id)


def unbind_conversation(conversation_id: str, platform: str, external_key: str) -> bool:
    """Remove one binding (e.g. a Telegram topic deleted by hand) so
    ``delivery_for`` stops choosing it. Returns True if a row was removed."""
    _ensure_db()
    with _conn() as conn:
        cur = conn.execute(
            "DELETE FROM conversation_bindings WHERE conversation_id = ? AND platform = ? AND external_key = ?",
            (conversation_id, platform, external_key),
        )
    if cur.rowcount:
        _notify_change(conversation_id)
    return bool(cur.rowcount)


def resolve_conversation_binding(platform: str, external_key: str) -> str | None:
    _ensure_db()
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT conversation_id
            FROM conversation_bindings
            WHERE platform = ? AND external_key = ?
            """,
            (platform, external_key),
        ).fetchone()
    return str(row["conversation_id"]) if row else None


def _touch_conversation(conn: sqlite3.Connection, conversation_id: str, when: str) -> None:
    conn.execute(
        "UPDATE conversations SET updated_at = ? WHERE id = ?",
        (when, conversation_id),
    )


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


_json_write_lock = threading.RLock()


def atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON atomically and privately: unique tmp file in the same dir (0600), fsync, os.replace."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(data, indent=2))
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_titles() -> dict[str, str]:
    try:
        data = json.loads(_TITLES_FILE.read_text())
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items() if str(v).strip()}
    except FileNotFoundError:
        pass
    except Exception as exc:
        log.warning("Could not read %s (%s) — treating as no custom titles", _TITLES_FILE, exc)
    return {}


def _save_titles(titles: dict[str, str]) -> None:
    try:
        atomic_write_json(_TITLES_FILE, titles)
    except Exception as exc:
        log.warning("Could not write %s (%s)", _TITLES_FILE, exc)


def _title_for(conversation_id: str, slack_key: str | None, first_message: str | None) -> tuple[str, bool]:
    custom_titles = _load_titles()
    if conversation_id in custom_titles:
        return custom_titles[conversation_id], True
    if slack_key and slack_key in custom_titles:
        return custom_titles[slack_key], True
    return _extract_title(first_message), False


def _extract_title(text: str | None) -> str:
    text = (text or "").strip()
    if not text:
        return "(new session)"
    stripped = _TITLE_META_RE.sub("", text, count=1).strip()
    if stripped:
        text = stripped
    return " ".join(text.split())


def _compact_preview(text: str | None, limit: int = 160) -> str:
    compact = " ".join((text or "").split())
    return compact[:limit]


def get_or_create_conversation(
    slack_key: str | None,
    copilot_session_id: str | None = None,
    *,
    parent_conversation_id: str | None = None,
) -> str:
    """Create/resolve a conversation. ``parent_conversation_id`` is only
    meaningful when creating a brand-new standalone conversation (no
    ``slack_key``) - it's ignored when resolving an existing/bound
    conversation, since a conversation's origin shouldn't be rewritten once
    it already exists. Used by both the Web UI's "Continue in new session"
    button and the session_create MCP tool to record "spawned from
    conversation X" for a backlink."""
    _ensure_db()
    if slack_key:
        return get_or_create_bound_conversation(SLACK_PLATFORM, slack_key, copilot_session_id=copilot_session_id)
    now = _now()
    with _conn() as conn:
        conversation_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO conversations(id, copilot_session_id, pinned, created_at, updated_at, parent_conversation_id) "
            "VALUES (?, ?, 0, ?, ?, ?)",
            (conversation_id, copilot_session_id, now, now, parent_conversation_id),
        )
    _notify_change(conversation_id)
    return conversation_id


def resolve_conversation_by_slack_key(slack_key: str) -> str | None:
    return resolve_conversation_binding(SLACK_PLATFORM, slack_key)


def get_or_create_bound_conversation(
    platform: str,
    external_key: str,
    *,
    copilot_session_id: str | None = None,
) -> str:
    """Binding-aware conversation resolver/creator used by channel-specific
    entry points (conversation_bindings is the only source of truth)."""
    _ensure_db()
    now = _now()
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT c.id, c.copilot_session_id
            FROM conversation_bindings b
            JOIN conversations c ON c.id = b.conversation_id
            WHERE b.platform = ? AND b.external_key = ?
            """,
            (platform, external_key),
        ).fetchone()
        if row is not None:
            conversation_id = str(row["id"])
            _bind_conversation(conn, conversation_id, platform, external_key, created_at=now)
            if copilot_session_id and row["copilot_session_id"] != copilot_session_id:
                conn.execute(
                    "UPDATE conversations SET copilot_session_id = ?, updated_at = ? WHERE id = ?",
                    (copilot_session_id, now, conversation_id),
                )
            _notify_change(conversation_id)
            return conversation_id

        conversation_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO conversations(id, copilot_session_id, pinned, created_at, updated_at) VALUES (?, ?, 0, ?, ?)",
            (conversation_id, copilot_session_id, now, now),
        )
        _bind_conversation(conn, conversation_id, platform, external_key, created_at=now)
    _notify_change(conversation_id)
    return conversation_id


def get_turn_generation(conversation_id: str) -> int:
    """Read the persisted last-completed-turn generation for a conversation;
    0 if it has no row yet or never completed a turn."""
    _ensure_db()
    with _conn() as conn:
        row = conn.execute(
            "SELECT turn_generation FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
    if row is None or row["turn_generation"] is None:
        return 0
    return int(row["turn_generation"])


def set_turn_generation(conversation_id: str, value: int) -> None:
    """Persist the latest completed turn_generation for a conversation, so a
    session_turn watch's baseline math (watchers.py's
    _poll_session_turn_watch) survives a bot restart. Best-effort: a
    conversation_id with no row yet (e.g. an in-memory-only conversation
    that was never routed through get_or_create_conversation()) is silently
    skipped rather than inserting a partial row — this mirrors
    _touch_conversation's assumption that the row already exists."""
    _ensure_db()
    now = _now()
    with _conn() as conn:
        conn.execute(
            "UPDATE conversations SET turn_generation = ?, updated_at = ? WHERE id = ?",
            (value, now, conversation_id),
        )


def get_conversation(conversation_id: str) -> dict[str, Any] | None:
    _ensure_db()
    with _conn() as conn:
        row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
    return _row_to_dict(row)


def set_conversation_model(conversation_id: str, model: str | None, reason: str | None) -> None:
    """Record the routed model (None = CLI default) and why."""
    _ensure_db()
    with _conn() as conn:
        conn.execute("UPDATE conversations SET model = ?, model_reason = ? WHERE id = ?", (model, reason, conversation_id))
    _notify_change(conversation_id)


def get_conversation_model(conversation_id: str) -> str | None:
    convo = get_conversation(conversation_id)
    return (convo or {}).get("model") or None


def set_slack_key(conversation_id: str, slack_key: str | None) -> None:
    _ensure_db()
    now = _now()
    with _conn() as conn:
        conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))
        if slack_key:
            _bind_conversation(conn, conversation_id, SLACK_PLATFORM, slack_key, created_at=now)
    _notify_change(conversation_id)


def get_copilot_session_id(conversation_id: str) -> str | None:
    _ensure_db()
    with _conn() as conn:
        row = conn.execute(
            "SELECT copilot_session_id FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
    return str(row["copilot_session_id"]) if row and row["copilot_session_id"] else None


def set_copilot_session_id(conversation_id: str, session_id: str | None) -> None:
    _ensure_db()
    now = _now()
    with _conn() as conn:
        conn.execute(
            "UPDATE conversations SET copilot_session_id = ?, updated_at = ? WHERE id = ?",
            (session_id, now, conversation_id),
        )
    _notify_change(conversation_id)


def set_pinned(conversation_id: str, pinned: bool) -> None:
    # Note: intentionally does NOT touch updated_at - pinning/unpinning is a
    # metadata change, not conversation activity, and updated_at also drives
    # last-activity display/ordering for the unpinned list.
    _ensure_db()
    with _conn() as conn:
        if pinned:
            max_order = conn.execute(
                "SELECT MAX(pin_order) AS m FROM conversations WHERE pinned = 1"
            ).fetchone()["m"]
            next_order = (max_order or 0) + 1
            conn.execute(
                "UPDATE conversations SET pinned = 1, pin_order = ? WHERE id = ?",
                (next_order, conversation_id),
            )
        else:
            conn.execute(
                "UPDATE conversations SET pinned = 0, pin_order = NULL WHERE id = ?",
                (conversation_id,),
            )
    _notify_change(conversation_id)


def set_archived(conversation_id: str, archived: bool) -> None:
    # Like pinning, archiving is a metadata change, not conversation
    # activity - don't touch updated_at, or archiving/unarchiving would
    # reshuffle the unpinned list's most-recent-first ordering.
    _ensure_db()
    with _conn() as conn:
        conn.execute(
            "UPDATE conversations SET archived = ? WHERE id = ?",
            (1 if archived else 0, conversation_id),
        )
    _notify_change(conversation_id)


def set_pin_order(ordered_conversation_ids: list[str]) -> None:
    """Reorder currently-pinned conversations per a user drag-and-drop action.

    Only affects conversations already pinned; unknown/unpinned ids are ignored.
    """
    _ensure_db()
    with _conn() as conn:
        for index, conversation_id in enumerate(ordered_conversation_ids):
            conn.execute(
                "UPDATE conversations SET pin_order = ? WHERE id = ? AND pinned = 1",
                (index, conversation_id),
            )
    for conversation_id in ordered_conversation_ids:
        _notify_change(conversation_id)


def set_group_order(ordered_conversation_ids: list[str]) -> None:
    """Reorder sessions within a group's Grouped-view list per a user
    drag-and-drop action. Unlike set_pin_order, this isn't filtered to a
    particular group server-side - the caller (Web UI) is expected to pass
    the full, already-correct list of conversation_ids for one group, and
    each id's group_order is simply set to its index in that list.
    """
    _ensure_db()
    with _conn() as conn:
        for index, conversation_id in enumerate(ordered_conversation_ids):
            conn.execute(
                "UPDATE conversations SET group_order = ? WHERE id = ?",
                (index, conversation_id),
            )
    for conversation_id in ordered_conversation_ids:
        _notify_change(conversation_id)


def get_note(conversation_id: str) -> str:
    """Private per-session note-to-self, never sent to the agent. Returns ""
    (not None) when there is no note."""
    _ensure_db()
    with _conn() as conn:
        row = conn.execute("SELECT note FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
    return (row["note"] if row and row["note"] is not None else "")


def set_note(conversation_id: str, note: str) -> str:
    # Like pinning/archiving, editing the private note is a metadata
    # change, not conversation activity - don't touch updated_at.
    _ensure_db()
    convo = get_conversation(conversation_id)
    if convo is None:
        raise KeyError(conversation_id)
    with _conn() as conn:
        conn.execute(
            "UPDATE conversations SET note = ? WHERE id = ?",
            (note, conversation_id),
        )
    _notify_change(conversation_id)
    return note


def get_last_read(conversation_id: str) -> str | None:
    """Server-side read/unread tracking. Returns None if the session has
    never been marked read (the Web UI treats that as "unread")."""
    _ensure_db()
    with _conn() as conn:
        row = conn.execute("SELECT last_read_at FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
    return row["last_read_at"] if row else None


def mark_read(conversation_id: str, when: str | None = None) -> str | None:
    """Marks a conversation read up to `when` (defaults to its own current
    updated_at - i.e. "read up to the latest activity right now"). Like
    pin/archive/note, this is metadata, not conversation activity, so it
    does not touch updated_at itself. Broadcasts a change notification so
    every other open tab/device picks up the new read state immediately
    via the sessions SSE stream, instead of only the tab that clicked."""
    _ensure_db()
    convo = get_conversation(conversation_id)
    if convo is None:
        raise KeyError(conversation_id)
    read_at = when or convo["updated_at"]
    with _conn() as conn:
        conn.execute(
            "UPDATE conversations SET last_read_at = ? WHERE id = ?",
            (read_at, conversation_id),
        )
    _notify_change(conversation_id)
    return read_at


def get_bookmarked_message_ids(conversation_id: str) -> list[str]:
    """Message ids bookmarked/starred within this conversation,
    ordered oldest-first. Empty list (not None) if none/unknown conversation,
    so callers don't need a None-check."""
    _ensure_db()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT message_id FROM message_bookmarks WHERE conversation_id = ? ORDER BY created_at ASC, rowid ASC",
            (conversation_id,),
        ).fetchall()
    return [str(row["message_id"]) for row in rows]


def set_message_bookmarked(conversation_id: str, message_id: str, bookmarked: bool) -> bool:
    # Like pin/archive/note, bookmarking a message is a metadata change, not
    # conversation activity - don't touch the conversation's updated_at.
    _ensure_db()
    convo = get_conversation(conversation_id)
    if convo is None:
        raise KeyError(conversation_id)
    with _conn() as conn:
        if bookmarked:
            conn.execute(
                "INSERT OR IGNORE INTO message_bookmarks(conversation_id, message_id, created_at) VALUES (?, ?, ?)",
                (conversation_id, message_id, _now()),
            )
        else:
            conn.execute(
                "DELETE FROM message_bookmarks WHERE conversation_id = ? AND message_id = ?",
                (conversation_id, message_id),
            )
    _notify_change(conversation_id)
    return bookmarked


def set_title(conversation_id: str, title: str | None) -> str | None:
    convo = get_conversation(conversation_id)
    if convo is None:
        raise KeyError(conversation_id)
    cleaned = (title or "").strip()
    legacy_key = None if cleaned else get_primary_binding(conversation_id, SLACK_PLATFORM)
    with _json_write_lock:  # serialize load-modify-save between threads
        titles = _load_titles()
        if cleaned:
            titles[conversation_id] = cleaned
        else:
            titles.pop(conversation_id, None)
            if legacy_key:
                titles.pop(legacy_key, None)
            cleaned = None
        _save_titles(titles)
    _notify_change(conversation_id)
    return cleaned


def get_title(conversation_id: str) -> str | None:
    convo = get_conversation(conversation_id)
    if convo is None:
        return None
    title, custom = _title_for(conversation_id, get_primary_binding(conversation_id, SLACK_PLATFORM), None)
    return title if custom else None


def get_display_title(conversation_id: str) -> str:
    """Return the same title the Web UI session list shows for this
    conversation (custom title if one was set, else derived from its first
    message) - used e.g. by the 'continue in new session' backlink so it
    doesn't need to duplicate list_conversations()'s title-derivation logic
    itself. Raises KeyError if the conversation doesn't exist."""
    convo = get_conversation(conversation_id)
    if convo is None:
        raise KeyError(conversation_id)
    with _conn() as conn:
        first = conn.execute(
            "SELECT content FROM messages WHERE conversation_id = ? AND role IN ('user', 'system', 'assistant') ORDER BY seq ASC LIMIT 1",
            (conversation_id,),
        ).fetchone()
    slack_key = get_primary_binding(conversation_id, SLACK_PLATFORM)
    title, _custom = _title_for(conversation_id, slack_key, first["content"] if first else None)
    return title


def find_message_id_by_external_ref(conversation_id: str, external_ref: str) -> str | None:
    _ensure_db()
    with _conn() as conn:
        row = conn.execute(
            "SELECT id FROM messages WHERE conversation_id = ? AND external_ref = ?",
            (conversation_id, external_ref),
        ).fetchone()
    return str(row["id"]) if row else None


_UNSET_ACTIONS = object()


def record_message(
    conversation_id: str,
    role: str,
    content: str,
    message_id: str | None = None,
    *,
    external_ref: str | None = None,
    created_at: str | None = None,
    updated_at: str | None = None,
    actions: Any = _UNSET_ACTIONS,
) -> str:
    """``actions`` (optional): a list of ``{"id": "<action_id>", "label":
    "<button text>"}`` dicts the Web UI should render as inline buttons on
    this message (see ``messages.actions`` column migration above). Omitting
    this kwarg leaves whatever was previously stored untouched (same
    COALESCE-style "don't touch if not given" semantics as ``external_ref``)
    - pass ``actions=[]`` or ``actions=None`` explicitly to clear the
    buttons (e.g. once a button has been clicked/consumed)."""
    _ensure_db()
    now = _now()
    effective_updated_at = updated_at or now
    if message_id is None and external_ref:
        message_id = find_message_id_by_external_ref(conversation_id, external_ref)
    actions_provided = actions is not _UNSET_ACTIONS
    actions_json = json.dumps(actions) if actions_provided and actions else None
    with _conn() as conn:
        if message_id is not None:
            existing = conn.execute(
                "SELECT id, created_at, external_ref FROM messages WHERE id = ? AND conversation_id = ?",
                (message_id, conversation_id),
            ).fetchone()
            if existing is not None:
                if actions_provided:
                    conn.execute(
                        "UPDATE messages SET role = ?, content = ?, external_ref = COALESCE(?, external_ref), updated_at = ?, actions = ? WHERE id = ?",
                        (role, content, external_ref, effective_updated_at, actions_json, message_id),
                    )
                else:
                    conn.execute(
                        "UPDATE messages SET role = ?, content = ?, external_ref = COALESCE(?, external_ref), updated_at = ? WHERE id = ?",
                        (role, content, external_ref, effective_updated_at, message_id),
                    )
                _touch_conversation(conn, conversation_id, effective_updated_at)
                stored_id = str(existing["id"])
            else:
                message_id = None
        if message_id is None:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM messages WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            next_seq = int(row["max_seq"] or 0) + 1
            stored_id = str(uuid.uuid4())
            ts = created_at or now
            conn.execute(
                "INSERT INTO messages(id, conversation_id, role, content, seq, external_ref, created_at, updated_at, actions) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (stored_id, conversation_id, role, content, next_seq, external_ref, ts, effective_updated_at, actions_json),
            )
            _touch_conversation(conn, conversation_id, effective_updated_at)
            # Live activity (not a backfill with an explicit created_at) revives an archived session.
            if created_at is None and role in ("user", "assistant"):
                conn.execute("UPDATE conversations SET archived = 0 WHERE id = ? AND archived = 1", (conversation_id,))
    _notify_change(conversation_id)
    return stored_id


def get_messages(conversation_id: str) -> list[dict[str, Any]]:
    _ensure_db()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, role, content, seq, external_ref, created_at, updated_at, actions FROM messages WHERE conversation_id = ? ORDER BY seq ASC",
            (conversation_id,),
        ).fetchall()
    return [
        {
            "id": str(r["id"]),
            "role": str(r["role"]),
            "content": str(r["content"]),
            "seq": int(r["seq"]),
            "external_ref": r["external_ref"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "actions": json.loads(r["actions"]) if r["actions"] else [],
        }
        for r in rows
    ]


def record_attachment(
    message_id: str,
    conversation_id: str,
    *,
    kind: str,
    local_path: str,
    slack_file_id: str | None = None,
    filename: str | None = None,
    mimetype: str | None = None,
    size: int | None = None,
) -> str:
    """Record one captured file (currently always an image - see
    handlers.py::maybe_capture_images) against the message that carried it.
    Returns the new attachment id."""
    _ensure_db()
    attachment_id = str(uuid.uuid4())
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO message_attachments(
                id, message_id, conversation_id, kind, slack_file_id,
                filename, mimetype, size, local_path, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attachment_id, message_id, conversation_id, kind, slack_file_id,
                filename, mimetype, size, local_path, _now(),
            ),
        )
    _notify_change(conversation_id)
    return attachment_id


def get_attachments_for_message(message_id: str) -> list[dict[str, Any]]:
    _ensure_db()
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT id, message_id, conversation_id, kind, slack_file_id,
                   filename, mimetype, size, local_path, created_at
            FROM message_attachments WHERE message_id = ? ORDER BY created_at ASC
            """,
            (message_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_attachments_for_conversation(conversation_id: str) -> dict[str, list[dict[str, Any]]]:
    """Bulk fetch of every attachment in a conversation, grouped by
    message_id - used by the Web UI's session-detail endpoint so rendering
    N messages doesn't cost N queries."""
    _ensure_db()
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT id, message_id, conversation_id, kind, slack_file_id,
                   filename, mimetype, size, local_path, created_at
            FROM message_attachments WHERE conversation_id = ? ORDER BY created_at ASC
            """,
            (conversation_id,),
        ).fetchall()
    by_message: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_message.setdefault(str(r["message_id"]), []).append(dict(r))
    return by_message


def get_attachment(attachment_id: str, conversation_id: str) -> dict[str, Any] | None:
    """Look up one attachment, scoped to a conversation_id, so the serving
    route in web_server.py can't be used to read another conversation's
    cached file by guessing/enumerating attachment ids."""
    _ensure_db()
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT id, message_id, conversation_id, kind, slack_file_id,
                   filename, mimetype, size, local_path, created_at
            FROM message_attachments WHERE id = ? AND conversation_id = ?
            """,
            (attachment_id, conversation_id),
        ).fetchone()
    return dict(row) if row else None


def count_messages(conversation_id: str) -> int:
    _ensure_db()
    with _conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM messages WHERE conversation_id = ?", (conversation_id,)).fetchone()
    return int(row["c"] or 0) if row else 0


def search_messages(query: str, limit: int = 50) -> list[dict[str, Any]]:
    """Full-text-ish search across every conversation's message content
    (case-insensitive substring match — no FTS5 virtual table, this store
    is small enough that a plain `LIKE` scan is fast and avoids adding a
    parallel FTS index that has to stay in sync with `messages`).

    Returns one row per matching message with enough context to render a
    result list and jump straight to that conversation: conversation_id,
    the display title, a short snippet centered on the match (so long
    messages don't have to be shown in full), role, and created_at.
    Results are ordered most-recent-first. Empty/whitespace-only queries
    return an empty list rather than every message in the store.
    """
    query = (query or "").strip()
    if not query:
        return []
    _ensure_db()
    # Escape LIKE wildcards in the user's query itself so literal % / _
    # characters in a search term don't act as wildcards.
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like_pattern = f"%{escaped}%"
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT m.id, m.conversation_id, m.role, m.content, m.created_at, c.id AS conversation_id_for_title
            FROM messages m
            JOIN conversations c ON c.id = m.conversation_id
            WHERE m.content LIKE ? ESCAPE '\\'
            ORDER BY m.created_at DESC
            LIMIT ?
            """,
            (like_pattern, limit),
        ).fetchall()
        results: list[dict[str, Any]] = []
        for r in rows:
            conversation_id = str(r["conversation_id"])
            first = conn.execute(
                "SELECT content FROM messages WHERE conversation_id = ? AND role IN ('user', 'system', 'assistant') ORDER BY seq ASC LIMIT 1",
                (conversation_id,),
            ).fetchone()
            title, _ = _title_for(
                conversation_id,
                get_primary_binding(conversation_id, SLACK_PLATFORM),
                first["content"] if first else None,
            )
            results.append(
                {
                    "message_id": str(r["id"]),
                    "conversation_id": conversation_id,
                    "title": title,
                    "role": str(r["role"]),
                    "snippet": _search_snippet(str(r["content"]), query),
                    "created_at": r["created_at"],
                }
            )
    return results


def _search_snippet(content: str, query: str, radius: int = 60) -> str:
    """Return a short excerpt of `content` centered on the first
    case-insensitive occurrence of `query`, so a search result list can
    show relevant context instead of an arbitrarily truncated message
    start."""
    lower_content = content.lower()
    idx = lower_content.find(query.lower())
    if idx == -1:
        return _compact_preview(content, limit=2 * radius)
    start = max(0, idx - radius)
    end = min(len(content), idx + len(query) + radius)
    snippet = content[start:end].replace("\n", " ").strip()
    if start > 0:
        snippet = "…" + snippet
    if end < len(content):
        snippet = snippet + "…"
    return snippet


def list_conversations() -> list[dict[str, Any]]:
    _ensure_db()
    with _conn() as conn:
        convos = conn.execute(
            "SELECT id, copilot_session_id, pinned, pin_order, archived, created_at, updated_at, last_read_at, group_id, is_group_master, group_order, model, model_reason FROM conversations ORDER BY updated_at DESC, created_at DESC"
        ).fetchall()
        results: list[dict[str, Any]] = []
        for convo in convos:
            conversation_id = str(convo["id"])
            latest = conn.execute(
                "SELECT role, content, created_at, updated_at FROM messages WHERE conversation_id = ? ORDER BY seq DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
            first = conn.execute(
                "SELECT content FROM messages WHERE conversation_id = ? AND role IN ('user', 'system', 'assistant') ORDER BY seq ASC LIMIT 1",
                (conversation_id,),
            ).fetchone()
            turn_row = conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE conversation_id = ? AND role = 'user'",
                (conversation_id,),
            ).fetchone()
            slack_key = get_primary_binding(conversation_id, SLACK_PLATFORM)
            channel = None
            thread_ts = None
            if slack_key:
                parts = str(slack_key).split(":", 1)
                channel = parts[0]
                thread_ts = parts[1] if len(parts) > 1 else None
            title, custom_title = _title_for(conversation_id, slack_key, first["content"] if first else None)
            results.append(
                {
                    "conversation_id": conversation_id,
                    "session_id": convo["copilot_session_id"],
                    "channel": channel,
                    "thread_ts": thread_ts,
                    "title": title,
                    "custom_title": custom_title,
                    "preview": _compact_preview(latest["content"] if latest else ""),
                    "last_role": latest["role"] if latest else None,
                    "turn_count": int(turn_row["c"] or 0) if turn_row else 0,
                    "updated_at": convo["updated_at"],
                    "created_at": convo["created_at"],
                    "running": False,
                    "pinned": bool(convo["pinned"]),
                    "pin_order": convo["pin_order"],
                    "archived": bool(convo["archived"]),
                    "slack_key": slack_key,
                    "last_read_at": convo["last_read_at"],
                    "group_id": convo["group_id"],
                    "model": convo["model"],
                    "model_reason": convo["model_reason"],
                    "is_group_master": bool(convo["is_group_master"]),
                    "group_order": convo["group_order"],
                }
            )
    pinned = [r for r in results if r["pinned"] and not r["archived"]]
    unpinned = [r for r in results if not r["pinned"] and not r["archived"]]
    archived = [r for r in results if r["archived"]]
    # Pinned order is a stable, user-controlled manual order (set via pin
    # action / drag-and-drop reorder), independent of conversation activity -
    # do NOT sort pinned items by updated_at, or the list reshuffles every
    # time a pinned conversation gets a new message.
    pinned.sort(key=lambda r: (r["pin_order"] if r["pin_order"] is not None else 0))
    unpinned.sort(key=lambda r: (r["updated_at"] or "", r["conversation_id"]), reverse=True)
    # Archived sessions always sort after active ones (pinned or not) - the
    # Web UI hides them behind a toggle by default, but when shown they
    # should read as a separate "put away" tail, not interleaved by recency
    # with sessions still in active use.
    archived.sort(key=lambda r: (r["updated_at"] or "", r["conversation_id"]), reverse=True)
    return pinned + unpinned + archived


def find_stuck_status_conversations() -> list[dict[str, Any]]:
    """Return every conversation whose most recent message is still a
    'status' placeholder (the ":thinking_face: Working..." bubble) — i.e. a
    turn that was never finalized (no 'assistant'/'system' follow-up
    recorded). This is a DB-level, always-durable defense-in-depth check
    used by _resume_interrupted_turns() alongside active_status.json: if a
    process is killed in the narrow window after clearing an
    active_status.json entry but before persisting the final response (or
    if any other bug ever drops that entry), the conversation would
    otherwise be stuck forever with no way for the restart-recovery pass to
    notice it. Because message_store is the durable source of truth (unlike
    the flat active_status.json file), this check catches it regardless."""
    _ensure_db()
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT c.id AS conversation_id, m.id AS message_id, m.external_ref AS external_ref
            FROM conversations c
            JOIN messages m ON m.conversation_id = c.id
            WHERE m.role = 'status'
              AND m.seq = (SELECT MAX(seq) FROM messages WHERE conversation_id = c.id)
            """
        ).fetchall()
    results: list[dict[str, Any]] = []
    for r in rows:
        slack_key = get_primary_binding(str(r["conversation_id"]), SLACK_PLATFORM)
        channel = None
        thread_ts = None
        if slack_key:
            parts = str(slack_key).split(":", 1)
            channel = parts[0]
            thread_ts = parts[1] if len(parts) > 1 else None
        # external_ref for a Slack-backed status message is "slack:<channel>:<ts>"
        # (see slack_external_ref) — this is the *message's own* ts, which may
        # differ from the thread root ts above; needed to chat_update the
        # exact stuck bubble rather than guessing it's the thread root.
        status_channel = None
        status_ts = None
        external_ref = r["external_ref"]
        if external_ref and str(external_ref).startswith("slack:"):
            ref_parts = str(external_ref).split(":", 2)
            if len(ref_parts) == 3:
                status_channel = ref_parts[1]
                status_ts = ref_parts[2]
        results.append(
            {
                "conversation_id": str(r["conversation_id"]),
                "message_id": str(r["message_id"]),
                "channel": channel,
                "thread_ts": thread_ts,
                "status_channel": status_channel,
                "status_ts": status_ts,
            }
        )
    return results


def find_known_dm_channel() -> str | None:
    _ensure_db()
    for convo in list_conversations():
        slack_binding = convo.get("slack_key")  # derived from bindings by list_conversations
        if not slack_binding:
            continue
        channel = slack_binding.split(":", 1)[0]
        if channel.startswith("D"):
            return channel
    return None


# ---------------------------------------------------------------------------
# Session groups ("workstreams")
#
# A group is a user-titled bucket of conversations (no nesting, a session is
# in at most one group, "no group" = Ungrouped). One member may be flagged
# the group's master (shown first in the UI). Archiving a group cascades to
# its sessions; deleting a group only unassigns them. None of these touch
# conversations.updated_at - grouping is metadata, not conversation activity,
# so it must not reshuffle the most-recent-first session list.
# ---------------------------------------------------------------------------

GROUP_COLORS = ["#4f8cff", "#e5734b", "#3fb68b", "#b073e6", "#e0b43c", "#d9578f", "#4cb5c9", "#8a9a5b"]


def _group_row(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "color": row["color"],
        "emoji": row["emoji"],
        "description": row["description"],
        "archived": bool(row["archived"]),
        "sort_order": row["sort_order"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _group_member_ids(conn, group_id: str) -> None:
    ids = [r["id"] for r in conn.execute("SELECT id FROM conversations WHERE group_id = ?", (group_id,)).fetchall()]
    return ids


def create_group(title: str, color: str | None = None, emoji: str | None = None, description: str | None = None) -> dict[str, Any]:
    cleaned = (title or "").strip()
    if not cleaned:
        raise ValueError("group title must not be empty")
    _ensure_db()
    now = _now()
    with _conn() as conn:
        if not color:
            n = conn.execute("SELECT COUNT(*) AS c FROM session_groups").fetchone()["c"]
            color = GROUP_COLORS[n % len(GROUP_COLORS)]
        group_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO session_groups(id, title, color, emoji, description, archived, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
            (group_id, cleaned, color, (emoji or None), (description or None), now, now),
        )
        row = conn.execute("SELECT * FROM session_groups WHERE id = ?", (group_id,)).fetchone()
        result = _group_row(row)
    _notify_change("")
    return result


def set_groups_order(ordered_group_ids: list[str]) -> None:
    """Persist the manual order of groups (index in the given list)."""
    _ensure_db()
    with _conn() as conn:
        for index, gid in enumerate(ordered_group_ids):
            conn.execute("UPDATE session_groups SET sort_order = ? WHERE id = ?", (index, gid))
    _notify_change("")


def get_group(group_id: str) -> dict[str, Any] | None:
    _ensure_db()
    with _conn() as conn:
        row = conn.execute("SELECT * FROM session_groups WHERE id = ?", (group_id,)).fetchone()
    return _group_row(row) if row else None


def list_groups(include_archived: bool = False) -> list[dict[str, Any]]:
    """Groups with member counts (``session_count`` = non-archived members,
    ``total_count`` = all members) and the ``master_id`` if one is set."""
    _ensure_db()
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM session_groups ORDER BY sort_order IS NULL, sort_order ASC, created_at ASC").fetchall()
        out = []
        for row in rows:
            g = _group_row(row)
            if g["archived"] and not include_archived:
                continue
            counts = conn.execute(
                "SELECT COUNT(*) AS total, SUM(CASE WHEN archived = 0 THEN 1 ELSE 0 END) AS active FROM conversations WHERE group_id = ?",
                (g["id"],),
            ).fetchone()
            master = conn.execute(
                "SELECT id FROM conversations WHERE group_id = ? AND is_group_master = 1 LIMIT 1", (g["id"],)
            ).fetchone()
            g["total_count"] = int(counts["total"] or 0)
            g["session_count"] = int(counts["active"] or 0)
            g["master_id"] = master["id"] if master else None
            out.append(g)
    return out


def update_group(group_id: str, *, title: str | None = None, color: str | None = None, emoji: str | None = None, description: str | None = None) -> dict[str, Any]:
    _ensure_db()
    sets, vals = [], []
    if title is not None:
        if not title.strip():
            raise ValueError("group title must not be empty")
        sets.append("title = ?"); vals.append(title.strip())
    if color is not None:
        sets.append("color = ?"); vals.append(color or None)
    if emoji is not None:
        sets.append("emoji = ?"); vals.append(emoji or None)
    if description is not None:
        sets.append("description = ?"); vals.append(description or None)
    with _conn() as conn:
        if conn.execute("SELECT 1 FROM session_groups WHERE id = ?", (group_id,)).fetchone() is None:
            raise KeyError(group_id)
        if sets:
            sets.append("updated_at = ?"); vals.append(_now())
            conn.execute(f"UPDATE session_groups SET {', '.join(sets)} WHERE id = ?", (*vals, group_id))
        row = conn.execute("SELECT * FROM session_groups WHERE id = ?", (group_id,)).fetchone()
        members = _group_member_ids(conn, group_id)
        result = _group_row(row)
    for cid in members or [""]:
        _notify_change(cid)
    return result


def delete_group(group_id: str) -> int:
    """Delete a group; its sessions become ungrouped (NOT archived/deleted).
    Returns how many sessions were unassigned."""
    _ensure_db()
    with _conn() as conn:
        members = _group_member_ids(conn, group_id)
        conn.execute("UPDATE conversations SET group_id = NULL, is_group_master = 0 WHERE group_id = ?", (group_id,))
        conn.execute("DELETE FROM session_groups WHERE id = ?", (group_id,))
    for cid in members or [""]:
        _notify_change(cid)
    return len(members)


def set_group_archived(group_id: str, archived: bool) -> int:
    """Archive/unarchive a group AND all its member sessions (cascade).
    Returns the number of sessions affected."""
    _ensure_db()
    flag = 1 if archived else 0
    with _conn() as conn:
        if conn.execute("SELECT 1 FROM session_groups WHERE id = ?", (group_id,)).fetchone() is None:
            raise KeyError(group_id)
        members = _group_member_ids(conn, group_id)
        conn.execute("UPDATE session_groups SET archived = ?, updated_at = ? WHERE id = ?", (flag, _now(), group_id))
        conn.execute("UPDATE conversations SET archived = ? WHERE group_id = ?", (flag, group_id))
    for cid in members or [""]:
        _notify_change(cid)
    return len(members)


def mark_group_read(group_id: str) -> int:
    """Marks every unread member session of a group read (last_read_at =
    its own updated_at). Returns the number of sessions changed."""
    _ensure_db()
    with _conn() as conn:
        if conn.execute("SELECT 1 FROM session_groups WHERE id = ?", (group_id,)).fetchone() is None:
            raise KeyError(group_id)
        rows = conn.execute(
            "SELECT id FROM conversations WHERE group_id = ? AND updated_at IS NOT NULL "
            "AND (last_read_at IS NULL OR last_read_at < updated_at)",
            (group_id,),
        ).fetchall()
        conn.execute(
            "UPDATE conversations SET last_read_at = updated_at WHERE group_id = ? AND updated_at IS NOT NULL "
            "AND (last_read_at IS NULL OR last_read_at < updated_at)",
            (group_id,),
        )
    for r in rows:
        _notify_change(r["id"])
    return len(rows)


def assign_session_group(conversation_id: str, group_id: str | None) -> None:
    """Put a session in a group (or ungroup it with ``None``). Moving a
    session always clears its master flag - it can't stay master of a group
    it has left."""
    _ensure_db()
    with _conn() as conn:
        if conn.execute("SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)).fetchone() is None:
            raise KeyError(conversation_id)
        if group_id is not None and conn.execute("SELECT 1 FROM session_groups WHERE id = ?", (group_id,)).fetchone() is None:
            raise KeyError(group_id)
        conn.execute(
            "UPDATE conversations SET group_id = ?, is_group_master = 0 WHERE id = ?",
            (group_id, conversation_id),
        )
    _notify_change(conversation_id)


def get_session_group_id(conversation_id: str) -> str | None:
    _ensure_db()
    with _conn() as conn:
        row = conn.execute("SELECT group_id FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
    return row["group_id"] if row else None


def set_group_master(group_id: str, conversation_id: str | None) -> None:
    """Make ``conversation_id`` (which must already be a member) the group's
    only master; ``None`` clears the master."""
    _ensure_db()
    with _conn() as conn:
        if conn.execute("SELECT 1 FROM session_groups WHERE id = ?", (group_id,)).fetchone() is None:
            raise KeyError(group_id)
        if conversation_id is not None:
            row = conn.execute("SELECT group_id FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
            if row is None:
                raise KeyError(conversation_id)
            if row["group_id"] != group_id:
                raise ValueError("session is not a member of this group - assign it to the group first")
        members = _group_member_ids(conn, group_id)
        conn.execute("UPDATE conversations SET is_group_master = 0 WHERE group_id = ?", (group_id,))
        if conversation_id is not None:
            conn.execute("UPDATE conversations SET is_group_master = 1 WHERE id = ?", (conversation_id,))
    for cid in members or [""]:
        _notify_change(cid)


def inherit_group_from_parent(child_id: str, parent_id: str) -> str | None:
    """Put ``child_id`` in ``parent_id``'s group, auto-creating one (titled
    after the parent, parent as master) if the parent has none. Returns the
    group id. Used by session_create(inherit_group=True)."""
    group_id = get_session_group_id(parent_id)
    if group_id is None:
        group = create_group(get_display_title(parent_id))
        group_id = group["id"]
        assign_session_group(parent_id, group_id)
        set_group_master(group_id, parent_id)
    assign_session_group(child_id, group_id)
    return group_id


# ---- Messages inbox -------------------------------------------------------
# Sessions whose LAST conversational message (status rows ignored) is from the
# assistant and contains an ```ask / ```ideas fence the user has not yet
# dismissed. The widgets' own parsers decide client-side whether the fences are
# valid, so this only pre-filters (cheap, one query, no per-conversation N+1).

_INBOX_FENCE_RE = re.compile(r"^```(?:ask|ideas|actions)\b", re.MULTILINE)


def inbox_candidates() -> list[dict[str, Any]]:
    _ensure_db()
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT c.id AS conversation_id, c.group_id, c.updated_at, m.id AS message_id,
                   m.content AS text, m.created_at AS message_at
            FROM conversations c
            JOIN messages m ON m.conversation_id = c.id
             AND m.seq = (SELECT MAX(seq) FROM messages
                          WHERE conversation_id = c.id AND role IN ('user', 'assistant', 'system'))
            WHERE c.archived = 0 AND m.role = 'assistant'
              AND (m.content LIKE '%```ask%' OR m.content LIKE '%```ideas%' OR m.content LIKE '%```actions%')
              AND NOT EXISTS (SELECT 1 FROM inbox_dismissed d
                              WHERE d.conversation_id = c.id AND d.message_id = m.id)
            ORDER BY m.created_at DESC
            """
        ).fetchall()
    out = []
    for r in rows:
        if not _INBOX_FENCE_RE.search(r["text"] or ""):
            continue
        out.append(dict(r))
    return out


def dismiss_inbox(conversation_id: str, message_id: str) -> None:
    _ensure_db()
    with _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO inbox_dismissed (conversation_id, message_id, created_at) VALUES (?, ?, ?)",
            (conversation_id, message_id, _now()),
        )
    _notify_change(conversation_id)


def undismiss_inbox(conversation_id: str, message_id: str | None = None) -> None:
    _ensure_db()
    with _conn() as conn:
        if message_id:
            conn.execute("DELETE FROM inbox_dismissed WHERE conversation_id = ? AND message_id = ?", (conversation_id, message_id))
        else:
            conn.execute("DELETE FROM inbox_dismissed WHERE conversation_id = ?", (conversation_id,))
    _notify_change(conversation_id)
