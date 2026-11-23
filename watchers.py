"""Background watcher polling: scheduling, persistence and delivery for every
watch type, plus new-watch announcements and the WATCHERS.md snapshot writer.

Integration watch types come from the integrations registry
(integrations/runtime.py -> drivers/<id>); this module holds no
integration-specific code. Only the builtin non-integration
types (config ``watchers.builtin_types``: time, self_restart, session_turn,
script) are implemented here.

Depends on the live Slack ``app``/``client`` and the core ``_handle()`` turn
engine, both of which are only fully constructed inside dop_agent.py — to
avoid a circular import, this module uses late binding via
``configure(app, handle_fn)``, which dop_agent.py calls once at startup.

Gotchas:
  - The ``self_restart`` watch type doesn't just notify, it re-injects a
    resume prompt into the thread. It must be marked done before attempting
    resume, otherwise a crash during resume would re-fire it forever.
  - Token savings (documented in README "Watcher token savings"): script-only
    watches (meta.script, watch_scripts.py, `_fire_script_watch`).
"""

import asyncio
import contextvars
import json
import os
import re
import sqlite3
import threading
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from croniter import croniter

import adapters
from adapters import SlackAdapter
from bot_config import (
    ALLOWED_USER_ID,
    WATCH_POLL_TIMEOUT,
    WATCHER_POLL_INTERVAL,
    WATCHERS_MD_PATH,
    log,
)
import message_kinds
import watch_scripts
import config
import message_store
from copilot_session import (
    _conversation_id_for,
    add_turn_complete_listener,
    get_convo,
    get_convo_if_exists,
)

# Late-bound by configure() — see module docstring. None until dop_agent.py
# finishes its own module-level setup and calls configure(app, _handle).
_app = None
_handle_fn = None


def configure(app, handle_fn) -> None:
    """Wire this module up to the live Slack app and the core turn-handling
    function. Must be called once, after both exist, before watcher_loop()
    or any _poll_*_watch() function runs."""
    global _app, _handle_fn
    _app = app
    _handle_fn = handle_fn
    add_turn_complete_listener(_on_turn_complete)


def _slack_adapter() -> SlackAdapter:
    """Stateless wrapper around the configured app's Slack client, built on
    demand so it always reflects the current ``_app`` (tests reconfigure it)."""
    return adapters.REGISTRY.slack_for(_app.client)


def _registry() -> "adapters.AdapterRegistry":
    """The process-wide adapter registry, with the Slack adapter refreshed
    from the currently configured app (no-op before configure())."""
    if _app is not None:
        adapters.REGISTRY.register(_slack_adapter())
    return adapters.REGISTRY


def _delivery_target(conversation_id: str, *, strict: bool = False):
    """(adapter, thread_key) for an out-of-band message to this conversation,
    resolved from its bindings via the registry — never inferred from
    whether a Slack channel string is truthy."""
    return _registry().delivery_for(conversation_id, strict=strict)


def _fmt_watch_ts(iso_str: str | None) -> str:
    """Compact display for a watch's created_at/updated_at (both stored as
    full ISO-8601 with microseconds) — e.g. '2026-08-06 15:52 UTC'."""
    if not iso_str:
        return "n/a"
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return iso_str


DB_PATH = Path(__file__).parent / "watchers.db"
BOOT_ID_FILE = Path(__file__).parent / ".boot_id"

BUILTIN_EMOJI = {"self_restart": ("🔁", ":repeat:"), "time": ("⏰", ":alarm_clock:"),
                 "session_turn": ("🔄", ":arrows_counterclockwise:"), "script": ("📜", ":scroll:")}
# "broken": key/format is permanently invalid (not a transient fetch error) —
# set via mark_broken(), never polled again until fixed + re-activated.
VALID_STATUSES = ("active", "paused", "done", "broken")

# A session_turn watch waiting this long with no fire gets exactly one
# "are you still waiting on purpose?" notice (see _maybe_notify_stale_
# session_turn_watch) — not auto-closed, since a legitimately long-running
# wait (e.g. watching a multi-day migration thread) is a real use case, not
# a bug.
SESSION_TURN_STALE_AFTER = timedelta(days=config.value("watchers.session_turn.stale_after_days"))

# Default max_chain for a self-targeting session_turn watch (see add_watch()):
# nothing external bounds how often a self-subscription can auto-resume.
SESSION_TURN_DEFAULT_MAX_CHAIN = config.value("watchers.session_turn.default_max_chain")

# `time` watch key formats — see parse_time_key() below for full semantics.
_TIME_IN_RE = re.compile(r"^in:(\d+)\s*(m|h|d)$", re.IGNORECASE)
_TIME_AT_RE = re.compile(r"^at:(\d{4}-\d{2}-\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?$")
_TIME_CRON_DAILY_RE = re.compile(r"^cron:daily\s+(\d{2}):(\d{2})$", re.IGNORECASE)
_TIME_CRON_WEEKLY_RE = re.compile(
    r"^cron:(mon|tue|wed|thu|fri|sat|sun)\s+(\d{2}):(\d{2})$", re.IGNORECASE
)
_TIME_CRON_FULL_RE = re.compile(r"^cron:\s*(\S+(?:\s+\S+){4})$", re.IGNORECASE)
_TIME_DOW_INDEX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _integrations():
    from integrations.runtime import get_registry
    return get_registry()


def builtin_types() -> tuple[str, ...]:
    return tuple(config.value("watchers.builtin_types") or ())


def valid_types() -> tuple[str, ...]:
    """Every watch type currently accepted: enabled integrations' types (registry) + builtins."""
    reg_types = sorted(_integrations().watch_types())
    return tuple(reg_types) + tuple(t for t in builtin_types() if t not in reg_types)


def _watch_extra(type_: str, integration: str | None = None) -> dict:
    meta = _integrations().watch_type_meta(type_, integration)
    return (meta or {}).get("extra") or {}


def _key_error(type_: str, key: str | None, integration: str | None = None) -> str:
    meta = _integrations().watch_type_meta(type_, integration) or {}
    tpl = (meta.get("extra") or {}).get("key_error")
    if tpl:
        return tpl.format(key=key)
    return f"{type_} watch key must match {meta.get('key_match', '?')}, got {key!r}"





def parse_time_key(key: str) -> dict:
    """Parse a `time` watch's `key` into a dict describing its schedule.

    Supported forms (all times are the bot machine's local system time —
    deliberately no explicit timezone handling, since this is a personal
    single-user bot and "every Monday at 9" means the user's own local time):
      "in:<N>m" / "in:<N>h" / "in:<N>d"  -> one-shot, fires N minutes/hours/
                                             days from registration time.
      "at:<YYYY-MM-DD>T<HH:MM>[:SS]"     -> one-shot, fires at this exact
                                             local date/time (space instead
                                             of "T" also accepted).
      "cron:<5-field cron expr>"         -> recurring, per the cron expression
                                             (minute hour dom month dow).
      "cron:daily <HH:MM>"               -> recurring, fires every day at
                                             this local time.
      "cron:<mon|tue|wed|thu|fri|sat|sun> <HH:MM>" -> recurring, fires every
                                             week on this weekday at this
                                             local time.

    Returns one of:
      {"kind": "in", "amount": int, "unit": "m"|"h"|"d"}
      {"kind": "at", "datetime": datetime}
      {"kind": "cron", "expr": str}
      {"kind": "cron_daily", "hour": int, "minute": int}
      {"kind": "cron_weekly", "dow": str, "hour": int, "minute": int}

    Raises ValueError with an actionable message on anything else.
    """
    key = key.strip()
    m = _TIME_IN_RE.match(key)
    if m:
        return {"kind": "in", "amount": int(m.group(1)), "unit": m.group(2).lower()}
    m = _TIME_AT_RE.match(key)
    if m:
        date_str, hour, minute, second = m.group(1), m.group(2), m.group(3), m.group(4)
        try:
            dt = datetime(
                *map(int, date_str.split("-")), int(hour), int(minute), int(second or 0)
            )
        except ValueError as exc:
            raise ValueError(f"time watch 'at:' key has an invalid date/time: {key!r} ({exc})") from exc
        return {"kind": "at", "datetime": dt}
    m = _TIME_CRON_DAILY_RE.match(key)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError(f"time watch 'cron:daily' key has an invalid time: {key!r}")
        return {"kind": "cron_daily", "hour": hour, "minute": minute}
    m = _TIME_CRON_WEEKLY_RE.match(key)
    if m:
        dow, hour, minute = m.group(1).lower(), int(m.group(2)), int(m.group(3))
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError(f"time watch 'cron:{dow}' key has an invalid time: {key!r}")
        return {"kind": "cron_weekly", "dow": dow, "hour": hour, "minute": minute}
    m = _TIME_CRON_FULL_RE.match(key)
    if m:
        expr = " ".join(m.group(1).split())
        if not croniter.is_valid(expr):
            raise ValueError(f"time watch 'cron:' key has an invalid 5-field cron expression: {key!r} "
                             "(fields: minute hour day-of-month month day-of-week, local time)")
        return {"kind": "cron", "expr": expr}
    raise ValueError(
        f"time watch key must be one of 'in:<N>m|h|d' (e.g. 'in:1h'), "
        f"'at:<YYYY-MM-DDTHH:MM>', 'cron:<5-field cron expr>' (e.g. 'cron:*/10 * * * *'), "
        f"'cron:daily <HH:MM>', or 'cron:<mon|tue|wed|thu|fri|sat|sun> <HH:MM>', got {key!r}"
    )


def compute_next_fire(parsed: dict, now: datetime | None = None) -> datetime:
    """Compute the next (local, naive) datetime a `time` watch should fire,
    given its already-parsed key (see parse_time_key) and the current time.
    Used at registration to seed meta.next_fire_at."""
    if now is None:
        now = datetime.now()
    kind = parsed["kind"]
    if kind == "in":
        delta = {"m": timedelta(minutes=1), "h": timedelta(hours=1), "d": timedelta(days=1)}[parsed["unit"]]
        return now + delta * parsed["amount"]
    if kind == "at":
        return parsed["datetime"]
    if kind == "cron":
        return croniter(parsed["expr"], now).get_next(datetime)
    if kind == "cron_daily":
        candidate = now.replace(hour=parsed["hour"], minute=parsed["minute"], second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate
    if kind == "cron_weekly":
        target_dow = _TIME_DOW_INDEX[parsed["dow"]]
        candidate = now.replace(hour=parsed["hour"], minute=parsed["minute"], second=0, microsecond=0)
        days_ahead = (target_dow - candidate.weekday()) % 7
        candidate += timedelta(days=days_ahead)
        if candidate <= now:
            candidate += timedelta(days=7)
        return candidate
    raise ValueError(f"Unknown parsed time watch kind {kind!r}")


def is_recurring_time_kind(kind: str) -> bool:
    return kind in ("cron", "cron_daily", "cron_weekly")


def advance_recurring(parsed: dict, previous_fire: datetime, now: datetime | None = None) -> datetime:
    """Compute the next occurrence after a recurring `time` watch has just
    fired at `previous_fire`. Advances by exactly one day/week from the
    scheduled time (not "now") to avoid drift, then fast-forwards past `now`
    in case the bot was offline across one or more occurrences — so a
    daily/weekly watch that missed several ticks fires once for the next
    still-upcoming slot instead of spamming one message per missed
    occurrence. Note: naive local-time arithmetic does not account for DST
    transitions — acceptable for this personal-use bot's granularity."""
    if now is None:
        now = datetime.now()
    if parsed["kind"] == "cron":
        return croniter(parsed["expr"], max(previous_fire, now)).get_next(datetime)
    step = timedelta(days=1) if parsed["kind"] == "cron_daily" else timedelta(days=7)
    next_fire = previous_fire + step
    while next_fire <= now:
        next_fire += step
    return next_fire


def validate_key(type_: str, key: str | None, integration: str | None = None) -> None:
    """Raise ValueError with a clear, actionable message if `key` doesn't
    match the format the poller for `type_` expects. Called from add_watch()
    so a malformed key fails at registration time instead of being
    silently accepted and then skipped forever by the poller. Integration types are validated against the
    registry's key_match (messages from the driver manifest)."""
    if type_ == "self_restart":
        return  # no key used
    stripped = (key or "").strip()
    if type_ in _integrations().watch_types():
        extra = _watch_extra(type_, integration)
        if not stripped:
            raise ValueError(extra.get("key_required") or f"{type_} watch requires --key")
        if _integrations().resolve_watch(type_, stripped, integration) is None:
            if integration and _integrations().resolve_watch(type_, stripped) is not None:
                raise ValueError(f"integration {integration!r} is not enabled or does not provide the "
                                 f"{type_!r} watch type")
            raise ValueError(_key_error(type_, key, integration))
    elif type_ == "time":
        if not stripped:
            raise ValueError(
                "time watch requires --key: 'in:<N>m|h|d', 'at:<YYYY-MM-DDTHH:MM>', "
                "'cron:daily <HH:MM>', or 'cron:<mon|tue|wed|thu|fri|sat|sun> <HH:MM>'"
            )
        parse_time_key(stripped)  # raises ValueError itself if malformed
    elif type_ == "script":
        if not stripped:
            raise ValueError(
                "script watch requires --key: the schedule, e.g. 'cron:*/10 * * * *', "
                "'in:30m' or 'at:<YYYY-MM-DDTHH:MM>'"
            )
        parse_time_key(stripped)
    elif type_ == "session_turn":
        if not stripped:
            raise ValueError(
                "session_turn watch requires --key: a conversation_id to watch, or "
                "'self' to watch this own conversation's own future turns"
            )
    elif type_ not in builtin_types():
        raise ValueError(_unknown_type_message(type_))


def _unknown_type_message(type_: str) -> str:
    return (f"Unknown watch type {type_!r}, expected one of {valid_types()} "
            f"(integration types need an enabled integration in config.yaml `integrations:`)")


def check_watch_resource_exists(type_: str, key: str | None, integration: str | None = None) -> None:
    """Best-effort registration-time existence check for integration watch types:
    one live driver fetch right after validate_key() passes. Only a CONFIRMED
    not-found (drivers.base.NotFound) blocks registration, using the manifest's
    `not_found_error` message; any other failure (network, auth, 5xx, client
    unavailable, kinds without a single resource such as pipeline '@ref') is
    logged and swallowed so a transient outage never makes watch_add flaky."""
    from drivers.base import NotFound, UnsupportedOperation
    key = (key or "").strip()
    reg = _integrations()
    if not key or type_ not in reg.watch_types():
        return
    r = reg.resolve_watch(type_, key, integration)
    if r is None:
        return  # validate_key() already rejects this
    it, kind, ident = r
    try:
        reg.fetch(it, kind, ident)
    except UnsupportedOperation:
        return
    except NotFound:
        tpl = ((reg.watch_type_meta(type_, it.name) or {}).get("extra") or {}).get("not_found_error")
        if tpl:
            raise ValueError(tpl.format(**ident))
        raise ValueError(f"{type_} watch target {key!r} not found")
    except Exception as exc:
        log.warning("[watcher] existence check for %s watch %r skipped/non-fatal, allowing "
                    "registration through: %s", type_, key, exc)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    """Per-thread cached connection (opening one per call — WAL open, schema
    read, PRAGMAs — is costly, especially with antivirus scanning file opens)."""
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
        conn.execute("PRAGMA busy_timeout=5000")
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
            CREATE TABLE IF NOT EXISTS watches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                key TEXT,
                label TEXT,
                channel TEXT NOT NULL,
                thread_ts TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                last_state TEXT,
                meta TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        # ``conversation_id`` (the bot-side conversation UUID, which every
        # conversation has — Slack-bound or Web-UI-only) is the identity for
        # lookup/scoping; ``channel``/``thread_ts`` are only a delivery/display
        # cache, resolved fresh from the conversation's current Slack binding
        # at announce/fire time (see _resolve_delivery()).
        cols = {row[1] for row in conn.execute("PRAGMA table_info(watches)")}
        if "conversation_id" not in cols:
            conn.execute("ALTER TABLE watches ADD COLUMN conversation_id TEXT")
        # Script-only time watches keep their "changed"/failure-dedup state
        # here (see watch_scripts.py).
        if "script_state" not in cols:
            conn.execute("ALTER TABLE watches ADD COLUMN script_state TEXT")
    _backfill_conversation_ids()


def _backfill_conversation_ids() -> None:
    """Fill watches.conversation_id for rows missing it, resolving each
    (channel, thread_ts) pair to its bot-side conversation UUID via the same
    path Slack turns use (_conversation_id_for ->
    message_store.get_or_create_bound_conversation). No-op when no row is
    missing it."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, channel, thread_ts FROM watches WHERE conversation_id IS NULL"
        ).fetchall()
    if not rows:
        return
    for row in rows:
        try:
            conv_id = _conversation_id_for(row["channel"], row["thread_ts"])
        except Exception:
            log.exception("[watcher] could not backfill conversation_id for watch #%s", row["id"])
            continue
        with _conn() as conn:
            conn.execute(
                "UPDATE watches SET conversation_id = ? WHERE id = ?",
                (conv_id, row["id"]),
            )


# ---------------------------------------------------------------------------
# Immediate announcement (runs at `add` time, standalone from the bot daemon —
# so a watch gets announced even if it's registered seconds before a
# self_restart kills the running process, before the next watcher_loop tick).
# ---------------------------------------------------------------------------

def _fmt_ts_human(iso_str: str | None) -> str:
    if not iso_str:
        return "unknown"
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return iso_str


def _fmt_local_ts_human(iso_str: str | None) -> str:
    """Like _fmt_ts_human but for naive local-time strings (used by `time`
    watches — see parse_time_key's docstring for why these aren't UTC)."""
    if not iso_str:
        return "unknown"
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%Y-%m-%d %H:%M local")
    except Exception:
        return iso_str


def _fmt_timedelta_human(delta: timedelta) -> str:
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        return "overdue"
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


def _type_emoji(type_: str, slack: bool = False) -> str | None:
    if type_ in BUILTIN_EMOJI:
        return BUILTIN_EMOJI[type_][1 if slack else 0]
    meta = _integrations().watch_type_meta(type_) or {}
    return (meta.get("slack_emoji") if slack else meta.get("emoji")) or None


def _format_watch_announcement(watch_id: int, type_: str, key: str | None,
                                label: str | None, created_at: str,
                                meta: dict | None = None) -> str:
    emoji = _type_emoji(type_) or "👀"
    lines = [f"{emoji} *New watcher registered* — #{watch_id} `{type_}`"]
    if key:
        lines.append(f"• Target: `{key}`")
    if label:
        lines.append(f"• {label}")
    lines.append(f"• Added: {_fmt_ts_human(created_at)}")
    if type_ == "script" and meta:
        lines.append(f"• Script: `{meta.get('script')}` {' '.join(meta.get('args') or [])}".rstrip())
    if type_ in ("time", "script") and meta and meta.get("next_fire_at"):
        next_fire_str = meta["next_fire_at"]
        try:
            next_fire = datetime.fromisoformat(next_fire_str)
            delta_str = _fmt_timedelta_human(next_fire - datetime.now())
            lines.append(f"• Next fire: {_fmt_local_ts_human(next_fire_str)} (in {delta_str})")
        except Exception:
            lines.append(f"• Next fire: {next_fire_str}")
        if meta.get("recurring"):
            lines.append("• Recurring: fires again automatically after each occurrence.")
    if type_ == "self_restart":
        lines.append("_I'll resume this thread automatically right after the restart completes._")
    else:
        lines.append(
            "_I'll resume this thread automatically when it fires. "
            "Use the `watch_pause` or `watch_remove` MCP tool to stop it._"
        )
    return "\n".join(lines)


def _resolve_delivery(conversation_id: str) -> tuple[str, str | None]:
    """Resolve a conversation's *current* Slack binding (if any); the source
    of truth is message_store's conversation_bindings table, not the watches
    row. Returns ("", None) for a Web-UI-only conversation with no bound
    Slack thread; callers must treat an empty channel as "deliver via
    WebAdapter straight into the conversation", not as an error."""
    try:
        slack_key = message_store.get_primary_binding(conversation_id, message_store.SLACK_PLATFORM)
    except Exception:
        log.exception("[watcher] could not resolve Slack binding for conversation %s", conversation_id)
        return "", None
    if not slack_key:
        return "", None
    channel, _, thread_ts = slack_key.partition(":")
    return channel, (thread_ts or None)


def _post_watch_announcement(watch_id: int, type_: str, key: str | None, label: str | None,
                              conversation_id: str, channel: str, thread_ts: str | None,
                              created_at: str, meta: dict | None = None) -> bool:
    """Post the "new watcher registered" message immediately, independent of
    whether the bot daemon is up and not routed through watcher_loop. Delivers via
    the adapter the conversation's binding resolves to (registry), so any
    platform works. Returns True only if it was delivered synchronously; on
    failure, no live adapter, or when scheduled on the running loop it
    returns False and the bot's tick-based announce-retry covers it (the
    background task claims ``meta.announced`` itself on success).
    """
    text = _format_watch_announcement(watch_id, type_, key, label, created_at, meta)
    try:
        target = _delivery_target(conversation_id, strict=True)
    except Exception as exc:
        print(f"WARNING: could not resolve delivery for watch announcement: {exc}", file=sys.stderr)
        return False
    if target is None:
        # Bound to a platform whose adapter isn't live in this process (e.g.
        # a CLI call with no bot running): leave it to the daemon's tick.
        return False
    adapter, thread_key = target

    async def _send() -> None:
        await adapters.deliver(adapter, thread_key, text, conversation_id=conversation_id, role="system")

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            adapters.run_sync(_send())
            return True
        except Exception as exc:
            print(f"WARNING: failed to post immediate watch announcement: {exc}", file=sys.stderr)
            return False

    # We are on the bot's own event loop (e.g. an in-process MCP tool call):
    # blocking is impossible, so deliver in a background task. It claims the
    # ``announced`` flag first (so the tick-based retry can't double-post)
    # and releases it again if delivery fails.
    async def _announce_in_background() -> None:
        _set_announced(watch_id, True)
        try:
            await _send()
        except Exception:
            log.exception("[watcher] immediate announcement for watch #%s failed - tick will retry", watch_id)
            _set_announced(watch_id, False)

    task = asyncio.get_running_loop().create_task(_announce_in_background())
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return False


_BACKGROUND_TASKS: set = set()


def _set_announced(watch_id: int, value: bool) -> None:
    watch = get_watch(watch_id)
    if watch is None:
        return
    try:
        meta = json.loads(watch.get("meta") or "{}")
    except Exception:
        meta = {}
    meta["announced"] = value
    set_meta(watch_id, meta)


def add_watch(
    type_: str,
    conversation_id: str,
    key: str | None = None,
    label: str | None = None,
    meta: dict | None = None,
    announce: bool = True,
) -> int:
    if type_ not in valid_types():
        raise ValueError(_unknown_type_message(type_))
    conversation_id = (conversation_id or "").strip()
    if not conversation_id:
        raise ValueError(
            "watch requires a conversation_id (this session's own bot-side "
            "conversation id — see the '[Session context: ...]' block injected "
            "at the top of this session, or ask via session_get). Slack "
            "channel/thread_ts are no longer accepted directly: watches are "
            "keyed by conversation, not by Slack thread, so they work "
            "correctly for Web-UI-only sessions too."
        )
    if message_store.get_conversation(conversation_id) is None:
        raise ValueError(
            f"Unknown conversation_id {conversation_id!r} — no such bot "
            "conversation exists. Don't guess or reuse another session's id."
        )
    integration = (meta or {}).get("integration")
    validate_key(type_, key, integration)
    if integration:
        check_watch_resource_exists(type_, key, integration)
    else:
        check_watch_resource_exists(type_, key)
    if type_ == "script" and not (label and label.strip()):
        raise ValueError("script watch requires --label (the prompt injected when the script reports a change or fails)")
    if type_ == "time" and not (label and label.strip()):
        # For `time`, the label IS the message injected when it fires (there's
        # no external event data), so it can't be empty.
        raise ValueError("time watch requires --label (the prompt/message to inject when it fires)")
    meta = dict(meta or {})
    if type_ == "time" and (meta.get("integration") or meta.get("watcher")):
        raise ValueError("meta.integration / meta.watcher are not supported on `time` watches (they would be "
                         "silently ignored); use type='script' with meta.watcher to run a driver watcher recipe.")
    if type_ == "script":
        meta = watch_scripts.apply_watcher(meta)
        watch_scripts.validate_script_watch_meta(meta)
        if meta.get("expires_in"):
            m = re.match(r"^(\d+)\s*(m|h|d)$", str(meta["expires_in"]), re.IGNORECASE)
            if not m:
                raise ValueError(f"meta.expires_in must look like '30m', '12h' or '7d', got {meta['expires_in']!r}")
            unit = {"m": "minutes", "h": "hours", "d": "days"}[m.group(2).lower()]
            meta["expires_at"] = (datetime.now() + timedelta(**{unit: int(m.group(1))})).isoformat()
    elif meta.get("script") is not None:
        if type_ != "time":
            raise ValueError("meta.script is only supported on `time` watches (type='time') or `script` watches.")
        watch_scripts.validate_meta(meta)
    if type_ == "self_restart":
        # Stamp the *current* boot generation so we can later detect "a
        # restart happened since this watch was created" by comparing this
        # value against the boot id at check time (see due_self_restart_watches).
        meta.setdefault("boot_id_at_creation", current_boot_id())
    if type_ in ("time", "script"):
        parsed = parse_time_key(key.strip())
        next_fire = compute_next_fire(parsed)
        if type_ == "script" and meta.get("run_immediately", True):
            next_fire = datetime.now()  # first run right away (baseline), then on schedule
        meta["next_fire_at"] = next_fire.isoformat()
        meta["recurring"] = is_recurring_time_kind(parsed["kind"])
    if type_ == "session_turn":
        # Resolve "self" now: the poller can't tell which conversation
        # registered the watch, so the row must store a concrete id.
        target = (key or "").strip()
        if target.lower() == "self":
            target = conversation_id
        if message_store.get_conversation(target) is None:
            raise ValueError(
                f"session_turn watch target conversation_id {target!r} does not exist — "
                "don't guess or reuse another session's id."
            )
        key = target

        # Baseline is the target's last *completed* turn generation (same for
        # self- and other-session watches). If a turn is in flight now, its
        # generation hasn't been recorded as completed yet (send()'s finally:
        # block bumps it only once the turn fully ends), so that turn's
        # completion satisfies the ">" check in _poll_session_turn_watch()
        # and fires the watch; if idle, the next turn fires it.
        target_convo = get_convo_if_exists(target)
        if target_convo is not None:
            baseline = target_convo.last_completed_turn_generation()
        else:
            # No in-memory CopilotConversation yet in this process (it exists
            # in message_store but hasn't run since the last bot restart), so
            # its first completed turn fires this watch.
            baseline = 0
        meta["baseline_generation"] = baseline
        # turn_generation is in memory only and resets on every bot restart;
        # the boot id lets _poll_session_turn_watch detect a restart.
        meta["boot_id_at_creation"] = current_boot_id()
        meta.setdefault("recurring", False)
        if target == conversation_id:
            # Nothing external bounds how often a self-subscription can chain,
            # so cap it by default; callers can override via meta["max_chain"].
            meta.setdefault("max_chain", SESSION_TURN_DEFAULT_MAX_CHAIN)
            meta.setdefault("chain_count", 0)
    _ensure_db()
    channel, thread_ts = _resolve_delivery(conversation_id)
    now = _now()
    with _conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO watches (type, key, label, channel, thread_ts, conversation_id,
                                  status, last_state, meta, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'active', NULL, ?, ?, ?)
            """,
            (type_, key, label, channel or "", thread_ts or "", conversation_id,
             json.dumps(meta), now, now),
        )
        watch_id = cur.lastrowid

    if announce:
        # On failure, meta.announced stays unset so dop_agent.py's tick-based
        # _announce_new_watches retries.
        if _post_watch_announcement(watch_id, type_, key, label, conversation_id, channel, thread_ts, now, meta):
            meta["announced"] = True
            set_meta(watch_id, meta)

    return watch_id



def list_watches(conversation_id: str | None = None, include_inactive: bool = False) -> list[dict]:
    _ensure_db()
    query = "SELECT * FROM watches"
    clauses = []
    params: list = []
    if conversation_id is not None:
        clauses.append("conversation_id = ?")
        params.append(conversation_id)
    if not include_inactive:
        clauses.append("status != 'done'")
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY id"
    with _conn() as conn:
        return [dict(row) for row in conn.execute(query, params)]


def get_watch(watch_id: int) -> dict | None:
    _ensure_db()
    with _conn() as conn:
        row = conn.execute("SELECT * FROM watches WHERE id = ?", (watch_id,)).fetchone()
        return dict(row) if row else None


def set_status(watch_id: int, status: str) -> bool:
    if status not in VALID_STATUSES:
        raise ValueError(f"Unknown status {status!r}, expected one of {VALID_STATUSES}")
    _ensure_db()
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE watches SET status = ?, updated_at = ? WHERE id = ?",
            (status, _now(), watch_id),
        )
        return cur.rowcount > 0


def mark_broken(watch_id: int, reason: str) -> bool:
    """Flip a watch to the ``broken`` status and record ``reason`` in its
    meta (key: ``broken_reason``) — called by dop_agent.py's poller when a
    watch's key is permanently invalid (a static format defect, not a
    transient fetch error), so it's never silently polled/skipped again.
    Distinct from ``pause``/``resume``: a paused watch is expected to come
    back; a broken watch needs the key fixed (remove + re-add) before it can
    ever fire, so it's surfaced differently in `list`/WATCHERS.md."""
    _ensure_db()
    w = get_watch(watch_id)
    meta = {}
    if w:
        try:
            meta = json.loads(w.get("meta") or "{}")
        except Exception:
            meta = {}
    meta["broken_reason"] = reason
    meta["broken_at"] = _now()
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE watches SET status = 'broken', meta = ?, updated_at = ? WHERE id = ?",
            (json.dumps(meta), _now(), watch_id),
        )
        return cur.rowcount > 0


def set_meta(watch_id: int, meta: dict) -> bool:
    """Overwrite a watch's meta blob (JSON-serialized dict). Callers should
    read-modify-write (get_watch() -> merge -> set_meta()) rather than
    assuming this merges for them. Used e.g. to flip a one-off
    ``announced`` flag once a "new watcher registered" message has been
    posted, so it isn't re-posted on every subsequent tick."""
    _ensure_db()
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE watches SET meta = ?, updated_at = ? WHERE id = ?",
            (json.dumps(meta), _now(), watch_id),
        )
        return cur.rowcount > 0


def set_script_state(watch_id: int, state: dict) -> None:
    with _conn() as conn:
        conn.execute("UPDATE watches SET script_state=? WHERE id=?", (json.dumps(state), watch_id))


def set_last_state(watch_id: int, last_state: str | None) -> bool:
    """Persist arbitrary poll-progress state for a watch (e.g. a JSON blob
    like {"last_seen_at": "<iso ts>"} for a `teams` watch, so the next poll
    tick knows what's already been seen/notified and doesn't re-fire on the
    same message). Callers pass a pre-serialized string (usually
    json.dumps(...)) so this module stays agnostic of the state shape."""
    _ensure_db()
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE watches SET last_state = ?, updated_at = ? WHERE id = ?",
            (last_state, _now(), watch_id),
        )
        return cur.rowcount > 0


def remove_watch(watch_id: int) -> bool:
    _ensure_db()
    with _conn() as conn:
        cur = conn.execute("DELETE FROM watches WHERE id = ?", (watch_id,))
        return cur.rowcount > 0


def pause_session(session_id: str) -> int:
    """Soft-disable (pause) every non-done watch registered by this session
    (the watches table still names the column ``conversation_id``)."""
    _ensure_db()
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE watches SET status = 'paused', updated_at = ? "
            "WHERE conversation_id = ? AND status != 'done'",
            (_now(), session_id),
        )
        return cur.rowcount


# ---------------------------------------------------------------------------
# Boot-id / self_restart support
#
# ``self_restart`` watches don't poll anything — they fire once the bot
# process comes back up in a *new* boot generation after the watch was
# created. See dop_agent.py's startup sequence: it calls rotate_boot_id()
# once at the very top of main() (before the socket handler starts), then
# due_self_restart_watches(new_boot_id) to find + fire anything that was
# registered in an earlier generation.
# ---------------------------------------------------------------------------

def current_boot_id() -> str:
    if BOOT_ID_FILE.exists():
        return BOOT_ID_FILE.read_text().strip()
    return ""


def rotate_boot_id() -> tuple[str, str]:
    """Write a fresh boot id, return (previous_boot_id, new_boot_id)."""
    old = current_boot_id()
    new = uuid.uuid4().hex
    BOOT_ID_FILE.write_text(new)
    return old, new


def due_self_restart_watches(new_boot_id: str) -> list[dict]:
    """Active self_restart watches created in an earlier boot generation —
    i.e. a restart has happened since they were registered."""
    due = []
    for w in list_watches(include_inactive=False):
        if w["type"] != "self_restart" or w["status"] != "active":
            continue
        meta = json.loads(w["meta"] or "{}")
        if meta.get("boot_id_at_creation") != new_boot_id:
            due.append(w)
    return due


def check_watch(w: dict) -> str | None:
    """Validate a watch's key/meta against validate_key() without waiting for
    a poll tick to discover it's broken. Returns an error message, or None
    if it is well-formed (or already done). Does not mutate the registry."""
    if w["status"] not in ("active", "paused"):
        return None
    try:
        wmeta = json.loads(w.get("meta") or "{}")
        validate_key(w["type"], w["key"], wmeta.get("integration"))
        if w["type"] == "script":
            watch_scripts.validate_script_watch_meta(wmeta)
    except ValueError as exc:
        return str(exc)
    return None




# ---------------------------------------------------------------------------
# Watchers — fire a synthetic "message" into a conversation's session when an
# event a watch is tracking becomes true, so the agent resumes the work.
# ---------------------------------------------------------------------------

RESUME_STATUS_INSTRUCTION = (
    "If your last message above stated an intent or in-progress action "
    "(e.g. \"retrying\", \"found it\") rather than a finished result, that "
    "work is not actually done — continue and finish it now, don't just "
    "restate it. This also covers any earlier unfinished plan you described "
    "(e.g. \"now automating the rest via a script\") that you never verified "
    "as actually executed (no matching git commits/todo updates) — actually "
    "execute/run it now instead of repeating that it's planned. If "
    "everything really is already complete, say so briefly."
)
"""Shared instruction appended to every restart/interrupted-resume prompt fed
into the agent (self_restart watches, active_status-based interrupted-turn
resume). It tells the agent that a stated intent ("Retrying.", "Found it.")
or an announced-but-never-executed plan is not a finished result. Always used
together with build_resume_instruction(), which quotes the actual cut-off
last user/assistant messages so the agent has concrete content to act on."""


def _last_turn_snippets(conversation_id: str, max_len: int = 220) -> tuple[str | None, str | None]:
    """Return (last_user_text, last_assistant_text) for a conversation, each
    stripped and truncated to max_len chars, or None for either that hasn't
    happened yet.

    Used by build_resume_instruction() to remind the agent what it was doing
    right before an automated event interrupted it.
    """
    try:
        messages = message_store.get_messages(conversation_id)
    except Exception:
        log.exception("[watcher] Could not read messages for %s to build resume context", conversation_id)
        return None, None

    def _trunc(text: str | None) -> str | None:
        if text is None:
            return None
        text = text.strip()
        if not text:
            return None
        return text if len(text) <= max_len else text[:max_len].rstrip() + "…"

    last_user = None
    last_assistant = None
    for m in reversed(messages):
        role = m.get("role")
        if role == "user" and last_user is None:
            last_user = _trunc(m.get("content"))
        elif role == "assistant" and last_assistant is None:
            last_assistant = _trunc(m.get("content"))
        if last_user is not None and last_assistant is not None:
            break
    return last_user, last_assistant


def build_resume_instruction(conversation_id: str) -> str:
    """Build the full resume-instruction text for a restart/self_restart
    wake-up: the last user request + the (possibly cut-off) last assistant
    message, quoted verbatim, followed by RESUME_STATUS_INSTRUCTION.

    Quoting the exact messages gives the agent concrete content to act on
    instead of relying on scrollback it may not re-read after a restart.
    """
    last_user, last_assistant = _last_turn_snippets(conversation_id)
    parts = []
    if last_user:
        parts.append(f'Your last task here was: "{last_user}"')
    if last_assistant:
        parts.append(f'Your last message before being interrupted was: "{last_assistant}"')
    context = " ".join(parts)
    if context:
        return f"{context} {RESUME_STATUS_INSTRUCTION}"
    return RESUME_STATUS_INSTRUCTION


FIRE_REPLY_RULES = (
    "\n\n---\n(Watcher-event reply rules — follow strictly.\n"
    "1. If this event needs no action and tells the user nothing new (e.g. an echo of "
    "your own push, a bot/automation note, a pipeline of an older commit, a duplicate), "
    "reply with exactly `ACK` or `ACK - <few-word reason>` and nothing else.\n"
    "2. If this event completes what the user is waiting for (pipeline finished, MR ready "
    "or approved, review needed, ticket changed) or anything failed, do NOT answer with a "
    "one-liner: give a final report - a one-line headline, then a table of the relevant "
    "items (MR, pipeline, review, ticket, etc.) each with a clickable link and state, "
    "then a `Next:` line saying what is needed from the user (or `Next: nothing`).\n"
    "3. Otherwise act on it normally and keep the reply short.)"
)

FINAL_REPORT_REPROMPT = (
    "Your last reply was too thin for a final watcher event. Give the final report now: "
    "a one-line headline, then a table of the relevant items (MR, pipeline, review, "
    "ticket) each with a clickable link and state, then a `Next:` line (or `Next: nothing`). "
    "Reply with the report only."
)

# Fires arriving within this many seconds for the same conversation are
# merged into ONE agent turn (the raw notices are still all posted).
FIRE_COALESCE_SECS = config.value("watchers.coalesce_window_s")


def _watch_is_stale(watch_id: int) -> bool:
    w = get_watch(watch_id)
    return w is None or w.get("status") != "active"
_pending_fires: dict[str, list[str]] = {}
_pending_final: set[str] = set()


def _take_final(conversation_id: str) -> bool:
    flagged = conversation_id in _pending_final
    _pending_final.discard(conversation_id)
    return flagged


def _watch_is_final(watch_id: int) -> bool:
    """meta.final=true (or a meta.purpose) marks a watch whose fire is what
    the user is waiting for -> always answered with a full report."""
    w = get_watch(watch_id)
    try:
        meta = json.loads((w or {}).get("meta") or "{}")
    except Exception:
        return False
    return bool(meta.get("final") or meta.get("purpose"))


def _is_coalesced_type(wtype: str) -> bool:
    return bool((_integrations().watch_type_meta(wtype) or {}).get("coalesced"))


# Time/session_turn labels are literal prompts (e.g. the daily review) - never wrap them.
RULES_WATCH_TYPES: set[str] = set()
_fire_ctx: contextvars.ContextVar = contextvars.ContextVar("watch_fire_ctx", default=None)


_flush_tasks: set = set()
FIRE_QUEUE_MAX_WAIT_SECS = 600.0


async def _flush_fires(conversation_id: str) -> None:
    """Wait for the coalescing window, then (queueing rather than
    interrupting) wait for any in-flight turn to finish, then deliver the
    batch as ONE agent turn."""
    try:
        await asyncio.sleep(FIRE_COALESCE_SECS)
        waited = 0.0
        convo = get_convo_if_exists(conversation_id)
        while convo is not None and convo.is_busy() and waited < FIRE_QUEUE_MAX_WAIT_SECS:
            await asyncio.sleep(2.0)
            waited += 2.0
        texts = _pending_fires.pop(conversation_id, [])
        final = _take_final(conversation_id)
        if texts:
            await _inject_watch_message_now(conversation_id, texts, rules=True, final=final)
    except Exception:
        _pending_fires.pop(conversation_id, None)
        _pending_final.discard(conversation_id)
        log.exception("[watcher] failed to deliver coalesced fires for %s", conversation_id)


async def _inject_watch_message(conversation_id: str, text: str) -> None:
    """Public entry for every watcher fire. Behaviour is driven by the
    ``_fire_ctx`` ContextVar that _poll_watch_group() sets around each poll
    (so poll functions - and the many tests that stub this function with a
    plain ``(conversation_id, text)`` signature - need no extra arguments):

    * stale-watch guard: if the firing watch was removed/paused/finished
      while its poll was in flight (typically because the agent reacted to a
      sibling's fire), the fire is dropped instead of arriving late;
    * coalescing (external-event watch types only - see
      see _is_coalesced_type): fires for the same conversation within
      FIRE_COALESCE_SECS are merged into ONE agent turn carrying
      FIRE_REPLY_RULES (ACK protocol + final-report template); all raw
      notices are still posted.

    Outside a poll (self_restart, notices, direct calls) it behaves exactly
    as _inject_watch_message_now()."""
    ctx = _fire_ctx.get()
    if ctx is None:
        await _inject_watch_message_now(conversation_id, text)
        return
    watch_id, wtype = ctx
    if _watch_is_stale(watch_id):
        log.info("[watcher] watch #%s no longer active - dropping stale fire", watch_id)
        return
    if not _is_coalesced_type(wtype):
        await _inject_watch_message_now(conversation_id, text, rules=wtype in RULES_WATCH_TYPES)
        return
    if _watch_is_final(watch_id):
        _pending_final.add(conversation_id)
    pending = _pending_fires.get(conversation_id)
    if pending is not None:
        pending.append(text)
        return
    _pending_fires[conversation_id] = [text]
    # Flush in the background so the first caller returns immediately and the
    # same poll's later fire (e.g. an MR's note + pipeline result) can join
    # the same batch instead of waiting behind this one's sleep.
    task = asyncio.create_task(_flush_fires(conversation_id))
    _flush_tasks.add(task)
    task.add_done_callback(_flush_tasks.discard)


async def _inject_watch_message_now(conversation_id: str, text: "str | list[str]", *, rules: bool = False,
                                    final: bool = False) -> None:
    """Post ``text`` verbatim first (so the observed fact, e.g. "pipeline
    #1234 succeeded", is always visible even if the agent's reaction differs),
    then feed the same ``text`` into the conversation's session as if the
    authorized user had sent it, delivering the agent's reply via whatever
    binding the conversation currently has (Slack thread, or Web UI message
    + live SSE update).

    Used by watcher_loop()/the startup self_restart check, never from a
    Slack event, so it uses the bot's own ``app.client`` rather than a Bolt
    ``say``/``client``. A Web-UI-only conversation (see _resolve_delivery())
    is delivered purely through WebAdapter/message_store."""

    raw_notices = text if isinstance(text, list) else [text]
    text = "\n\n".join(raw_notices)
    if rules:
        text = text + FIRE_REPLY_RULES

    adapter, thread_key = _delivery_target(conversation_id)
    # Opaque thread root for the turn engine's reaction/status bookkeeping.
    thread_ts = thread_key.get("thread_ts")

    # A watcher fire landing mid-turn makes send() abort that turn exactly like
    # a user follow-up would; tell the agent to handle the event and then go
    # back to the interrupted task instead of silently dropping it.
    convo = get_convo_if_exists(conversation_id)
    if convo is not None and convo.is_busy():
        last_user, _ = _last_turn_snippets(conversation_id)
        if last_user:
            text = (
                f"{text}\n\n(This message supersedes your in-progress task: "
                f"\"{last_user}\" — address THIS message now; only resume "
                "that task afterward if it's still relevant. Don't drop it "
                "entirely, just don't let it block handling this.)"
            )

    try:
        posted = None
        for notice in raw_notices:
            posted, _ = await adapters.deliver(adapter,
                thread_key,
                notice,
                conversation_id=conversation_id,
                role="system",
            )
    except Exception:
        log.exception("[watcher] Failed to post raw watch-fired notice to conversation %s", conversation_id)
        posted = None

    async def _run(prompt: str) -> None:
        await _handle_fn(
            user_id=ALLOWED_USER_ID or "watcher",
            text=prompt,
            ts=((adapter.message_ref(thread_key, posted).id if posted else None) or str(time.time())),
            thread_ts=thread_ts,
            conversation_id=conversation_id,
            record_user_message=False,
            adapter=adapter,
            thread_key=thread_key,
            authorized=True,
        )

    await _run(text)

    # Final fire (terminal pipeline/job result, or a watch flagged
    # meta.final) must end with a real report, not "ok, pipeline is green".
    # The reply rules ask for that, but the agent doesn't always comply -
    # so check what it actually said and re-prompt ONCE if it was thin.
    if rules and (final or any(message_kinds.is_final_fire_text(n) for n in raw_notices)):
        try:
            last = (message_store.get_messages(conversation_id) or [None])[-1]
        except Exception:
            log.exception("[watcher] could not read back reply for final-report check")
            last = None
        if last and last.get("role") == "assistant" and message_kinds.is_thin_reply(last.get("content")):
            log.info("[watcher] thin reply to a final fire in %s - re-prompting for the full report", conversation_id)
            await _run(FINAL_REPORT_REPROMPT)


async def _deliver_system_notice(conversation_id: str, text: str) -> None:
    """Post a plain informational system notice into a conversation. Unlike
    _inject_watch_message(), it never feeds the Copilot session (no
    _handle_fn call): used for "watch is broken"/"new watch registered"
    notices, which are asides about the watch system, not agent tasks."""
    adapter, thread_key = _delivery_target(conversation_id)
    await adapters.deliver(adapter, thread_key, text, conversation_id=conversation_id, role="system")


async def _fire_self_restart_watches(new_boot_id: str) -> None:
    """One-shot check run at startup (not on the periodic loop): resume any
    conversation that registered a "resume me after the bot restarts" watch
    in a previous boot generation. Runs as a fire-and-forget asyncio task
    (see main()) so a slow/failed resume can't delay startup."""
    try:
        due = due_self_restart_watches(new_boot_id)
    except Exception:
        log.exception("[watcher] Could not read due self_restart watches")
        return
    for w in due:
        log.info("[watcher] self_restart watch #%s due (conversation=%s) — resuming",
                  w["id"], w["conversation_id"])
        # Mark done *before* resuming: the resume runs a full agent turn, and a
        # restart landing meanwhile would kill the process before a later
        # set_status, leaving the watch active and re-firing on every restart.
        set_status(w["id"], "done")
        label = f" ({w['label']})" if w["label"] else ""
        text = f":repeat: Bot restarted successfully{label}. {build_resume_instruction(w['conversation_id'])}"
        try:
            await _inject_watch_message(w["conversation_id"], text)
        except Exception:
            log.exception("[watcher] Failed to resume self_restart watch #%s", w["id"])


async def _fetch_with_timeout(fn, *args, timeout: float | None = None, **kwargs):
    """Run a blocking client call (an integration client method) in a
    worker thread, bounded by `timeout` (defaults to WATCH_POLL_TIMEOUT) — a
    safety net for a hung DNS/VPN/socket state that the HTTP client's own
    timeouts don't cover.

    Scoped to *only* the network fetch, not the whole poll function: a poll
    also calls _inject_watch_message(), which triggers an open-ended agent
    turn (bounded only by SESSION_TIMEOUT) that must not be cancelled by a
    fetch-sized timeout — cancelling mid-turn leaves the session and its
    status UI in an unclean state."""
    return await asyncio.wait_for(
        asyncio.to_thread(fn, *args, **kwargs),
        timeout=timeout if timeout is not None else WATCH_POLL_TIMEOUT,
    )


async def _report_broken_watch(w: dict, reason: str, *, integration_problem: bool = False) -> None:
    """A watch's key is *permanently* invalid (a static format defect, not a
    transient fetch error). Posts one system notice into the watch's
    conversation (not fed to the agent) and marks the watch `broken` via
    mark_broken(), so watcher_loop()'s `status == "active"` filter stops
    polling it and the notice isn't repeated every tick."""
    log.warning("[watcher] %s watch #%s is broken: %s", w["type"], w["id"], reason)
    if integration_problem:
        text = (
            f":warning: *Watch #{w['id']} (`{w['type']}`) has no active integration and has been "
            f"marked broken* — {reason}\n"
            f"It will not be polled again: re-enable the integration and resume the watch, "
            f"or remove it with `watch_remove`."
        )
    else:
        text = (
            f":warning: *Watch #{w['id']} (`{w['type']}`) has an invalid key and has been "
            f"marked broken* — {reason}\n"
            f"It will not be polled again until fixed: use `watch_pause`/`watch_remove` "
            f"to stop it, then re-register it with a corrected key via `watch_add`."
        )
    try:
        await _deliver_system_notice(w["conversation_id"], text)
    except Exception:
        log.exception("[watcher] failed to post broken-watch notice for #%s", w["id"])
    mark_broken(w["id"], reason)


async def _fire_script_watch(w: dict, meta: dict, label: str) -> None:
    """Run the watch's allow-listed script (see watch_scripts.py) and either
    wake the model (wake_if condition / any failure), post a plain notice
    (no model turn) or stay silent."""
    result = await watch_scripts.run_script(meta)
    state = watch_scripts.parse_state(w.get("script_state"), meta)
    action, text, new_state = watch_scripts.decide(meta, result, state)
    set_script_state(w["id"], new_state)
    head = f":alarm_clock: *Scheduled watch #{w['id']}* `{meta['script']}`"
    if action == "wake":
        await _inject_watch_message(w["conversation_id"], f"{head} fired\n{label}\n\n{text}")
    elif action == "notice":
        await _deliver_system_notice(w["conversation_id"], f"{head}: {text}")
    else:
        log.info("[watcher] script watch #%s: nothing to report", w["id"])


async def _poll_time_watch(w: dict) -> None:
    """Check one active `time` watch: wakes the conversation at a scheduled
    local wall-clock moment. See parse_time_key() for the key grammar. The
    watch's `label` is the literal prompt injected on fire.

    One-shot watches ('in:'/'at:') flip to status=done after firing once.
    Recurring watches (cron kinds) stay active and advance meta.next_fire_at
    via advance_recurring, which skips occurrences missed while the bot was
    down instead of firing once per missed occurrence."""
    try:
        parsed = parse_time_key((w.get("key") or "").strip())
    except ValueError as exc:
        await _report_broken_watch(w, str(exc))
        return

    try:
        meta = json.loads(w.get("meta") or "{}")
    except Exception:
        meta = {}

    next_fire_str = meta.get("next_fire_at")
    if not next_fire_str:
        # add_watch() always seeds this; recompute if it is missing.
        next_fire = compute_next_fire(parsed)
        meta["next_fire_at"] = next_fire.isoformat()
        set_meta(w["id"], meta)
        return

    try:
        next_fire = datetime.fromisoformat(next_fire_str)
    except Exception:
        await _report_broken_watch(w, f"corrupted meta.next_fire_at: {next_fire_str!r}")
        return

    now = datetime.now()
    if now < next_fire:
        return  # not due yet

    label = (w.get("label") or "").strip() or "Scheduled check-in"
    text_out = f":alarm_clock: *Scheduled watch #{w['id']} fired*\n{label}"
    try:
        if meta.get("script"):
            await _fire_script_watch(w, meta, label)
        else:
            await _inject_watch_message(w["conversation_id"], text_out)
        log.info("[watcher] time watch #%s fired (kind=%s)", w["id"], parsed["kind"])
    except Exception:
        log.exception("[watcher] failed to inject time watch #%s result", w["id"])
        return  # don't advance/close the schedule if delivery failed

    if is_recurring_time_kind(parsed["kind"]):
        new_next_fire = advance_recurring(parsed, next_fire, now)
        meta["next_fire_at"] = new_next_fire.isoformat()
        set_meta(w["id"], meta)
    else:
        set_status(w["id"], "done")


async def _poll_script_watch(w: dict) -> None:
    """`script` watch: run an allow-listed read-only script on a schedule (same
    key grammar as `time`, incl. full cron) and let its EXIT CODE decide:
    0 = noop (silent), 200 = changed (wake the session with the output),
    anything else / timeout = error (after meta.retries consecutive failures:
    wake the session and finish the watch so the agent decides what to do).
    Raw output goes to watch_scripts.OUT_ROOT/<id>/ (all kept, never pruned); the wake
    text carries a truncated head + that path. The script keeps its own
    "previous state" in $WATCH_STATE_DIR (per-watch dir)."""
    try:
        parsed = parse_time_key((w.get("key") or "").strip())
        meta = json.loads(w.get("meta") or "{}")
    except Exception as exc:
        await _report_broken_watch(w, str(exc))
        return
    try:
        next_fire = datetime.fromisoformat(meta["next_fire_at"])
    except Exception:
        meta["next_fire_at"] = compute_next_fire(parsed).isoformat()
        set_meta(w["id"], meta)
        return
    now = datetime.now()
    if now < next_fire:
        return
    state = {}
    try:
        state = json.loads(w.get("script_state") or "{}")
    except Exception:
        pass
    cid = w["conversation_id"]
    name = meta.get("script")
    head = f":scroll: *Script watch #{w['id']}* `{name}`"
    prompt = (w.get("label") or "").strip()

    def advance() -> None:
        if is_recurring_time_kind(parsed["kind"]):
            meta["next_fire_at"] = advance_recurring(parsed, next_fire, now).isoformat()
            set_meta(w["id"], meta)
        else:
            set_status(w["id"], "done")

    def finish(reason: str) -> None:
        meta["finished_reason"] = reason
        set_meta(w["id"], meta)
        set_status(w["id"], "done")

    if state.get("pending"):  # previous delivery failed - retry it before running again
        try:
            await _inject_watch_message(cid, state["pending"])
        except Exception:
            log.exception("[watcher] script watch #%s pending delivery failed", w["id"])
            return
        state.pop("pending")
        set_script_state(w["id"], state)
        if meta.get("pending_finish"):
            finish("script_error")
            return
    if meta.get("expires_at") and now >= datetime.fromisoformat(meta["expires_at"]):
        finish("expired")
        await _deliver_system_notice(cid, f"{head} expired ({meta['expires_at']}) and was stopped.")
        return

    try:
        pinned_env = watch_scripts.instance_env(meta)
    except ValueError as exc:  # pinned integration gone: fail (with retries) instead of running as another account
        result = {"rc": 1, "stdout": "", "stderr": str(exc), "timed_out": False}
    else:
        result = await watch_scripts.run_script(
            meta, extra_env={**pinned_env,
                             "WATCH_STATE_DIR": str(watch_scripts.state_dir_for(w["id"])),
                             "WATCH_ID": str(w["id"])})
    out_path = None
    try:
        out_path = watch_scripts.write_output(w["id"], result)
    except Exception:
        log.exception("[watcher] script watch #%s could not store output", w["id"])
    outcome, text, state = watch_scripts.decide_contract(meta, result, state, out_path)
    set_script_state(w["id"], state)
    log.info("[watcher] script watch #%s -> %s (rc=%s)", w["id"], outcome, result["rc"])

    if outcome in ("changed", "error"):
        what = "reported a change" if outcome == "changed" else "FAILED - the watch is stopped; decide whether to fix and re-add it"
        msg = f"{head} {what}\n{prompt}\n\n{text}"
        try:
            await _inject_watch_message(cid, msg)
        except Exception:
            log.exception("[watcher] failed to deliver script watch #%s result", w["id"])
            state["pending"] = msg
            set_script_state(w["id"], state)
            if outcome == "error":
                meta["pending_finish"] = True
                set_meta(w["id"], meta)
            return
    if outcome == "error":
        finish("script_error")
        return
    if outcome == "changed" and meta.get("until_changed"):
        finish("changed")
        return
    if meta.get("max_runs") and state["runs"] >= meta["max_runs"]:
        finish("max_runs")
        await _deliver_system_notice(cid, f"{head} reached max_runs={meta['max_runs']} and was stopped.")
        return
    advance()


async def _maybe_notify_stale_session_turn_watch(w: dict, meta: dict) -> None:
    """Post a one-time "still waiting?" notice if this watch has gone
    unfired for longer than SESSION_TURN_STALE_AFTER since registration.
    Does not close the watch (a long-idle target isn't necessarily wrong);
    meta.stale_notice_sent prevents re-notifying on every tick."""
    if meta.get("stale_notice_sent"):
        return
    created_at = w.get("created_at")
    if not created_at:
        return
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError:
        return
    if datetime.now(timezone.utc) - created < SESSION_TURN_STALE_AFTER:
        return
    meta["stale_notice_sent"] = True
    set_meta(w["id"], meta)
    await _deliver_system_notice(
        w["conversation_id"],
        f":hourglass: *session_turn watch #{w['id']} still waiting* — no new turn observed "
        f"on `{w.get('key')}` in over {SESSION_TURN_STALE_AFTER.days} days since it was "
        "registered. Still waiting on purpose, or is the target stuck? "
        "`watch_remove` it if it's no longer needed.",
    )


async def _poll_session_turn_watch(w: dict) -> None:
    """Check one active `session_turn` watch: fires once the TARGET
    conversation (``w['key']``, a concrete conversation_id resolved at
    registration) has completed a turn whose generation is strictly greater
    than ``meta.baseline_generation`` (see add_watch()). For a
    self-subscription, the registering turn's own completion fires it.

    last_completed_turn_generation() only advances in send()'s finally: block
    after a turn fully ends, so polling can't observe it early. It is
    persisted to bot_store.db (message_store.get/set_turn_generation) and
    restored when a CopilotConversation is constructed, so a restart doesn't
    reset it; only the *completed* half is persisted, so a freshly restarted
    process (which has no turn in flight) is consistent with the baseline rule.

    A purely local in-memory comparison, so it runs on every base
    watcher_loop tick with no separate interval gate.
    """
    target_id = (w.get("key") or "").strip()
    if not target_id:
        await _report_broken_watch(w, "session_turn watch has no target conversation_id")
        return

    try:
        meta = json.loads(w.get("meta") or "{}")
    except Exception:
        meta = {}

    if meta.get("boot_id_at_creation") != current_boot_id():
        # A restart doesn't invalidate the watch (the completed generation is
        # persisted); re-stamp so this branch runs once per restart.
        meta["boot_id_at_creation"] = current_boot_id()
        set_meta(w["id"], meta)

    baseline = meta.get("baseline_generation")
    if baseline is None:
        await _report_broken_watch(w, "session_turn watch is missing meta.baseline_generation")
        return

    # get_convo() (not get_convo_if_exists()) so the conversation is
    # constructed on first poll after a restart, restoring its persisted
    # last_completed_turn_generation (see CopilotConversation.__init__).
    target_convo = get_convo(target_id)
    last_completed = target_convo.last_completed_turn_generation()
    if last_completed <= baseline:
        await _maybe_notify_stale_session_turn_watch(w, meta)
        return  # the in-flight (or no later) turn hasn't finished yet

    max_chain = meta.get("max_chain")
    chain_count = int(meta.get("chain_count", 0))
    if max_chain is not None and chain_count >= max_chain:
        await _deliver_system_notice(
            w["conversation_id"],
            f":octagonal_sign: *session_turn watch #{w['id']} stopped* — hit its "
            f"max_chain limit ({max_chain} auto-resumes) without the chain being "
            "explicitly extended, to avoid a runaway self-resuming loop. "
            "`watch_resume` it and raise meta.max_chain if a longer chain is "
            "genuinely intended.",
        )
        set_status(w["id"], "done")
        return

    label = (w.get("label") or "").strip() or "A watched session turn completed."
    text_out = (
        f":arrows_counterclockwise: *Session turn watch #{w['id']} fired* "
        f"(target `{target_id}`)\n{label}"
    )
    try:
        await _inject_watch_message(w["conversation_id"], text_out)
        log.info(
            "[watcher] session_turn watch #%s fired (target=%s, generation %s > baseline %s)",
            w["id"], target_id, last_completed, baseline,
        )
    except Exception:
        log.exception("[watcher] failed to inject session_turn watch #%s result", w["id"])
        return  # don't advance/close if delivery failed

    if meta.get("recurring"):
        meta["baseline_generation"] = last_completed
        if max_chain is not None:
            meta["chain_count"] = chain_count + 1
        set_meta(w["id"], meta)
    else:
        set_status(w["id"], "done")


# Watch ids whose session_turn poll is currently running - shared by
# watcher_loop() and the instant turn-complete dispatcher so the same watch
# is never polled (and therefore never fired) twice concurrently.
_session_turn_inflight: set[int] = set()
# After a target's turn ends, wait this long (lock already free) so its final
# reply is posted BEFORE the dependent session is triggered.
TURN_COMPLETE_GRACE_SECS = 1.5
TURN_COMPLETE_MAX_WAIT_SECS = 30.0
_turn_complete_tasks: set = set()


async def _poll_session_turn_guarded(w: dict) -> None:
    wid = w["id"]
    if wid in _session_turn_inflight:
        return
    _session_turn_inflight.add(wid)
    token = _fire_ctx.set((wid, "session_turn"))
    try:
        fresh = get_watch(wid)
        if not fresh or fresh.get("status") != "active":
            return
        await _poll_session_turn_watch(fresh)
    finally:
        _fire_ctx.reset(token)
        _session_turn_inflight.discard(wid)


async def _dispatch_turn_complete(conversation_id: str) -> None:
    """Instantly fire session_turn watches targeting ``conversation_id``
    (see add_turn_complete_listener). If the target is busy again after the
    grace period (e.g. a user message took over), do nothing - the periodic
    watcher_loop() remains the fallback."""
    try:
        convo = get_convo_if_exists(conversation_id)
        waited = 0.0
        while convo is not None and convo.is_busy() and waited < TURN_COMPLETE_MAX_WAIT_SECS:
            await asyncio.sleep(0.2)
            waited += 0.2
        await asyncio.sleep(TURN_COMPLETE_GRACE_SECS)
        if convo is not None and convo.is_busy():
            return
        for w in list_watches():
            if w["status"] == "active" and w["type"] == "session_turn" and (w.get("key") or "").strip() == conversation_id:
                log.info("[watcher] instant session_turn dispatch: turn of %s ended, polling watch #%s",
                         conversation_id, w["id"])
                lock_key = _conversation_lock_key(w)
                lock = _conversation_locks.setdefault(lock_key, asyncio.Lock())
                try:
                    # Same lock as background polls: never overlap two turns
                    # on the watch's conversation.
                    async with lock:
                        await _poll_session_turn_guarded(w)
                except Exception:
                    log.exception("[watcher] instant session_turn watch #%s poll failed", w["id"])
                finally:
                    _release_conversation_lock(lock_key)
    except Exception:
        log.exception("[watcher] turn-complete dispatch failed for %s", conversation_id)


def _on_turn_complete(conversation_id: str) -> None:
    """Sync listener: only schedules a task (never awaits - it runs inside
    send()'s finally: with the conversation lock held)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(_dispatch_turn_complete(conversation_id))
    _turn_complete_tasks.add(task)
    task.add_done_callback(_turn_complete_tasks.discard)


# Builtin type -> poll function name, shared by watcher_loop() and poll_once().
# self_restart has no entry: it's resolved once at process boot (see main()).
_BUILTIN_POLL_FN_NAMES = {
    "time": "_poll_time_watch",
    "script": "_poll_script_watch",
    "session_turn": "_poll_session_turn_watch",
}


def _poll_fn_for(type_: str):
    """Poll function for a watch type: registry (integration) types share the generic
    driver poller; builtin types have their own. None = unknown type."""
    if type_ in _integrations().watch_types():
        return _poll_integration_watch
    name = _BUILTIN_POLL_FN_NAMES.get(type_)
    return globals()[name] if name else None


async def poll_once(watch: dict) -> None:
    """Run a single watch's poll function immediately, outside watcher_loop()'s
    interval schedule. Backs the Web UI Watches panel's "re-check now" action
    (POST /api/watches/{id}/recheck). Not a read-only preview: it can trigger
    a real notification/agent turn.

    Raises ValueError (caller maps this to HTTP 400) for 'self_restart'
    watches (resolved only at boot) and any watch whose status isn't
    'active'."""
    type_ = watch.get("type")
    if type_ == "self_restart":
        raise ValueError("self_restart watches are resolved automatically at boot and can't be re-checked on demand")
    if watch.get("status") != "active":
        raise ValueError(f"watch #{watch.get('id')} is {watch.get('status')!r}, not 'active' — resume it first")
    poll_fn = _poll_fn_for(type_)
    if poll_fn is None:
        if type_ not in builtin_types():
            poll_fn = _poll_integration_watch  # integration disabled/missing: reports it as broken
        else:
            raise ValueError(f"unknown watch type {type_!r}")
    await poll_fn(watch)


# ---------------------------------------------------------------------------
# New-watch announcements + WATCHERS.md snapshot
# ---------------------------------------------------------------------------

async def _announce_new_watches(watches: list[dict]) -> None:
    """Fallback announcer: add_watch() normally posts the "watcher registered"
    message itself (see _post_watch_announcement); this tick-based check only
    catches watches whose immediate post failed or that were added with
    announce=False. Posted as a system notice, not fed to the agent.
    """
    for w in watches:
        try:
            meta = json.loads(w.get("meta") or "{}")
        except Exception:
            meta = {}
        if meta.get("announced"):
            continue

        emoji = (_type_emoji(w["type"], slack=True) or ":eyes:")
        lines = [f"{emoji} *New watcher registered* — #{w['id']} `{w['type']}`"]
        if w.get("key"):
            lines.append(f"• Target: `{w['key']}`")
        if w.get("label"):
            lines.append(f"• {w['label']}")
        lines.append(f"• Added: {_fmt_watch_ts(w.get('created_at'))}")
        lines.append(
            "_I'll resume this thread automatically when it fires. "
            "Use the `watch_pause` or `watch_remove` MCP tool to stop it._"
        )
        text = "\n".join(lines)

        if w.get("conversation_id") and message_store.get_conversation(w["conversation_id"]) is None:
            # Orphan: the conversation row is gone, so recording the notice
            # would fail its FOREIGN KEY on every tick forever.
            mark_broken(w["id"], f"conversation {w['conversation_id']} no longer exists")
            continue
        try:
            await _deliver_system_notice(w["conversation_id"], text)
        except Exception:
            log.exception("[watcher] failed to announce new watch #%s — will retry next tick", w["id"])
            continue  # don't mark announced; try again next tick

        meta["announced"] = True
        set_meta(w["id"], meta)


async def _write_watchers_md(watches: list[dict]) -> None:
    """Regenerate WATCHERS.md with one section per active watch: id/type/
    label/key, a link back to its thread, and created/last-checked
    timestamps. Rewritten on every watcher_loop tick; a human-skimmable
    index, not a source of truth (the sqlite db is)."""
    lines = [
        "# Active Watchers",
        "",
        f"_Last refreshed: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_",
        "",
    ]
    lines.append("## Watch types")
    lines.append("")
    lines.append("| Type | Emoji | Interval | Integration |")
    lines.append("|---|---|---|---|")
    try:
        for t, d in describe_types().items():
            interval = f"{d['interval_s']:g}s" if d["interval_s"] else "builtin"
            lines.append(f"| `{t}` | {d['slack_emoji'] or ''} | {interval} | {', '.join(d['integrations']) or '-'} |")
    except Exception:
        log.exception("[watcher] could not list watch types for WATCHERS.md")
    lines.append("")
    if not watches:
        lines.append("_(no active watches)_")
    for w in watches:
        emoji = (_type_emoji(w["type"], slack=True) or ":eyes:")
        broken_flag = " :warning: BROKEN" if w["status"] == "broken" else ""
        lines.append(f"## {emoji} Watch #{w['id']} — `{w['type']}`{broken_flag}")
        lines.append("")
        if w.get("label"):
            lines.append(f"- **Description:** {w['label']}")
        if w.get("key"):
            lines.append(f"- **Target:** `{w['key']}`")
        lines.append(f"- **Status:** {w['status']}")
        if w["status"] == "broken":
            try:
                meta = json.loads(w.get("meta") or "{}")
            except Exception:
                meta = {}
            if meta.get("broken_reason"):
                lines.append(f"- **Broken reason:** {meta['broken_reason']}")
        if w["type"] in ("time", "script") and w["status"] == "active":
            try:
                meta = json.loads(w.get("meta") or "{}")
            except Exception:
                meta = {}
            if meta.get("next_fire_at"):
                recurring_note = " (recurring)" if meta.get("recurring") else " (one-shot)"
                lines.append(f"- **Next fire:** {meta['next_fire_at']} local{recurring_note}")

        conversation_id = w.get("conversation_id")
        channel, thread_ts = _resolve_delivery(conversation_id) if conversation_id else (w.get("channel") or "", w.get("thread_ts") or None)
        if channel:
            permalink = None
            try:
                target = _registry().external_target(conversation_id) if conversation_id else None
                if target is not None and hasattr(target[0], "permalink"):
                    permalink = await target[0].permalink(target[1])
            except Exception as exc:
                log.debug("[watcher] permalink lookup failed for watch #%s (non-fatal): %s", w["id"], exc)
            thread_ref = f"`{channel}:{thread_ts}`"
            lines.append(f"- **Thread:** [{thread_ref}]({permalink})" if permalink else f"- **Thread:** {thread_ref}")
        else:
            try:
                tg_bound = bool(conversation_id and message_store.get_primary_binding(
                    conversation_id, message_store.TELEGRAM_PLATFORM))
            except Exception:
                tg_bound = False
            where = "Telegram, no Slack thread" if tg_bound else "Web UI only, no Slack thread"
            lines.append(f"- **Session:** `{conversation_id or 'unknown'}` ({where})")

        lines.append(f"- **Added:** {_fmt_watch_ts(w.get('created_at'))}")
        lines.append(f"- **Last checked:** {_fmt_watch_ts(w.get('updated_at'))}")
        lines.append("")

    try:
        WATCHERS_MD_PATH.write_text("\n".join(lines), encoding="utf-8")
    except Exception:
        log.exception("[watcher] failed to write %s", WATCHERS_MD_PATH)


async def _poll_watch_group(poll_fn, watches: list[dict], type_label: str) -> None:
    """Poll every watch in `watches` concurrently (`asyncio.gather`), each
    individually try/excepted so one misbehaving watch can neither raise out
    of the gather nor hold up its siblings. Running a group concurrently caps
    its duration at roughly one watch's worst-case timeout instead of the sum,
    so a network outage can't delay unrelated `time` watches.

    `poll_fn(w)` is deliberately NOT wrapped in a timeout: when something
    changed it calls `_inject_watch_message()`, which triggers an open-ended
    agent turn that a whole-function timeout would cancel mid-flight. The
    hung-network safety net is applied only around the blocking fetches, via
    `_fetch_with_timeout()`."""
    if not watches:
        return

    async def _one(w: dict) -> None:
        token = _fire_ctx.set((w["id"], w.get("type") or type_label))
        try:
            await poll_fn(w)
        except Exception:
            log.exception("[watcher] %s watch #%s poll failed", type_label, w["id"])
        finally:
            _fire_ctx.reset(token)

    await asyncio.gather(*(_one(w) for w in watches))


OUTAGE_NOTICE_SECS = 600
_blind_since: float | None = None
_outage_notified = False
_outage_title = ""
_outage_label = ""


def _note_reachability(driver, ok: bool) -> None:
    """Feed the outage tracker for drivers that opt in (Driver.tracks_outage): a connectivity
    failure opens the blind window, any answer (even an HTTP/auth error) closes it."""
    global _blind_since, _outage_title, _outage_label
    if not getattr(driver, "tracks_outage", False):
        return
    _outage_title = driver.integration.manifest.title
    _outage_label = driver.outage_label
    if ok:
        _blind_since = None
    elif _blind_since is None:
        _blind_since = time.time()


def _primary_notice_conversation() -> str | None:
    try:
        for c in message_store.list_conversations():
            if not c.get("archived"):
                return c["conversation_id"]
    except Exception:
        log.exception("[watcher] could not pick a conversation for the outage notice")
    return None


async def _check_outage(watch_count: int, now: float | None = None) -> None:
    """One deduplicated notice once every tracked-integration poll has failed for
    OUTAGE_NOTICE_SECS (silent VPN/sleep outages), one on recovery."""
    global _outage_notified
    global _blind_since
    now = time.time() if now is None else now
    if watch_count == 0 and _blind_since is not None:
        _blind_since = None  # nothing left to be blind
    blind = _blind_since
    title = _outage_title or "Integration"
    if blind is not None and not _outage_notified and now - blind >= OUTAGE_NOTICE_SECS:
        _outage_notified = True
        text = (
            f":warning: *{watch_count} {title} watches blind since "
            f"{datetime.fromtimestamp(blind).strftime('%H:%M')}* - every {title} poll is "
            f"failing, likely VPN/sleep. They resume automatically once {title} is reachable."
        )
    elif blind is None and _outage_notified:
        _outage_notified = False
        text = f":white_check_mark: {title} is reachable again - {_outage_label} watches are polling normally."
    else:
        return
    conv = _primary_notice_conversation()
    if conv:
        try:
            await _deliver_system_notice(conv, text)
        except Exception:
            log.exception("[watcher] failed to post %s outage notice", title)


def watch_interval_s(it, kind: str) -> float:
    """Poll interval for an integration kind: per-instance config override, else the manifest."""
    kr = it.kinds[kind]
    if kr.poll_interval_s is not None:
        return float(kr.poll_interval_s)
    return float(kr.spec.watch.poll_interval_s)


def describe_types() -> dict[str, dict]:
    """type -> {emoji, slack_emoji, interval_s, integration(s), builtin} for the settings UI."""
    reg = _integrations()
    out: dict[str, dict] = {}
    for t in valid_types():
        if t in BUILTIN_EMOJI and t not in reg.watch_types():
            out[t] = {"emoji": BUILTIN_EMOJI[t][0], "slack_emoji": BUILTIN_EMOJI[t][1],
                      "interval_s": None, "integration": None, "integrations": [], "intervals": {},
                      "builtin": True}
            continue
        meta = reg.watch_type_meta(t) or {}
        per: dict[str, float] = {}
        for it in reg._active():
            for kr in it.kinds.values():
                if kr.enabled and kr.spec.watch and kr.spec.watch.type == t:
                    per.setdefault(it.name, watch_interval_s(it, kr.name))
        first = next(iter(per), None)
        out[t] = {"emoji": meta.get("emoji"), "slack_emoji": meta.get("slack_emoji"),
                  "interval_s": per[first] if first else None, "integration": first,
                  "integrations": list(per), "intervals": per, "builtin": False}
    return out


BUILTIN_HELP = {
    "time": "key 'in:30m' | 'at:YYYY-MM-DD HH:MM' | 'cron:daily HH:MM' | 'cron:<dow> HH:MM' | 'cron:<5-field>'; label = prompt",
    "script": "key = schedule like time; meta.script from dop-agent/scripts/watch/ (see watch_script)",
    "session_turn": "key 'self' or another conversation_id (fires when that turn ends)",
    "self_restart": "no key; resumes the thread after the bot restarts",
}


def watch_types_doc() -> str:
    """One-line-per-type help (registry key_help + builtin help) for MCP tool docs and WATCHERS.md."""
    reg = _integrations()
    lines = []
    for t in valid_types():
        if t in reg.watch_types():
            help_ = _watch_extra(t).get("key_help") or "see integration docs"
            lines.append(f"{t}: key {help_}")
        else:
            lines.append(f"{t}: {BUILTIN_HELP.get(t, 'builtin')}")
    return "; ".join(lines)


def _unresolved_reason(w: dict, meta: dict) -> tuple[str, bool]:
    """(reason, integration_problem) for an integration-type watch the registry can't resolve."""
    reg = _integrations()
    type_, key = w["type"], (w.get("key") or "").strip()
    pinned = meta.get("integration")
    if type_ in reg.watch_types():
        if pinned and reg.resolve_watch(type_, key) is not None:
            return f"integration {pinned!r} is missing, disabled or invalid (watch pinned via meta.integration)", True
        return _key_error(type_, key), False
    return (f"no enabled integration provides watch type {type_!r} (integration disabled, removed or "
            f"invalid in config.yaml `integrations:`)"), True


async def _poll_integration_watch(w: dict) -> None:
    """Poll one registry-driven watch: resolve (type, key) to an integration instance, run the
    driver's poll (PollResult contract, drivers/base.py) in a worker thread bounded by
    WATCH_POLL_TIMEOUT, persist the new state, then inject events in order and finally apply
    done/broken. The timeout covers only the fetch, never the agent turn an injection starts."""
    key = (w.get("key") or "").strip()
    if not key:
        log.debug("[watcher] %s watch #%s has no key set — skipping", w["type"], w["id"])
        return
    try:
        meta = json.loads(w.get("meta") or "{}")
    except Exception:
        meta = {}
    try:
        last_state = json.loads(w.get("last_state") or "{}")
    except Exception:
        last_state = {}
    reg = _integrations()
    resolved = reg.resolve_watch(w["type"], key, meta.get("integration"))
    if resolved is None:
        reason, integration_problem = _unresolved_reason(w, meta)
        if integration_problem:
            await _report_broken_watch(w, reason, integration_problem=True)
        else:
            await _report_broken_watch(w, reason)
        return
    it, kind, ident = resolved
    try:
        driver = it.driver()
    except Exception:
        log.exception("[watcher] %s watch #%s: driver for %s unavailable", w["type"], w["id"], it.name)
        return
    pmeta = dict(meta)
    timeout = driver.poll_timeout_s or WATCH_POLL_TIMEOUT * 3
    try:
        res = await _fetch_with_timeout(reg.poll, it, kind, ident, last_state, pmeta, w.get("created_at"),
                                        w.get("label"), timeout=timeout)
    except (asyncio.TimeoutError, OSError):
        _note_reachability(driver, False)
        log.warning("[watcher] %s watch #%s: poll timed out/failed on %s", w["type"], w["id"], it.name)
        return
    except Exception:
        log.exception("[watcher] %s watch #%s: poll failed", w["type"], w["id"])
        return
    if res.error:
        _note_reachability(driver, not res.network_error)
        log.warning("[watcher] %s watch #%s: transient poll error: %s", w["type"], w["id"], res.error)
        return
    _note_reachability(driver, True)
    if res.broken:
        await _report_broken_watch(w, res.broken)
        return
    if res.state is not None:
        set_last_state(w["id"], json.dumps(res.state))
    for ev in res.events:
        try:
            await _inject_watch_message(w["conversation_id"], ev.text)
            log.info("[watcher] %s watch #%s fired (%s)", w["type"], w["id"], ev.kind)
        except Exception:
            log.exception("[watcher] failed to inject %s watch #%s %s event", w["type"], w["id"], ev.kind)
    if res.done:
        set_status(w["id"], "done")
        log.info("[watcher] %s watch #%s completed", w["type"], w["id"])


_background_inflight: set[tuple[str, int]] = set()
_conversation_locks: dict[str, asyncio.Lock] = {}


def _conversation_lock_key(w: dict) -> str:
    return w.get("conversation_id") or f"watch-{w['id']}"


def _release_conversation_lock(key: str) -> None:
    """Evict an idle lock so the dict doesn't grow for the process lifetime."""
    lock = _conversation_locks.get(key)
    if lock is not None and not lock.locked() and not getattr(lock, "_waiters", None):
        _conversation_locks.pop(key, None)
_background_tasks: set = set()


def _spawn_background_poll(w: dict, wtype: str, poll_fn) -> None:
    """Run one watch's poll as a background task, at most one per watch at a
    time (a still-running turn is never re-polled/re-fired by later ticks)."""
    key = (wtype, w["id"])
    if key in _background_inflight:
        return
    _background_inflight.add(key)

    async def _run():
        token = _fire_ctx.set((w["id"], wtype))
        lock_key = _conversation_lock_key(w)
        lock = _conversation_locks.setdefault(lock_key, asyncio.Lock())
        try:
            # Same-conversation fires run one after another; a concurrent
            # second turn would interrupt the first.
            async with lock:
                await poll_fn(w)
        except Exception:
            log.exception("[watcher] %s watch #%s poll failed", wtype, w["id"])
        finally:
            _fire_ctx.reset(token)
            _background_inflight.discard(key)
            _release_conversation_lock(lock_key)

    task = asyncio.create_task(_run())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def watcher_loop() -> None:
    """Background poll loop for every non-self_restart watch type (self_restart
    watches are resolved once at startup, see main()).

    Every tick it announces newly registered watches (_announce_new_watches),
    spawns `time`, `script` and `session_turn` watches as background tasks
    (_spawn_background_poll: their fires run whole agent turns, so they must
    not stall the loop; each watch is polled at most once at a time and same-
    conversation turns are serialized), polls integration watches grouped by
    (integration instance, kind) concurrently via _poll_watch_group() once
    each group's own interval has elapsed, runs the outage check, and
    regenerates WATCHERS.md. `time`/`script`/`session_turn` are checked every
    tick since they are local comparisons, not external API calls.
    """
    last_poll_at: dict[tuple[str, str], float] = {}
    while True:
        await asyncio.sleep(WATCHER_POLL_INTERVAL)
        try:
            watches = list_watches()
            active = [w for w in watches if w["status"] == "active"]
            time_watches = [w for w in active if w["type"] == "time"]
            session_turn_watches = [w for w in active if w["type"] == "session_turn"]
            script_watches = [w for w in active if w["type"] == "script"]
            builtin = set(builtin_types())
            integration_watches = [w for w in active if w["type"] not in builtin]

            await _announce_new_watches(watches)

            # time/script/session_turn fires run a whole agent turn (minutes);
            # background tasks keep one long turn from stalling the
            # integration polling below.
            for w in time_watches:
                _spawn_background_poll(w, "time", _poll_time_watch)
            for w in script_watches:
                _spawn_background_poll(w, "script", _poll_script_watch)
            for w in session_turn_watches:
                _spawn_background_poll(w, "session_turn", _poll_session_turn_guarded)

            # Group integration watches by (integration instance, kind): each group has its own
            # poll interval (config override > manifest default). Unresolvable
            # watches (integration disabled/removed) are polled every tick so they get marked broken.
            reg = _integrations()
            groups: dict[tuple[str, str], tuple[float, list[dict]]] = {}
            tracked = 0
            for w in integration_watches:
                try:
                    wmeta = json.loads(w.get("meta") or "{}")
                except Exception:
                    wmeta = {}
                r = reg.resolve_watch(w["type"], (w.get("key") or "").strip(), wmeta.get("integration"))
                if r is None:
                    gkey, interval = ("", w["type"]), 0.0
                else:
                    it, kind, _ident = r
                    gkey, interval = (it.name, kind), watch_interval_s(it, kind)
                    try:
                        tracked += bool(it.driver().tracks_outage)
                    except Exception:
                        pass
                groups.setdefault(gkey, (interval, []))[1].append(w)
            for gkey, (interval, group) in groups.items():
                if time.time() - last_poll_at.get(gkey, 0.0) >= interval:
                    last_poll_at[gkey] = time.time()
                    await _poll_watch_group(_poll_integration_watch, group, group[0]["type"])

            await _check_outage(tracked)

            await _write_watchers_md(list_watches(include_inactive=True))
        except Exception:
            log.exception("[watcher] tick failed")
