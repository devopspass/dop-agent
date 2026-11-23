"""Pure, testable business logic backing the bot's in-process MCP server.

The MCP transport wiring (tool registration/descriptions, auth middleware,
mounting at ``/mcp``) lives in ``web_server.py``; this module has no
``mcp``/``fastapi`` imports so it can be unit-tested directly.

Tool families:

- ``watch_*`` - thin wrappers around ``watchers.py`` (add/list/get/pause/
  resume/remove/pause_session, plus script watches).
- ``session_*`` - read-only search/lookup across BOTH the global Copilot CLI
  session store (``~/.copilot/session-store.db``: turns, checkpoints,
  session_files, session_refs for every local Copilot session) and this bot's
  own store (``bot_store.db`` via ``message_store.py``). Results are annotated
  with ``is_bot_session``, ``slack_key`` and ``bot_title`` by joining on
  ``conversations.copilot_session_id``. ``session_get`` also returns ``files``
  and ``refs``.
- ``group_manage`` - session-group mutations.

The global store is opened read-only (owned by the Copilot CLI runtime). The
``session_create`` tool lives in ``web_server.py`` because it spawns a live
turn and needs that module's runtime state (``_handle_fn``, ``_broadcaster``,
``_slack_client``).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import message_store
import watch_scripts
import watchers

# Global Copilot CLI session store (every local session, not just the bot's).
COPILOT_SESSION_STORE = Path.home() / ".copilot" / "session-store.db"


def _session_store_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{COPILOT_SESSION_STORE}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _snippet(content: str, query: str, radius: int = 60) -> str:
    """Short excerpt of ``content`` centered on the first case-insensitive
    occurrence of ``query`` (same behavior as ``message_store._search_snippet``)."""
    if not content:
        return ""
    lower_content = content.lower()
    idx = lower_content.find(query.lower())
    if idx == -1:
        snippet = content[: 2 * radius].replace("\n", " ").strip()
        return snippet + ("…" if len(content) > 2 * radius else "")
    start = max(0, idx - radius)
    end = min(len(content), idx + len(query) + radius)
    snippet = content[start:end].replace("\n", " ").strip()
    if start > 0:
        snippet = "…" + snippet
    if end < len(content):
        snippet = snippet + "…"
    return snippet


def _bot_session_index() -> dict[str, dict[str, Any]]:
    """Map ``copilot_session_id`` -> bot conversation summary, for
    annotating session_search/session_get results with
    is_bot_session/slack_key/bot_title without a query per row."""
    index: dict[str, dict[str, Any]] = {}
    for convo in message_store.list_conversations():
        sid = convo.get("session_id")
        if sid:
            index[str(sid)] = convo
    return index


# ---------------------------------------------------------------------------
# watch_* tools
# ---------------------------------------------------------------------------

def watch_add(
    type: str,
    session_id: str,
    key: str | None = None,
    label: str | None = None,
    meta: dict | None = None,
) -> dict:
    """Register a new watch. Returns ``{"watch_id": <id>}``."""
    watch_id = watchers.add_watch(type, session_id, key=key, label=label, meta=meta)
    return {"watch_id": watch_id}


def watch_script(
    session_id: str,
    script: str,
    schedule: str,
    prompt: str,
    args: list[str] | None = None,
    retries: int = 0,
    until_changed: bool = False,
    max_runs: int | None = None,
    expires_in: str | None = None,
    run_immediately: bool = True,
    dry_run: bool = False,
    watcher: str | None = None,
    integration: str | None = None,
) -> dict:
    """Register a `script` watch (or, with dry_run=True, just run the script once
    and report what it would do). Exit-code contract: 0 noop, 200 changed (wake),
    other = error (wake + stop after `retries` consecutive failures).
    `watcher` = a package-driver recipe id (e.g. teams-mentions; see that integration's skill): sets the icon in
    the watchers list and prepends the recipe's leading args; `integration` pins the instance (account)."""
    meta: dict[str, Any] = {"script": script, "args": args or [], "retries": retries,
                            "until_changed": until_changed, "run_immediately": run_immediately}
    if max_runs is not None:
        meta["max_runs"] = max_runs
    if expires_in:
        meta["expires_in"] = expires_in
    if watcher:
        meta["watcher"] = watcher
    if integration:
        meta["integration"] = integration
    meta = watch_scripts.apply_watcher(meta)
    watch_scripts.validate_script_watch_meta(meta)
    if dry_run:
        result = watch_scripts.run_script_sync(meta, extra_env=watch_scripts.instance_env(meta))
        rc = result["rc"]
        outcome = ("noop" if rc == watch_scripts.RC_NOOP else
                   "changed" if rc == watch_scripts.RC_CHANGED else "error")
        if outcome != "error":
            watch_scripts.mark_dry_run_ok(script)
        return {"dry_run": True, "rc": rc, "outcome": outcome, "timed_out": result["timed_out"],
                "stdout": result["stdout"][:2000], "stderr": result["stderr"][-1000:],
                "hint": ("Looks fine - register it with dry_run=False." if outcome != "error"
                         else "Script failed - fix it (exit 1/other = error) before registering.")}
    watch_scripts.require_dry_run(script)
    watch_id = watchers.add_watch("script", session_id, key=schedule, label=prompt, meta=meta)
    return {"watch_id": watch_id}


_PROTECTED_WATCH_IDS = frozenset(range(251, 256))  # standing daily-review cron watches
WATCH_COMPACT_FIELDS = ("id", "type", "key", "label", "status", "session_id", "next_fire_at", "last_state", "broken_error")


def _compact_watch(w: dict) -> dict:
    next_fire = None
    meta = w.get("meta")
    if meta:
        try:
            next_fire = (json.loads(meta) if isinstance(meta, str) else meta).get("next_fire_at")
        except (ValueError, AttributeError):
            pass
    out = {k: w.get(k) for k in WATCH_COMPACT_FIELDS if k not in ("next_fire_at", "session_id")}
    out["session_id"] = w.get("conversation_id")
    out["broken_error"] = w.get("broken_error")
    out["next_fire_at"] = next_fire
    for k, n in (("label", 80), ("last_state", 60)):
        if isinstance(out.get(k), str) and len(out[k]) > n:
            out[k] = out[k][: n - 1] + "…"
    return {k: v for k, v in out.items() if v is not None}


def watch_list(
    session_id: str | None = None,
    include_inactive: bool = False,
    compact: bool = False,
    fields: list[str] | None = None,
    limit: int | None = None,
) -> list[dict]:
    """List watches, optionally scoped to one session / including done ones.
    Active/paused watches whose key/meta no longer validates get a
    ``broken_error`` field.

    ``compact=True`` returns slim rows (id,type,key,label[:80],status,
    session_id,next_fire_at,last_state[:60]; empty values omitted);
    ``fields`` picks explicit columns from the full row instead; ``limit``
    caps the row count. Defaults return full rows."""
    rows = watchers.list_watches(conversation_id=session_id, include_inactive=include_inactive)
    for w in rows:
        err = watchers.check_watch(w)
        if err:
            w["broken_error"] = err
    if limit is not None:
        rows = rows[: max(limit, 0)]
    if fields:
        return [{k: w.get(k) for k in fields} for w in rows]
    if compact:
        return [_compact_watch(w) for w in rows]
    return rows


def watch_get(watch_id: int) -> dict | None:
    """Get full detail for one watch by id, or None if it doesn't exist."""
    return watchers.get_watch(watch_id)


def _as_ids(ids: int | list[int]) -> list[int]:
    return [ids] if isinstance(ids, int) else list(ids)


def watch_pause(ids: int | list[int], force: bool = False) -> dict:
    """Pause one or several watches (keeps the rows). Standing cron watches
    (key ``cron:*`` / ids 251-255) are skipped unless ``force``. Returns
    ``{"done": [ids], "skipped": [{"id", "reason"}]}``."""
    return _watch_batch(_as_ids(ids), lambda i: watchers.set_status(i, "paused"), force)


def watch_resume(ids: int | list[int]) -> dict:
    """Resume one or several paused watches. Returns
    ``{"done": [ids], "skipped": [{"id", "reason"}]}``."""
    return _watch_batch(_as_ids(ids), lambda i: watchers.set_status(i, "active"), force=True)


def watch_remove(ids: int | list[int], force: bool = False) -> dict:
    """Permanently delete one or several watches. Standing cron watches
    (key ``cron:*`` / ids 251-255) are skipped unless ``force``. Returns
    ``{"done": [ids], "skipped": [{"id", "reason"}]}``."""
    return _watch_batch(_as_ids(ids), watchers.remove_watch, force)


def _is_protected_watch(w: dict | None) -> bool:
    return bool(w) and (w["id"] in _PROTECTED_WATCH_IDS or str(w.get("key") or "").startswith("cron:"))


def _watch_batch(ids: list[int], action, force: bool) -> dict:
    done: list[int] = []
    skipped: list[dict] = []
    for watch_id in ids:
        w = watchers.get_watch(watch_id)
        if w is None:
            skipped.append({"id": watch_id, "reason": "not found"})
        elif _is_protected_watch(w) and not force:
            skipped.append({"id": watch_id, "reason": "protected cron watch (pass force=True to override)"})
        elif action(watch_id):
            done.append(watch_id)
        else:
            skipped.append({"id": watch_id, "reason": "no change"})
    return {"done": done, "skipped": skipped}


def watch_pause_session(session_id: str) -> dict:
    """Pause every active watch registered by a session. Returns
    ``{"paused_count": int}``."""
    return {"paused_count": watchers.pause_session(session_id)}


# ---------------------------------------------------------------------------
# session_* tools
# ---------------------------------------------------------------------------

def session_search(query: str, limit: int = 20) -> list[dict]:
    """Search past Copilot session content — both raw CLI turns (across
    every local session, bot-related or not) and this bot's rendered Slack
    messages — for a keyword/topic. Returns matches newest-first, each
    annotated with is_bot_session/slack_key/bot_title when applicable."""
    query = (query or "").strip()
    if not query:
        return []
    bot_index = _bot_session_index()
    results: list[dict[str, Any]] = []

    if COPILOT_SESSION_STORE.exists():
        like = f"%{query}%"
        with _session_store_conn() as conn:
            rows = conn.execute(
                """
                SELECT t.session_id, t.turn_index, t.user_message, t.assistant_response,
                       t.timestamp, s.cwd, s.repository, s.summary
                FROM turns t
                JOIN sessions s ON s.id = t.session_id
                WHERE t.user_message LIKE ? OR t.assistant_response LIKE ?
                ORDER BY t.timestamp DESC
                LIMIT ?
                """,
                (like, like, limit),
            ).fetchall()
        for r in rows:
            sid = str(r["session_id"])
            bot = bot_index.get(sid)
            text = r["user_message"] if r["user_message"] and query.lower() in r["user_message"].lower() else r["assistant_response"]
            results.append(
                {
                    "source": "cli_turn",
                    "session_id": sid,
                    "turn_index": r["turn_index"],
                    "timestamp": r["timestamp"],
                    "cwd": r["cwd"],
                    "repository": r["repository"],
                    "summary": r["summary"],
                    "snippet": _snippet(text or "", query),
                    "is_bot_session": bot is not None,
                    "slack_key": bot["slack_key"] if bot else None,
                    "bot_title": bot["title"] if bot else None,
                }
            )

    for m in message_store.search_messages(query, limit=limit):
        convo = message_store.get_conversation(m["conversation_id"])
        sid = convo.get("copilot_session_id") if convo else None
        results.append(
            {
                "source": "bot_message",
                "session_id": sid,
                "conversation_id": m["conversation_id"],
                "timestamp": m["created_at"],
                "snippet": m["snippet"],
                "is_bot_session": True,
                "slack_key": message_store.get_primary_binding(convo["id"], message_store.SLACK_PLATFORM) if convo else None,
                "bot_title": m["title"],
            }
        )

    results.sort(key=lambda r: r.get("timestamp") or "", reverse=True)
    return results[:limit]


def session_get(
    session_id: str,
    compact: bool = False,
    last_turns: int = 3,
    sections: list[str] | None = None,
    max_chars: int = 1500,
) -> dict | None:
    """Full detail for one Copilot CLI session found via session_search:
    every turn + checkpoint + touched file + extracted reference, plus (if
    it's a bot session) the rendered Slack messages and thread key. Returns
    None if the session id doesn't exist in the local CLI store.

    ``files``/``refs`` come from the ``session_files``/``session_refs``
    tables (file paths touched and ticket/MR/URL references extracted
    during the session). Older/foreign session stores may predate these
    tables; an ``sqlite3.OperationalError`` there falls back to ``[]``.

    ``compact=True`` (what the MCP tool uses by default) trims the payload:
    only the last ``last_turns`` turns, each text field cut to ``max_chars``;
    checkpoints reduced to number/title; files de-duplicated by path;
    refs de-duplicated; at most the last 6 bot messages, each cut to
    500 chars. ``sections`` (subset of turns/checkpoints/files/refs/
    messages) limits which collections are returned. A ``truncated`` key
    describes what was cut. ``compact=False`` returns everything.
    """
    if not COPILOT_SESSION_STORE.exists():
        return None
    with _session_store_conn() as conn:
        srow = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not srow:
            # Also accept a bot conversation_id, which can differ from the CLI session id.
            mapped = message_store.get_conversation(session_id)
            mapped_id = mapped.get("copilot_session_id") if mapped else None
            if mapped_id and mapped_id != session_id:
                srow = conn.execute("SELECT * FROM sessions WHERE id = ?", (mapped_id,)).fetchone()
                if srow:
                    session_id = mapped_id
        if not srow:
            return None
        turns = [
            dict(r)
            for r in conn.execute(
                "SELECT turn_index, user_message, assistant_response, timestamp "
                "FROM turns WHERE session_id = ? ORDER BY turn_index",
                (session_id,),
            ).fetchall()
        ]
        checkpoints = [
            dict(r)
            for r in conn.execute(
                "SELECT checkpoint_number, title, overview, work_done, next_steps, created_at "
                "FROM checkpoints WHERE session_id = ? ORDER BY checkpoint_number",
                (session_id,),
            ).fetchall()
        ]
        try:
            files = [
                dict(r)
                for r in conn.execute(
                    "SELECT file_path, tool_name, turn_index, first_seen_at "
                    "FROM session_files WHERE session_id = ? ORDER BY first_seen_at",
                    (session_id,),
                ).fetchall()
            ]
        except sqlite3.OperationalError:
            files = []
        try:
            refs = [
                dict(r)
                for r in conn.execute(
                    "SELECT ref_type, ref_value, turn_index, created_at "
                    "FROM session_refs WHERE session_id = ? ORDER BY created_at",
                    (session_id,),
                ).fetchall()
            ]
        except sqlite3.OperationalError:
            refs = []

    result: dict[str, Any] = dict(srow)
    result["turns"] = turns
    result["checkpoints"] = checkpoints
    result["files"] = files
    result["refs"] = refs

    bot = _bot_session_index().get(str(session_id))
    result["is_bot_session"] = bot is not None
    result["slack_key"] = bot.get("slack_key") if bot else None
    result["bot_title"] = bot.get("title") if bot else None
    result["model"] = bot.get("model") if bot else None
    result["model_reason"] = bot.get("model_reason") if bot else None
    result["bot_messages"] = message_store.get_messages(bot["conversation_id"]) if bot else []
    if compact:
        return _compact_session(result, session_id, last_turns, sections, max_chars)
    return result


def _cut(text: Any, n: int) -> Any:
    if isinstance(text, str) and len(text) > n:
        return text[:n] + f"… [+{len(text) - n} chars]"
    return text


def _compact_session(result: dict, session_id: str, n_turns: int, sections: list[str] | None, max_chars: int) -> dict:
    want = set(sections) if sections else {"turns", "checkpoints", "files", "refs", "messages"}
    totals = {
        "turns": len(result["turns"]), "checkpoints": len(result["checkpoints"]),
        "files": len(result["files"]), "refs": len(result["refs"]), "messages": len(result["bot_messages"]),
    }
    out: dict[str, Any] = {k: v for k, v in result.items()
                           if k not in ("turns", "checkpoints", "files", "refs", "bot_messages")}
    if "turns" in want:
        shown = result["turns"][-n_turns:] if n_turns > 0 else []
        out["turns"] = [{k: _cut(v, max_chars) for k, v in t.items()} for t in shown]
    if "checkpoints" in want:
        out["checkpoints"] = [
            {"checkpoint_number": c.get("checkpoint_number"), "title": c.get("title")} for c in result["checkpoints"]
        ]
    if "files" in want:
        out["files"] = sorted({f["file_path"] for f in result["files"]})[:50]
    if "refs" in want:
        out["refs"] = sorted({(r["ref_type"], r["ref_value"]) for r in result["refs"]})[:50]
        out["refs"] = [{"ref_type": a, "ref_value": b} for a, b in out["refs"]]
    if "messages" in want:
        out["bot_messages"] = [
            {k: _cut(v, min(max_chars, 500)) for k, v in m.items()} for m in result["bot_messages"][-6:]
        ]
    out["truncated"] = {
        "totals": totals,
        "turns_shown": len(out.get("turns", [])),
        "hint": (
            "compact view; pass full=True (or sections=[...], turns=N, max_chars=N) for more. "
            f"Raw data on disk: ~/.copilot/session-state/{session_id}/events.jsonl "
            "(full event log - grep it for exact text, tool calls, outputs), "
            f"checkpoints/ and files/ in the same dir; global index ~/.copilot/session-store.db "
            "(sqlite: sessions/turns/checkpoints/session_files/session_refs)."
        ),
    }
    return out


SESSION_COMPACT_FIELDS = ("conversation_id", "session_id", "title", "updated_at", "group_id", "running", "pinned", "archived", "model")


def session_list(
    limit: int = 50, include_archived: bool = False, group_id: str | None = None, compact: bool = False
) -> list[dict]:
    """List conversations (Slack thread, title, copilot_session_id,
    pinned/archived, last activity) for browsing without a search keyword."""
    convos = message_store.list_conversations()
    if not include_archived:
        convos = [c for c in convos if not c.get("archived")]
    if group_id is not None:
        wanted = None if group_id == "none" else group_id
        convos = [c for c in convos if c.get("group_id") == wanted]
    convos = convos[:limit]
    if compact:
        convos = [{k: c.get(k) for k in SESSION_COMPACT_FIELDS if c.get(k) not in (None, False)} for c in convos]
    return convos


# ---------------------------------------------------------------------------
# group_manage (create/update/assign/set_master/archive/delete in one tool)
# ---------------------------------------------------------------------------

GROUP_ACTIONS = ("create", "update", "assign", "set_master", "archive", "delete")


def _require_group(group_id: str | None) -> None:
    if not group_id or message_store.get_group(group_id) is None:
        raise ValueError(f"group_id {group_id!r} does not exist - list groups with group_list() or create one with group_manage(action='create', title=...)")


def group_manage(
    action: str,
    group_id: str | None = None,
    session_id: str | None = None,
    title: str | None = None,
    color: str | None = None,
    emoji: str | None = None,
    description: str | None = None,
    make_master: bool = False,
    archived: bool = True,
) -> dict:
    """Session-group mutations; see the MCP tool description for the
    per-action argument table. Raises ValueError (with a fix hint) on bad input."""
    if action == "create":
        if not title:
            raise ValueError("action='create' needs title")
        return message_store.create_group(title, color, emoji, description)
    if action == "update":
        _require_group(group_id)
        return message_store.update_group(group_id, title=title, color=color, emoji=emoji, description=description)
    if action == "assign":
        if not session_id or message_store.get_conversation(session_id) is None:
            raise ValueError(f"session_id {session_id!r} does not refer to an existing session - find ids with session_list()")
        if group_id is not None:
            _require_group(group_id)
        message_store.assign_session_group(session_id, group_id)
        if make_master and group_id is not None:
            message_store.set_group_master(group_id, session_id)
        return {"conversation_id": session_id, "group_id": group_id, "is_master": bool(make_master and group_id)}
    if action == "set_master":
        _require_group(group_id)
        message_store.set_group_master(group_id, session_id)
        return {"group_id": group_id, "master_id": session_id}
    if action == "archive":
        _require_group(group_id)
        return {"group_id": group_id, "archived": archived, "sessions": message_store.set_group_archived(group_id, archived)}
    if action == "delete":
        _require_group(group_id)
        return {"group_id": group_id, "unassigned": message_store.delete_group(group_id)}
    raise ValueError(f"unknown action {action!r} - use one of {', '.join(GROUP_ACTIONS)}")
