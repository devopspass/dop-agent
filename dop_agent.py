#!/usr/bin/env python3
"""
DOP Agent: a Slack, Telegram and Web UI front end that drives a persistent GitHub Copilot SDK session.

Entry point and wiring only: builds the Slack app, registers handlers, runs crash
recovery for turns/transcriptions cut off by a restart, then starts the Web UI,
watchers and Socket Mode. Each conversation (Slack thread, Telegram chat/topic or
Web UI session) has its own long-lived CopilotSession (see copilot_session.py).

Setup: copy .env.example to .env, `pip install -r requirements.txt`, run
`python3 dop_agent.py`.
"""

import asyncio
import os

import config

import watchers

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
# bot_config must be imported before web_server: it triggers load_dotenv() as a
# side effect, and web_server reads its WEB_UI_* env vars at import time.

import message_store  # noqa: E402
from bot_config import (  # noqa: E402
    ALLOWED_USER_ID,
    COPILOT_WORKDIR,
    SESSION_TIMEOUT,
    SLACK_APP_TOKEN,
    SLACK_BOT_TOKEN,
    log,
)
import web_server  # noqa: E402  (must import after bot_config, see above)

# ---------------------------------------------------------------------------
# Persistence: crash-safe "active status" / "pending transcription" sweep files
# ---------------------------------------------------------------------------

from persistence import (  # noqa: E402
    _load_active_status,
    _load_pending_transcriptions,
    _message_id_for_slack_message,
    _save_active_status,
    _save_pending_transcriptions,
)


# ---------------------------------------------------------------------------
# Copilot session registry
# ---------------------------------------------------------------------------

from copilot_session import (  # noqa: E402
    _conversation_id_for,
    _convos,
)

# ---------------------------------------------------------------------------
# Message handling. handlers.register(app) below attaches the @app.action/
# @app.event decorators to our AsyncApp; handlers.py cannot import `app` from
# here without a circular import.
# ---------------------------------------------------------------------------

import handlers  # noqa: E402
from handlers import _handle, _transcribe_and_finalize, is_authorized  # noqa: E402
import adapters  # noqa: E402
from adapters import SlackAdapter  # noqa: E402


def _slack_adapter():
    """Live Slack adapter from the registry (falls back to wrapping ``app.client``)."""
    return adapters.REGISTRY.slack_for(app.client)


from watchers import (  # noqa: E402
    _fire_self_restart_watches,
    _inject_watch_message,
    watcher_loop,
)
import telegram_boot  # noqa: E402
import watchers as _watchers_module  # noqa: E402

# ---------------------------------------------------------------------------
# Slack App
# ---------------------------------------------------------------------------

# Disabled (adapters.slack.enabled=false or no tokens) runs Web-UI-only (no Socket Mode, no startup ping).
SLACK_DISABLED = (
    not config.value("adapters.slack.enabled")
    or not (SLACK_BOT_TOKEN and SLACK_APP_TOKEN)
)

if SLACK_DISABLED:
    app = None
else:
    from slack_bolt.async_app import AsyncApp

    app = AsyncApp(token=SLACK_BOT_TOKEN)
    handlers.register(app)


# ---------------------------------------------------------------------------
# Crash recovery: reading back a lost final answer from the Copilot event log
# ---------------------------------------------------------------------------

from crash_recovery import _recover_lost_response  # noqa: E402


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _resume_interrupted_turns() -> None:
    """Replace any ":thinking_face: Working..." placeholder left behind by a turn that
    was still in progress when the bot process died with an "interrupted"
    message (or the recovered final answer), then automatically resume the thread
    the same way a self_restart watch does.

    The active-status file is the source of truth for "which threads were running":
    written when a turn starts and cleared when it finishes. The original message is
    deliberately NOT re-sent verbatim (that could re-trigger a half-applied tool
    chain); instead watchers.build_resume_instruction() quotes the last user request
    and last (possibly cut-off) assistant message and tells the agent to finish the work.

    Each thread resumes as its own background task so interrupted threads run in
    parallel and none blocks the bot from accepting new messages."""
    stale = _load_active_status()
    handled_conversation_ids: set[str] = set()
    if stale:
        log.info("Found %d stale 'Working...' status message(s) from a previous run — "
                  "marking interrupted and auto-resuming", len(stale))
    _save_active_status({})  # claim all entries up front; nothing else touches this file yet at startup
    for key, entry in stale.items():
        platform = entry.get("platform") or message_store.SLACK_PLATFORM
        is_slack = platform == message_store.SLACK_PLATFORM
        if is_slack and SLACK_DISABLED:
            log.warning("[interrupted-resume] Slack is disabled — leaving Slack status %s untouched", key)
            continue
        adapter = _slack_adapter() if is_slack else adapters.REGISTRY.get(platform)
        if adapter is None:
            log.warning("[interrupted-resume] No adapter registered for platform %r — cannot recover %s", platform, key)
            continue
        channel = entry.get("channel")
        thread_key = entry.get("thread_key") or {"channel": channel}
        resume_thread_ts = entry.get("thread_ts") or entry.get("ts")
        conversation_id = entry.get("conversation_id") or (
            _conversation_id_for(channel, resume_thread_ts) if is_slack and resume_thread_ts else None)
        if conversation_id:
            handled_conversation_ids.add(conversation_id)
        status_message_id = entry.get("message_id") or (
            _message_id_for_slack_message(conversation_id, channel, entry["ts"]) if is_slack else None)

        # The Copilot session may already have produced its final answer before
        # this process died; if so, post it instead of the "interrupted" notice.
        recovered_text = _recover_lost_response(conversation_id)
        recovered = False
        try:
            if recovered_text:
                await adapters.deliver_update(adapter,
                    thread_key,
                    entry["ts"],
                    recovered_text,
                    conversation_id=conversation_id,
                    role="assistant",
                    message_id=status_message_id,
                    buttons=[],
                )
                log.info("[interrupted-resume] Recovered lost final answer for %s from events.jsonl", key)
                recovered = True
            else:
                interrupted_text = ":black_square_for_stop: _Interrupted — bot restarted before this finished. Resuming..._"
                await adapters.deliver_update(adapter,
                    thread_key,
                    entry["ts"],
                    interrupted_text,
                    conversation_id=conversation_id,
                    role="system",
                    message_id=status_message_id,
                    buttons=[],
                )
        except Exception as exc:
            log.warning("Could not clean up stale status message %s: %s", key, exc)

        # The normal completion path in _handle() removes the ⏳ reaction; a
        # crash skips it, so strip it here (original message and thread root).
        msg_ts = entry.get("msg_ts")
        entry_thread_ts = entry.get("thread_ts")
        if adapter.capabilities.supports_reactions:
            for reaction_ts in {t for t in (msg_ts, entry_thread_ts) if t}:
                try:
                    await adapter.remove_reaction(thread_key, "hourglass_flowing_sand", reaction_ts)
                except Exception:
                    # Reaction may already be gone; not worth logging.
                    pass

        thread_ts = entry.get("thread_ts") or entry.get("ts")
        if not channel or not thread_ts:
            continue

        if recovered:
            # The turn actually completed; nothing to resume.
            continue

        async def _resume(adapter=adapter, notice_key=({**thread_key, "thread_ts": thread_ts} if is_slack else thread_key),
                          key=key, conversation_id=conversation_id):
            log.info("[interrupted-resume] Resuming thread %s (was mid-turn at restart)", key)
            try:
                if not conversation_id:
                    raise RuntimeError("no conversation id found for interrupted thread")
                text = (
                    f":repeat: Bot restarted while this was in progress. "
                    f"{_watchers_module.build_resume_instruction(conversation_id)}"
                )
                await _inject_watch_message(conversation_id, text)
            except Exception:
                log.exception("[interrupted-resume] Failed to resume thread %s", key)
                try:
                    await adapters.deliver(
                        adapter, notice_key,
                        ":warning: Auto-resume after the bot restart failed — send a message to continue.",
                        record=False,
                    )
                except Exception:
                    log.exception("[interrupted-resume] Could not post resume-failure notice for %s", key)

        asyncio.create_task(_resume())

    # DB-based safety net: conversations whose last message is still a 'status'
    # placeholder but that the active-status file did not list. Always runs,
    # since message_store is the durable source of truth for "stuck".
    try:
        stuck = message_store.find_stuck_status_conversations()
    except Exception:
        log.exception("[interrupted-resume] Could not query stuck status conversations")
        stuck = []
    stuck = [s for s in stuck if s["conversation_id"] not in handled_conversation_ids]
    if stuck:
        log.info("Found %d additional stuck 'Working...' conversation(s) via DB fallback "
                  "(missed by active_status.json)", len(stuck))
    for entry in stuck:
        conversation_id = entry["conversation_id"]
        recovered_text = _recover_lost_response(conversation_id)
        channel = entry.get("status_channel") or entry.get("channel")
        status_ts = entry.get("status_ts")
        thread_ts = entry.get("thread_ts")
        try:
            if recovered_text:
                if channel and status_ts and not SLACK_DISABLED:
                    await adapters.deliver_update(_slack_adapter(),
                        {"channel": channel},
                        status_ts,
                        recovered_text,
                        conversation_id=conversation_id,
                        role="assistant",
                        message_id=entry["message_id"],
                        buttons=[],
                    )
                else:
                    # No Slack channel (Web UI conversation): fix the DB row; the
                    # Web UI picks it up on its next SSE update/reconnect.
                    message_store.record_message(
                        conversation_id, "assistant", recovered_text, message_id=entry["message_id"],
                    )
                log.info("[interrupted-resume] Recovered lost final answer for conversation %s "
                         "via DB fallback", conversation_id)
            else:
                interrupted_text = (":black_square_for_stop: _Interrupted — bot restarted before this "
                                     "finished. Send a new message to continue._")
                if channel and status_ts and not SLACK_DISABLED:
                    await adapters.deliver_update(_slack_adapter(),
                        {"channel": channel},
                        status_ts,
                        interrupted_text,
                        conversation_id=conversation_id,
                        role="system",
                        message_id=entry["message_id"],
                        buttons=[],
                    )
                else:
                    message_store.record_message(
                        conversation_id, "system", interrupted_text, message_id=entry["message_id"],
                    )
        except Exception:
            log.exception("[interrupted-resume] Could not reconcile stuck conversation %s", conversation_id)
        if channel and thread_ts and not SLACK_DISABLED:
            for reaction_ts in {t for t in (status_ts, thread_ts) if t}:
                try:
                    await app.client.reactions_remove(
                        channel=channel, name="hourglass_flowing_sand", timestamp=reaction_ts,
                    )
                except Exception:
                    pass


async def _resume_interrupted_transcriptions() -> None:
    """Like _resume_interrupted_turns(), but for a voice message whose Whisper
    transcription was running (":headphones: Transcribing..." placeholder) when
    the process died. Nothing else watches an in-flight transcription, so without
    this the placeholder stays stuck and the message is dropped. Slack's
    url_private_download stays valid across a restart, so the file is re-downloaded
    and retried as its own task via _transcribe_and_finalize()."""
    stale = _load_pending_transcriptions()
    if not stale:
        return
    log.info("Found %d stale transcription(s) interrupted by a previous run — retrying", len(stale))
    _save_pending_transcriptions({})  # claim all entries up front
    for key, entry in stale.items():
        channel = entry.get("channel")
        msg_ts = entry.get("msg_ts")
        if not channel or not msg_ts:
            continue

        async def _retry(channel=channel, msg_ts=msg_ts, entry=entry, key=key):
            log.info("[interrupted-transcription] Retrying %s (was mid-transcription at restart)", key)
            retry_text = ":repeat: _Bot restarted mid-transcription — retrying..._"
            thread_ts = entry.get("thread_ts") or msg_ts
            conversation_id = entry.get("conversation_id") or _conversation_id_for(channel, thread_ts)
            message_id = entry.get("message_id") or _message_id_for_slack_message(conversation_id, channel, msg_ts)
            try:
                await adapters.deliver_update(_slack_adapter(),
                    {"channel": channel},
                    msg_ts,
                    retry_text,
                    conversation_id=conversation_id,
                    role="status",
                    message_id=message_id,
                    buttons=[],
                )
            except Exception:
                log.warning("Could not update stale transcription placeholder %s", key)
            file_info = entry.get("file") or {}
            name = file_info.get("name") or "audio"

            async def _transcribe(on_queued, _f=file_info):
                return await handlers.transcribe_slack_audio(_f, on_queued=on_queued)

            try:
                await _transcribe_and_finalize(
                    name=name, transcribe=_transcribe,
                    adapter=_slack_adapter(),
                    thread_key={"channel": channel, "thread_ts": thread_ts},
                    conversation_id=conversation_id, user_id=entry.get("user_id"),
                    message_ref=msg_ts, buf_key=conversation_id or channel,
                    message_id=message_id, clear_pending=channel,
                )
            except Exception:
                log.exception("[interrupted-transcription] Failed to retry %s", key)

        asyncio.create_task(_retry())


async def main():
    log.info("Starting DOP Agent (SDK mode)")
    log.info("  Working dir  : %s", COPILOT_WORKDIR)
    log.info("  Allowed user : %s", ALLOWED_USER_ID or "(anyone — set ALLOWED_USER_ID in .env)")
    log.info("  Timeout      : %ss per turn", SESSION_TIMEOUT)

    _watchers_module.configure(app, _handle)

    # Telegram (optional): register its adapter BEFORE web_server.init and crash
    # recovery - recovery drops in-flight entries whose platform has no adapter.
    # No-op unless TELEGRAM_BOT_TOKEN is configured; never raises.
    _telegram = telegram_boot.setup()

    web_server.init(
        slack_client=None if SLACK_DISABLED else app.client,
        handle_fn=_handle,
        convos=_convos,
        is_authorized=is_authorized,
        allowed_user_id=ALLOWED_USER_ID,
    )
    # Wait until the in-process Web UI / MCP server (/mcp, watch_*/session_*
    # tools) is listening BEFORE resuming or creating any Copilot session: every
    # bot-spawned session's mcp_servers includes this /mcp endpoint
    # (bot_config.SESSION_MCP_SERVERS), and a session that hits connection-refused
    # has that MCP server marked unreachable for its whole lifetime.
    _web_ready = asyncio.Event()
    asyncio.create_task(web_server.run_in_background(_web_ready))
    try:
        await asyncio.wait_for(_web_ready.wait(), timeout=15.0)
    except asyncio.TimeoutError:
        log.warning(
            "Web UI /mcp server did not report ready within 15s — proceeding anyway; any "
            "session resumed/created before it actually starts listening will silently lose "
            "access to the dop-agent watch_*/session_* MCP tools for its entire lifetime."
        )

    await _resume_interrupted_turns()
    if not SLACK_DISABLED:
        await _resume_interrupted_transcriptions()
    telegram_boot.start_poller(_telegram)  # after recovery, so recovered turns are not raced
    if not SLACK_DISABLED:
        asyncio.create_task(handlers.audio_maintenance_loop(_slack_adapter))

    if ALLOWED_USER_ID and not SLACK_DISABLED:
        try:
            # conversations_open needs the im:write scope, which this app lacks,
            # so reuse a DM channel ID known from bot_store.db.
            dm_channel = config.value("agent.startup_notify_channel")
            if not dm_channel:
                dm_channel = message_store.find_known_dm_channel() or ""
            if dm_channel:
                await adapters.deliver(_slack_adapter(), {"channel": dm_channel},
                                       ":white_check_mark: Bot started and ready.", record=False)
            else:
                log.warning("No known DM channel to send startup notification to "
                            "(set STARTUP_NOTIFY_CHANNEL in .env, or DM the bot once first)")
        except Exception:
            log.exception("Failed to send startup notification to ALLOWED_USER_ID")

    # Rotate the boot id (new process generation), then resume threads waiting on
    # a self_restart watch from the previous generation. Run as a background task:
    # resuming a Copilot session can take as long as any turn and must not block
    # handler.start_async() below.
    adapters.set_main_loop(asyncio.get_running_loop())
    _old_boot_id, _new_boot_id = watchers.rotate_boot_id()
    asyncio.create_task(_fire_self_restart_watches(_new_boot_id))

    asyncio.create_task(watcher_loop())

    if SLACK_DISABLED:
        log.warning("Slack DISABLED (disabled or credentials missing) — Web UI remains available")
        await asyncio.Event().wait()
        return

    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

    handler = AsyncSocketModeHandler(app, SLACK_APP_TOKEN)
    await handler.start_async()


if __name__ == "__main__":
    asyncio.run(main())
