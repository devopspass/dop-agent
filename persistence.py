"""
Session ID persistence, user-visible message mirroring (bot_store.db via
message_store), and crash-safe "active status" / "pending transcription"
sweep files.

Depends only on message_store (the sqlite
store) and bot_config.log (shared logger) — no dependency on dop_agent.py
itself, so it can be imported freely by other modules (handlers,
crash-recovery, etc.) without circularity.
"""

from __future__ import annotations

import json
from pathlib import Path

import message_store
from message_ref import MessageRef
from bot_config import log


# ---------------------------------------------------------------------------
# Session ID persistence + user-visible message mirror (bot_store.db)
# ---------------------------------------------------------------------------

def _get_saved_session_id(conversation_id: str) -> str | None:
    return message_store.get_copilot_session_id(conversation_id)


def _set_saved_session_id(conversation_id: str, session_id: str | None) -> None:
    message_store.set_copilot_session_id(conversation_id, session_id)


def _slack_external_ref(channel: str, ts: str | None) -> str | None:
    if not channel or not ts:
        return None
    return message_store.slack_external_ref(channel, ts)


def _message_id_for_slack_message(
    conversation_id: str | None,
    channel: str | None,
    ts: str | None,
) -> str | None:
    if not conversation_id:
        return None
    external_ref = _slack_external_ref(channel or "", ts)
    if not external_ref:
        return None
    return message_store.find_message_id_by_external_ref(conversation_id, external_ref)


def _record_visible_message(
    conversation_id: str | None,
    role: str,
    content: str,
    *,
    message_id: str | None = None,
    channel: str | None = None,
    ts: str | None = None,
    created_at: str | None = None,
) -> str | None:
    if not conversation_id:
        return None
    return message_store.record_message(
        conversation_id,
        role,
        content,
        message_id=message_id,
        external_ref=_slack_external_ref(channel or "", ts),
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# Active "Working..." status message tracking (survives a crash/restart)
# ---------------------------------------------------------------------------
# While a turn is in progress, _handle() records its live status message
# (channel+ts) here. It's cleared the moment that turn finishes (success,
# error, or interrupted) — so under normal operation this file is empty.
# If the bot process dies mid-turn, the entry is left behind; on the next
# startup we sweep leftovers and replace each stale
# ":thinking_face: Working..." placeholder with an "interrupted" message
# instead of leaving it stuck with a dead Stop button.
ACTIVE_STATUS_FILE = Path(__file__).parent / "active_status.json"


def _load_active_status() -> dict[str, dict]:
    if not ACTIVE_STATUS_FILE.exists():
        return {}
    try:
        return _migrate_side_file_keys(json.loads(ACTIVE_STATUS_FILE.read_text()), "ts")
    except Exception as exc:
        log.warning("Could not read %s: %s", ACTIVE_STATUS_FILE, exc)
        return {}


def _save_active_status(mapping: dict[str, dict]) -> None:
    try:
        message_store.atomic_write_json(ACTIVE_STATUS_FILE, mapping)
    except Exception as exc:
        log.warning("Could not write %s: %s", ACTIVE_STATUS_FILE, exc)


def _slack_ref_key(channel: str, ts: str) -> str:
    return MessageRef(message_store.SLACK_PLATFORM, channel, ts).key()


def _migrate_side_file_keys(mapping: dict[str, dict], ts_field: str) -> dict[str, dict]:
    """Re-key bare ``"<channel>:<ts>"`` entries to ``MessageRef.key()`` so
    crash recovery, which reads these on startup, finds them."""
    out: dict[str, dict] = {}
    for key, entry in mapping.items():
        if (not key.startswith(f"{message_store.SLACK_PLATFORM}:") and isinstance(entry, dict)
                and not entry.get("platform") and entry.get("channel") and entry.get(ts_field)):
            key = _slack_ref_key(entry["channel"], entry[ts_field])
        out[key] = entry
    return out


def _status_key(platform: str, conversation_key: str, ts: str) -> str:
    return MessageRef(platform, conversation_key, ts).key()


def _register_active_status(channel: str, ts: str, thread_ts: str | None, msg_ts: str | None = None, *,
                            platform: str = message_store.SLACK_PLATFORM,
                            thread_key: dict | None = None, conversation_id: str | None = None,
                            message_id: str | None = None) -> None:
    """Mark an in-flight "Working..." status message so a restart can find it.

    ``channel`` is the platform's conversation key (a Slack channel id, a
    Telegram chat id, ...). Slack entries hold only channel/ts/thread_ts/
    msg_ts; entries for any other platform also carry ``platform``/``thread_key``/
    ``conversation_id``/``message_id`` so recovery can go through that
    platform's adapter instead of assuming Slack."""
    with message_store._json_write_lock:
        _register_active_status_locked(channel, ts, thread_ts, msg_ts, platform, thread_key, conversation_id, message_id)


def _register_active_status_locked(channel, ts, thread_ts, msg_ts, platform, thread_key, conversation_id, message_id):
    mapping = _load_active_status()
    entry = {"channel": channel, "ts": ts, "thread_ts": thread_ts, "msg_ts": msg_ts}
    if platform != message_store.SLACK_PLATFORM:
        entry.update(platform=platform, thread_key=thread_key or {}, conversation_id=conversation_id,
                     message_id=message_id)
    mapping[_status_key(platform, channel, ts)] = entry
    _save_active_status(mapping)


def _clear_active_status(channel: str, ts: str, *, platform: str = message_store.SLACK_PLATFORM) -> None:
    with message_store._json_write_lock:
        mapping = _load_active_status()
        if mapping.pop(_status_key(platform, channel, ts), None) is not None:
            _save_active_status(mapping)


# Same pattern as ACTIVE_STATUS_FILE above, but for an in-flight Whisper
# transcription (the ":headphones: Transcribing..." placeholder). Nothing else
# watches an interrupted transcription, so on startup we sweep stale entries
# and re-run it from scratch (Slack's url_private_download stays valid, so
# re-fetching the audio is safe).
PENDING_TRANSCRIPTIONS_FILE = Path(__file__).parent / "pending_transcriptions.json"


def _load_pending_transcriptions() -> dict[str, dict]:
    if not PENDING_TRANSCRIPTIONS_FILE.exists():
        return {}
    try:
        return _migrate_side_file_keys(json.loads(PENDING_TRANSCRIPTIONS_FILE.read_text()), "msg_ts")
    except Exception as exc:
        log.warning("Could not read %s: %s", PENDING_TRANSCRIPTIONS_FILE, exc)
        return {}


def _save_pending_transcriptions(mapping: dict[str, dict]) -> None:
    try:
        message_store.atomic_write_json(PENDING_TRANSCRIPTIONS_FILE, mapping)
    except Exception as exc:
        log.warning("Could not write %s: %s", PENDING_TRANSCRIPTIONS_FILE, exc)


def _register_pending_transcription(channel: str, msg_ts: str, *, thread_ts: str | None,
                                      conversation_id: str | None, user_id: str | None,
                                      file_info: dict, message_id: str | None = None) -> None:
    with message_store._json_write_lock:
        mapping = _load_pending_transcriptions()
        mapping[_slack_ref_key(channel, msg_ts)] = {
            "channel": channel, "msg_ts": msg_ts, "thread_ts": thread_ts,
            "conversation_id": conversation_id, "user_id": user_id, "file": file_info,
            "message_id": message_id,
        }
        _save_pending_transcriptions(mapping)


def _clear_pending_transcription(channel: str, msg_ts: str) -> None:
    with message_store._json_write_lock:
        mapping = _load_pending_transcriptions()
        if mapping.pop(_slack_ref_key(channel, msg_ts), None) is not None:
            _save_pending_transcriptions(mapping)
