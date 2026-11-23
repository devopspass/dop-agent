"""
Crash recovery: reading back a lost final answer from the Copilot
runtime's own durable event log.

Depends only on bot_config (SESSION_STATE_DIR, log) and message_store. The
orchestration that resumes interrupted turns lives in dop_agent.py.
"""

from __future__ import annotations

import json
import uuid

import message_store
from bot_config import SESSION_STATE_DIR, log


# The Copilot runtime durably appends every session event (including each
# assistant.message) to ``~/.copilot/session-state/<session_id>/events.jsonl``
# before our process mirrors the final answer into bot_store.db. If the bot
# dies in that window the answer is still on disk; this reads it back
# (read-only, best-effort).
def _find_last_assistant_message_after(session_id: str | None, after_iso: str | None) -> str | None:
    """Best-effort scan of the Copilot session's own events.jsonl for the
    last ``assistant.message`` event with a non-empty ``content`` recorded
    strictly after ``after_iso`` (an ISO-8601 timestamp, e.g. our own
    message_store ``created_at``/``updated_at`` format). Returns None if the
    session/file doesn't exist, can't be read, or no such event is found —
    callers should fall back to the plain "interrupted" message in that case."""
    if not session_id:
        return None
    try:
        session_id = str(uuid.UUID(str(session_id)))
    except ValueError:
        log.warning("Ignoring non-UUID session id for crash recovery: %r", session_id)
        return None
    path = SESSION_STATE_DIR / session_id / "events.jsonl"
    try:
        if not path.resolve().is_relative_to(SESSION_STATE_DIR.resolve()):
            return None
    except OSError:
        return None
    if not path.exists():
        return None
    last_content: str | None = None
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("type") != "assistant.message":
                    continue
                ts = obj.get("timestamp")
                if after_iso and ts and str(ts) <= after_iso:
                    continue
                data = obj.get("data") or {}
                content = data.get("content") if isinstance(data, dict) else None
                if content:
                    last_content = str(content)
    except OSError as exc:
        log.debug("Could not read events.jsonl for session %s: %s", session_id, exc)
        return None
    return last_content.strip() if last_content else None


def _recover_lost_response(conversation_id: str | None) -> str | None:
    """If ``conversation_id``'s Copilot session already produced a final
    answer that our own process never got to persist (see
    _find_last_assistant_message_after's docstring), return it. Uses this
    conversation's last recorded message (expected to be the stale ``status``
    placeholder) as the "after" boundary, so only genuinely new content
    counts as recovered — never re-surfaces an old answer."""
    if not conversation_id:
        return None
    try:
        session_id = message_store.get_copilot_session_id(conversation_id)
        messages = message_store.get_messages(conversation_id)
    except Exception:
        log.debug("Could not look up session/messages for recovery of %s", conversation_id, exc_info=True)
        return None
    if not session_id or not messages:
        return None
    boundary = messages[-1].get("created_at")
    return _find_last_assistant_message_after(session_id, boundary)
