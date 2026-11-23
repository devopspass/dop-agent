"""
Copilot session management for the Slack bot — the CopilotConversation
class (one persistent CopilotSession per Slack thread) plus the
conversation registry and thread/event-dedup helpers.

This module depends only on bot_config and persistence (for session-id
save/load) plus message_store (for the Slack-thread -> internal conversation
UUID mapping) — no Slack-client handler logic lives here.

Gotchas:
  - ``_seen_event_keys``/``_is_duplicate_event``: Slack redelivers
    message/app_mention events after brief reconnects (e.g. VPN blips).
    Deduped via ``(channel, ts)`` in a bounded ``OrderedDict``; without it
    duplicate events trigger duplicate turns.
  - Sessions must always be created/resumed with
    ``working_directory=COPILOT_WORKDIR``; every ``create_session``/
    ``resume_session``/``CopilotClient`` call here passes it explicitly.
  - Final answer extraction: ``send_and_wait()`` only *returns* the single
    LAST assistant.message event of a turn, which can be empty (a bare
    ``task_complete`` tool call) or a short remark after the real answer
    (e.g. a todo-status update). The SDK still *streams* every
    assistant.message to handlers registered via ``session.on()``, so
    ``send()`` registers a collector for the whole turn that records every
    non-empty assistant.message ``content`` and every ``task_complete``
    summary (``_message_texts``). If the final event itself has content it is
    used unchanged; otherwise ``_final_text_from_turn()`` picks the single
    LONGEST collected text. Joining all texts is wrong: the system prompt asks
    the agent for short progress narration around tool calls, which would be
    glued onto the answer. The ``task_complete`` summary competes in the
    longest-candidate pick because it is often the real final answer.
"""

from __future__ import annotations

import asyncio
import errno
import os

import config
import sqlite3
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable

import message_store
import usage_index
from copilot import CopilotClient
from copilot.session import PermissionHandler
from copilot.generated.session_events import (
    AssistantIntentData,
    AssistantMessageData,
    AssistantTurnStartData,
    ModelCallStartData,
    SessionModelChangeData,
    ToolExecutionProgressData,
    ToolExecutionStartData,
)

from bot_config import (
    COPILOT_WORKDIR,
    DISABLED_BUILTIN_MCP_SERVERS,
    INTERRUPTED,
    SESSION_MCP_SERVERS,
    SESSION_STATE_DIR,
    SESSION_TIMEOUT,
    SKILL_DIRECTORIES,
    _STATUS_HEARTBEAT_SECS,
    _STATUS_HISTORY_MAX,
    _TODO_STATUS_ICON,
    log,
)
from persistence import _get_saved_session_id, _set_saved_session_id


# Sync callbacks invoked (with the conversation_id) when a real turn ends -
# lets watchers.py fire session_turn watches instantly instead of waiting for
# its periodic poll tick. Kept as a plain list here so this module has no
# dependency on watchers.py (which imports it).
_turn_complete_listeners: list[Callable[[str], None]] = []


def add_turn_complete_listener(fn: Callable[[str], None]) -> None:
    """Register ``fn(conversation_id)``; idempotent. ``fn`` MUST be sync and
    non-blocking (e.g. only schedule a task): it runs inside send()'s
    ``finally:`` while the conversation lock is still held."""
    if fn not in _turn_complete_listeners:
        _turn_complete_listeners.append(fn)


def _notify_turn_complete(conversation_id: str) -> None:
    for fn in list(_turn_complete_listeners):
        try:
            fn(conversation_id)
        except Exception:
            log.exception("turn-complete listener failed for %s", conversation_id)


class CopilotConversation:
    """Holds a persistent CopilotSession for one Slack conversation.

    The underlying Copilot ``session_id`` is persisted in bot_store.db so
    that after a bot restart the same conversation resumes the same
    Copilot session (and its history) instead of starting a brand-new one.
    """

    def __init__(self, conversation_id: str) -> None:
        self.conversation_id = conversation_id
        self._client: CopilotClient | None = None
        self._session = None
        self._lock = asyncio.Lock()
        # Set by a newer send() call when it interrupts a currently-running
        # one (see send() below), and consumed by the interrupted call to
        # signal to its caller that it was superseded rather than completed.
        self._interrupted = False
        # Best-effort cache of the model currently in use, learned from
        # ModelCallStartData/SessionModelChangeData events as they stream in
        # during send(). Persists across turns (in-memory only) so the model
        # name is already known — and can be shown in the "Working..." status
        # — well before the model actually responds on a later turn.
        self.model_name: str | None = None
        self._request_counter = 0
        self._latest_request_id = 0
        # Monotonic per-conversation turn counter. The *completed* half
        # (self._last_completed_turn_generation) is persisted to bot_store.db
        # and restored here so a session_turn watch's baseline comparison
        # survives a restart (see watchers.py's _poll_session_turn_watch).
        # The in-flight half is not persisted: a fresh process never has a
        # turn in flight, so both counters are seeded with the restored value.
        try:
            _restored_generation = message_store.get_turn_generation(conversation_id)
        except Exception:
            _restored_generation = 0
        self._turn_generation = _restored_generation
        # Generation of the most recently *finished* turn (0 = none yet).
        # Set in send()'s finally block for every real (non-superseded) turn.
        self._last_completed_turn_generation = _restored_generation
        # Wall-clock epoch (time.time(), not time.monotonic()) when the
        # current turn started — None when idle. Exposed to the Web UI so the
        # browser can tick a "Working... Ns" counter client-side.
        self.turn_started_at: float | None = None

    @staticmethod
    def _is_broken_pipe_error(exc: Exception) -> bool:
        if isinstance(exc, BrokenPipeError):
            return True
        if isinstance(exc, OSError) and exc.errno == errno.EPIPE:
            return True
        msg = str(exc).lower()
        return "broken pipe" in msg or "errno 32" in msg

    @staticmethod
    def _task_complete_summary(data: AssistantMessageData) -> str:
        """The `summary` argument of a `task_complete` tool request, or ""."""
        for req in data.tool_requests or []:
            if req.name == "task_complete":
                args = req.arguments or {}
                summary = args.get("summary") if isinstance(args, dict) else None
                if summary:
                    return str(summary).strip()
        return ""

    @staticmethod
    def _message_texts(data: AssistantMessageData) -> list[str]:
        """Non-empty user-visible texts of one assistant.message event: its
        content, then its `task_complete` summary (the summary is the
        agent's real final answer, see _final_text_from_turn())."""
        texts = []
        content = (data.content or "").strip()
        if content:
            texts.append(content)
        summary = CopilotConversation._task_complete_summary(data)
        if summary:
            texts.append(summary)
        return texts

    @staticmethod
    def _final_text_from_assistant_message(data: AssistantMessageData) -> str:
        """Extract the user-visible final text from a single assistant.message
        event: its `content`, or the `task_complete` summary when that is
        empty (bare tool-call-only final turn) or longer than a short
        accompanying remark.

        Kept as the single-event fallback used by `_final_text_from_turn()`
        below (and by the broken-pipe retry path, which doesn't bother
        collecting the whole turn's events) — see that function's docstring
        and the module docstring's "(no output) / dropped report" note for
        why a *whole-turn* collector is needed in the normal path.
        """
        content = (data.content or "").strip()
        summary = CopilotConversation._task_complete_summary(data)
        if summary and len(summary) > len(content):
            return summary
        return content

    @staticmethod
    def _final_text_from_turn(collected_texts: list[str], final_data: AssistantMessageData) -> str:
        """Recover the real user-visible answer for a (possibly many-step)
        autopilot turn when the final event `send_and_wait()` hands back is
        empty (trailing tool-call-only steps — see the module docstring's
        "(no output) / dropped report" note for the two incidents this
        covers).

        If `final_data.content` is itself non-empty, callers should just use
        it directly — this function only needs to be consulted for the
        empty-final-event case, and does NOT re-check `final_data.content`
        for emptiness itself (its only job is the recovery fallback).

        `collected_texts` is populated by a handler registered via
        `session.on()` for the duration of the turn (see `send()`) — the SDK
        delivers every assistant.message event to such handlers as they
        stream in, even though `send_and_wait()`'s own return value is only
        the last one. Consecutive duplicate strings are collapsed first (the
        SDK can replay the same event to both its own internal handler and
        ours), then the single LONGEST remaining candidate is returned — not
        a join of all of them. A real full answer (a daily report, a root-
        cause summary, ...) is reliably far longer than the short "progress
        update" narration this bot's system prompt asks the agent to post
        around its tool calls, so picking the longest recovers the dropped
        answer without dragging that surrounding narration into the final
        delivered message (an earlier version of this function joined
        everything instead and was found, by self-review, to glue unrelated
        narration onto real answers on nearly every multi-step turn — ties
        broken in favor of the latest candidate for determinism).

        If nothing was collected at all (e.g. a bare `task_complete`-only
        turn with no earlier text whatsoever, or a fake/test session whose
        `on()` never replays events), falls back to
        `_final_text_from_assistant_message()` — the `task_complete`-summary
        recovery that fixed the original "(no output)" incident.
        """
        candidates: list[str] = []
        for text in collected_texts:
            if not candidates or candidates[-1] != text:
                candidates.append(text)
        if candidates:
            longest = candidates[0]
            for text in candidates[1:]:
                if len(text) >= len(longest):
                    longest = text
            return longest
        return CopilotConversation._final_text_from_assistant_message(final_data)

    def _stored_model(self) -> str | None:
        try:
            return message_store.get_conversation_model(self.conversation_id)
        except Exception:
            log.exception("[%s] could not read stored model", self.conversation_id)
            return None

    async def _ensure_session(self) -> None:
        if self._session is not None:
            return

        try:  # package-driver skills (one per enabled instance); never blocks a session
            from integrations.skills import ensure_skills
            await asyncio.to_thread(ensure_skills)
        except Exception:
            log.exception("[%s] integration skills not refreshed", self.conversation_id)

        self._client = CopilotClient(working_directory=COPILOT_WORKDIR, github_token=config.copilot_github_token() or None)
        await self._client.__aenter__()

        # Model routed at session creation (NULL = CLI default); passed on
        # create AND resume so a resumed session keeps its model.
        stored_model = self._stored_model()
        model_kwargs = {"model": stored_model} if stored_model else {}

        saved_session_id = _get_saved_session_id(self.conversation_id)
        if saved_session_id:
            try:
                log.info("[%s] Resuming Copilot session %s (workdir=%s)",
                         self.conversation_id, saved_session_id, COPILOT_WORKDIR)
                self._session = await self._client.resume_session(
                    saved_session_id,
                    on_permission_request=PermissionHandler.approve_all,
                    working_directory=COPILOT_WORKDIR,
                    mcp_servers=SESSION_MCP_SERVERS,
                    disabled_mcp_servers=DISABLED_BUILTIN_MCP_SERVERS,
                    enable_skills=True,
                    skill_directories=SKILL_DIRECTORIES,
                    **model_kwargs,
                )
            except Exception as exc:
                log.warning("[%s] Could not resume session %s (%s) — starting a new one",
                            self.conversation_id, saved_session_id, exc)
                self._session = None

        if self._session is None:
            # A conversation's first CLI session reuses the conversation UUID
            # as its session id. A replacement session (resume failed) needs a
            # fresh uuid4, since the CLI already knows the old id.
            new_session_id = _first_session_id(self.conversation_id) if not saved_session_id else str(uuid.uuid4())
            log.info("[%s] Creating new Copilot session %s (workdir=%s)",
                     self.conversation_id, new_session_id, COPILOT_WORKDIR)
            create_kwargs = dict(
                session_id=new_session_id,
                on_permission_request=PermissionHandler.approve_all,
                working_directory=COPILOT_WORKDIR,
                mcp_servers=SESSION_MCP_SERVERS,
                disabled_mcp_servers=DISABLED_BUILTIN_MCP_SERVERS,
                enable_skills=True,
                skill_directories=SKILL_DIRECTORIES,
            )
            try:
                self._session = await self._client.create_session(**create_kwargs, **model_kwargs)
            except Exception as exc:
                if not model_kwargs:
                    raise
                # Routed model rejected: fall back to the CLI default, visibly,
                # rather than failing the session.
                log.warning("[%s] create_session(model=%s) failed (%s) - retrying on default model",
                            self.conversation_id, stored_model, exc)
                message_store.set_conversation_model(self.conversation_id, None, f"fallback: {stored_model} rejected ({exc})")
                message_store.record_message(
                    self.conversation_id, "system",
                    f":warning: Model {stored_model} was rejected by the SDK - this session runs on the default model.")
                self._session = await self._client.create_session(**create_kwargs)
            _set_saved_session_id(self.conversation_id, new_session_id)

        await self._session.__aenter__()
        log.info("[%s] Copilot session ready (session_id=%s)", self.conversation_id, self._session.session_id)

    # Max length of an argument summary appended to a tool status line.
    _ARG_SUMMARY_MAX = 160

    @staticmethod
    def _summarize_tool_arguments(tool_name: str, arguments) -> str | None:
        """Best-effort human-readable summary of a tool call's key argument(s).

        Different tools name their "interesting" argument differently (a shell
        command, a file path, a search pattern, a URL, ...). This inspects the
        arguments dict for the first known/likely key and returns a short,
        single-line summary suitable for a Slack status update.
        """
        if not isinstance(arguments, dict):
            return None

        def _clip(s: str) -> str:
            """Clip a RAW (not yet markdown-wrapped) value to the shared
            length budget. Must only ever be called on plain text, before
            any surrounding backticks/parens are added - clipping an
            already-composed string like "`some long command`" could cut
            the closing backtick off along with the tail, leaving a broken,
            unterminated inline-code span in the rendered status line (a
            real cosmetic bug: long commands/patterns rendered as literal
            text from the stray opening backtick onward instead of a code
            span). Clipping the inner value first and wrapping it
            afterwards keeps every backtick pair balanced no matter how
            long the original value was."""
            s = " ".join(str(s).split())  # collapse newlines/whitespace
            max_len = CopilotConversation._ARG_SUMMARY_MAX
            return s if len(s) <= max_len else s[: max_len - 1] + "…"

        name = (tool_name or "").lower()

        # Shell/bash-style tools: prefer the human "description", fall back to command.
        if name in ("bash", "shell", "execute", "run_command") or "command" in arguments:
            desc = arguments.get("description")
            cmd = arguments.get("command")
            if desc and cmd:
                return f"{_clip(desc)} (`{_clip(cmd)}`)"
            if desc:
                return _clip(desc)
            if cmd:
                return f"`{_clip(cmd)}`"

        # Search-style tools (grep/glob) — check before the generic "path" branch,
        # since these tools also carry a path/paths scope alongside the pattern.
        if "pattern" in arguments:
            scope = arguments.get("path") or arguments.get("paths") or ""
            return f"`{_clip(arguments['pattern'])}`" + (f" in {_clip(scope)}" if scope else "")

        # File read/write tools.
        if "path" in arguments:
            path = arguments["path"]
            extra = arguments.get("view_range")
            if extra:
                return _clip(f"{path} (lines {extra[0]}-{extra[1]})")
            return _clip(str(path))
        if "paths" in arguments:
            return _clip(str(arguments["paths"]))

        # Web fetch.
        if "url" in arguments:
            return _clip(arguments["url"])

        # SQL / generic query tools.
        if "query" in arguments:
            return _clip(arguments["query"])
        if "description" in arguments:
            return _clip(arguments["description"])
        if "prompt" in arguments:
            return _clip(arguments["prompt"])
        if "name" in arguments:
            return _clip(arguments["name"])

        # Unknown shape — show a compact key=value fallback so it's still useful.
        try:
            preview = ", ".join(f"{k}={v}" for k, v in list(arguments.items())[:2])
            return _clip(preview) if preview else None
        except Exception:
            return None

    @staticmethod
    def _read_session_todos(session_id: str | None) -> list[tuple[str, str]]:
        """Read (title, status) rows from the running session's own local
        todos table (same schema/location as this tool's own SQL todos —
        ``~/.copilot/session-state/<session_id>/session.db``). There is no
        SDK/RPC accessor for this yet (SessionTodosChangedData is a
        signal-only "something changed, go re-read it yourself" event), so
        this reads the sqlite file directly, read-only, best-effort. Returns
        an empty list if the session hasn't created any todos this turn, the
        db/table doesn't exist yet, or it can't be read for any reason —
        callers should just omit the checklist in that case, not error out.
        """
        if not session_id:
            return []
        db_path = SESSION_STATE_DIR / session_id / "session.db"
        if not db_path.exists():
            return []
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
            try:
                cur = conn.execute("SELECT title, status FROM todos ORDER BY created_at ASC")
                return cur.fetchall()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            log.debug("Could not read todos for session %s: %s", session_id, exc)
            return []

    @staticmethod
    def _build_todos_block(todos: list[tuple[str, str]]) -> str:
        """Render a todo checklist block, or '' if there are no todos. Always
        meant to be prepended before all other status text (see _handle's
        request to always show it first)."""
        if not todos:
            return ""
        lines = [f"{_TODO_STATUS_ICON.get(status, ':white_large_square:')} {title}" for title, status in todos]
        return ":clipboard: *To-dos:*\n" + "\n".join(lines) + "\n\n"

    @staticmethod
    def _event_to_status(event) -> str | None:
        """Return a human-readable status string for interesting SDK events, or None."""
        d = event.data
        if isinstance(d, ToolExecutionStartData):
            name = d.mcp_tool_name or d.tool_name
            pretty = name.replace("_", " ").replace("-", " ").title()
            detail = CopilotConversation._summarize_tool_arguments(d.tool_name, d.arguments)
            if detail:
                return f":wrench: *{pretty}*: {detail}"
            return f":wrench: `{pretty}`"
        if isinstance(d, ToolExecutionProgressData):
            return f":gear: {d.progress_message}"
        if isinstance(d, AssistantIntentData) and d.intent:
            return f":dart: {d.intent}"
        if isinstance(d, AssistantTurnStartData):
            return ":thought_balloon: Thinking..."
        return None

    @staticmethod
    def _event_to_model(event) -> str | None:
        """Return the model name carried by this event, if any.

        Used to learn which model is actually running so it can be surfaced
        in the "Working..." status (see send()'s event_handler below).
        """
        d = event.data
        if isinstance(d, SessionModelChangeData) and d.new_model:
            return d.new_model
        if isinstance(d, ModelCallStartData) and d.model:
            return d.model
        return None

    async def interrupt(self) -> bool:
        """Abort the in-progress turn (if any) without starting a replacement
        one. Used by the ":black_square_for_stop: Stop" button on the live "Working..." status
        message. Mirrors the "new message arrived while busy" abort path in
        send() below — setting self._interrupted causes the task currently
        awaiting send_and_wait() to return the INTERRUPTED sentinel, which
        _handle() turns into the "Interrupted" status message update.
        Returns True if a turn was actually running (and abort was attempted),
        False if there was nothing to stop.
        """
        if self._lock.locked():
            log.info("[%s] Stop button pressed — aborting in-progress turn", self.conversation_id)
            self._interrupted = True
            try:
                if self._session is not None:
                    await self._session.abort()
            except Exception as exc:
                log.warning("[%s] Failed to abort in-progress turn via Stop button: %s",
                            self.conversation_id, exc)
            return True
        return False

    def is_busy(self) -> bool:
        """True if a turn is currently in flight for this conversation.

        Used by watchers.py's _inject_watch_message to detect that an
        automated message is about to abort a live turn (see send()). As
        coarse as send()'s own busy check (self._lock.locked()): any
        in-progress turn counts, even one blocked on a long tool call.
        """
        return self._lock.locked()

    def current_turn_generation(self) -> int | None:
        """Generation number of the turn currently in flight for this
        conversation, or None if idle. Used by watchers.py's session_turn
        watches as the baseline at registration time, so a watch registered
        mid-turn does not fire on that same turn's completion."""
        return self._turn_generation if self.turn_started_at is not None else None

    def last_completed_turn_generation(self) -> int:
        """Generation number of the most recently *finished* turn for this
        conversation (0 if none has ever completed). Monotonically
        non-decreasing; compared against a session_turn watch's stored
        meta.baseline_generation (see watchers.py)."""
        return self._last_completed_turn_generation

    async def send(
        self,
        message: str,
        on_status: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        self._request_counter += 1
        request_id = self._request_counter
        self._latest_request_id = request_id
        # If a previous turn is still running for this conversation, abort it
        # so this new message can start right away instead of queuing behind
        # the whole in-progress turn. The interrupted call detects this via
        # the request-id guard below (only the newest queued message may
        # proceed into send_and_wait()) and returns the INTERRUPTED sentinel
        # instead of a normal response.
        if self._lock.locked() and self._session is not None:
            log.info("[%s] New message arrived while busy — aborting in-progress turn",
                      self.conversation_id)
            try:
                await self._session.abort()
            except Exception as exc:
                log.warning("[%s] Failed to abort in-progress turn: %s", self.conversation_id, exc)
        async with self._lock:
            # Aborting a turn (above) and a turn timeout (see except block
            # below) keep the saved session id, so history is preserved. Only
            # a broken pipe (CLI process gone) clears it.
            await self._ensure_session()
            if request_id != self._latest_request_id:
                # This turn was superseded before it even started. Clear any
                # stale _interrupted flag left behind by a Stop-button press
                # aimed at *this* (now-discarded) turn — otherwise the next,
                # newer turn (which passes this check) would incorrectly see
                # _interrupted still True below and be wrongly treated as
                # interrupted itself, silently dropping its message without
                # ever calling send_and_wait().
                self._interrupted = False
                return INTERRUPTED
            if self._interrupted:
                self._interrupted = False
                return INTERRUPTED
            self.turn_started_at = time.time()
            # A real turn is now confirmed to be starting (past both
            # "superseded before it even started" early-return checks
            # above) — advance the generation counter so session_turn
            # watches registered mid-turn can tell this turn apart from
            # whichever one runs next (see current_turn_generation()).
            self._turn_generation += 1

            updater_task = None
            unsubscribe = None

            # Collects every non-empty assistant.message content seen while
            # this turn runs, so the final answer can be reconstructed even
            # if later steps in the same turn (more tool calls, a trailing
            # task_complete) leave send_and_wait()'s own return event empty
            # or short — see _final_text_from_turn()'s docstring and the
            # module docstring's "(no output) / dropped report" note.
            collected_texts: list[str] = []

            def _collect_handler(event) -> None:
                usage_index.note_live_event(self.conversation_id, event.data)
                if isinstance(event.data, AssistantMessageData):
                    collected_texts.extend(self._message_texts(event.data))

            collect_unsubscribe = self._session.on(_collect_handler)

            if on_status:
                loop = asyncio.get_running_loop()
                queue: asyncio.Queue[str | None] = asyncio.Queue()

                def event_handler(event) -> None:
                    model = CopilotConversation._event_to_model(event)
                    if model:
                        # Cached on the conversation (not just this turn) so the
                        # *next* turn's initial "Working..." placeholder already
                        # knows the model, even before any event has fired yet.
                        self.model_name = model
                    status = CopilotConversation._event_to_status(event)
                    if status:
                        # call_soon_threadsafe works from any thread / sync callback
                        loop.call_soon_threadsafe(queue.put_nowait, status)

                unsubscribe = self._session.on(event_handler)

                async def status_updater() -> None:
                    # Keep a running history of status lines instead of only showing
                    # the latest — so the user can follow everything Copilot did
                    # during this turn, not just the most recent action.
                    history: list[str] = []
                    turn_start = time.monotonic()
                    last_activity = turn_start
                    last_push = 0.0

                    def _append(item: str) -> None:
                        nonlocal last_activity
                        if not history or item != history[-1]:
                            history.append(item)
                        last_activity = time.monotonic()

                    def _build_combined() -> str:
                        # Staleness indicator up front: elapsed time since the turn
                        # started plus time since the last real activity. If a turn
                        # genuinely hangs, this heartbeat (see below) keeps pushing
                        # updates so the "idle" number keeps climbing in the Slack
                        # message instead of silently freezing — that's what lets
                        # the user tell "still working" apart from "stuck a while ago".
                        now = time.monotonic()
                        elapsed = int(now - turn_start)
                        idle = int(now - last_activity)
                        model_suffix = f" ({self.model_name})" if self.model_name else ""
                        header = f":thinking_face: _Working...{model_suffix}_"
                        clock = f"_:stopwatch: {elapsed}s elapsed"
                        if idle >= _STATUS_HEARTBEAT_SECS:
                            clock += f", idle {idle}s"
                        clock += "_"
                        # Todo checklist (if the agent is using todos this turn) is
                        # always shown first, before the "Working..." header/clock
                        # and step history — re-read on every rebuild so it stays
                        # current as todos are added/completed mid-turn.
                        todos_block = CopilotConversation._build_todos_block(
                            CopilotConversation._read_session_todos(self.session_id))
                        return f"{todos_block}{header}\n{clock}\n" + "\n".join(history)

                    while True:
                        try:
                            status = await asyncio.wait_for(queue.get(), timeout=2.0)
                        except asyncio.TimeoutError:
                            # No new tool-call events recently — still refresh the
                            # header periodically (heartbeat) purely so the elapsed/
                            # idle counters keep advancing while the turn is alive.
                            if time.monotonic() - last_push >= _STATUS_HEARTBEAT_SECS:
                                try:
                                    await on_status(_build_combined())
                                except Exception as exc:
                                    log.debug("Status heartbeat update failed: %s", exc)
                                last_push = time.monotonic()
                            continue
                        if status is None:
                            break
                        _append(status)
                        # Drain everything else currently queued into history too.
                        stop = False
                        while not queue.empty():
                            nxt = queue.get_nowait()
                            if nxt is None:
                                stop = True
                                break
                            _append(nxt)
                        # Cap history so the Slack message doesn't grow unbounded.
                        if len(history) > _STATUS_HISTORY_MAX:
                            del history[: len(history) - _STATUS_HISTORY_MAX]
                        try:
                            await on_status(_build_combined())
                        except Exception as exc:
                            log.debug("Status update callback error: %s", exc)
                        last_push = time.monotonic()
                        if stop:
                            break
                        # Stay under Slack's ~1 msg/s rate limit
                        await asyncio.sleep(0.9)

                updater_task = asyncio.create_task(status_updater())

            try:
                event = await self._session.send_and_wait(
                    message,
                    agent_mode="autopilot",
                    timeout=SESSION_TIMEOUT,
                )
                # A newer message superseded us, or the user explicitly hit
                # Stop while this turn was being prepared/running.
                if request_id != self._latest_request_id:
                    # Same reasoning as the pre-flight check above: clear the
                    # flag so it can't leak into whichever newer turn is
                    # queued behind us.
                    self._interrupted = False
                    return INTERRUPTED
                if self._interrupted:
                    self._interrupted = False
                    return INTERRUPTED
                if event and isinstance(event.data, AssistantMessageData):
                    # Common case first, untouched: if the final event itself
                    # has real content, use it directly — only consult the
                    # whole-turn collector (_final_text_from_turn) for the
                    # empty-final-event recovery case (see that function's
                    # docstring for why it must not be used unconditionally).
                    if (event.data.content or "").strip():
                        return self._final_text_from_assistant_message(event.data)
                    return self._final_text_from_turn(collected_texts, event.data)
                return "_(no response)_"
            except TimeoutError as exc:
                # A timeout usually means one long-running tool call blocked
                # past SESSION_TIMEOUT, not that the session is dead. Only drop
                # the local handle and try to abort the stuck turn; keep the
                # saved session_id so _ensure_session() resumes the same
                # session (and falls back to a new one if resume fails).
                log.warning("[%s] Copilot turn timed out after %.0fs — closing local handle "
                            "and will attempt to resume the same session next turn",
                            self.conversation_id, SESSION_TIMEOUT)
                try:
                    if self._session is not None:
                        await self._session.abort()
                except Exception as abort_exc:
                    log.warning("[%s] Failed to abort timed-out turn: %s",
                                self.conversation_id, abort_exc)
                await self._close()
                raise TimeoutError(f"Copilot request timed out after {SESSION_TIMEOUT}s") from exc
            except Exception as exc:
                if not self._is_broken_pipe_error(exc):
                    raise
                log.warning("[%s] Broken pipe from Copilot CLI — resetting session and retrying once",
                            self.conversation_id)
                await self._close()
                _set_saved_session_id(self.conversation_id, None)
                await self._ensure_session()
                # The old session and its `on()` registration are gone after
                # _close()/_ensure_session(); register a fresh collector.
                retry_collected_texts: list[str] = []

                def _retry_collect_handler(event) -> None:
                    usage_index.note_live_event(self.conversation_id, event.data)
                    if isinstance(event.data, AssistantMessageData):
                        retry_collected_texts.extend(self._message_texts(event.data))

                retry_collect_unsubscribe = self._session.on(_retry_collect_handler)
                try:
                    retry_event = await self._session.send_and_wait(
                        message,
                        agent_mode="autopilot",
                        timeout=SESSION_TIMEOUT,
                    )
                finally:
                    retry_collect_unsubscribe()
                if retry_event and isinstance(retry_event.data, AssistantMessageData):
                    if (retry_event.data.content or "").strip():
                        return self._final_text_from_assistant_message(retry_event.data)
                    return self._final_text_from_turn(retry_collected_texts, retry_event.data)
                return "_(no response)_"
            finally:
                self.turn_started_at = None
                # Runs for every real (non-superseded) turn regardless of
                # outcome — success, timeout, broken-pipe retry, interrupted/
                # aborted. A session_turn watch's baseline comparison relies
                # on this being unconditional: an aborted/erroring turn still
                # counts as "this turn ended" (see _poll_session_turn_watch's
                # docstring in watchers.py for why swallowed-error turns must
                # not leave a watch stuck waiting forever).
                self._last_completed_turn_generation = self._turn_generation
                try:
                    message_store.set_turn_generation(
                        self.conversation_id, self._last_completed_turn_generation
                    )
                except Exception:
                    log.exception(
                        "Failed to persist turn_generation for %s", self.conversation_id
                    )
                _notify_turn_complete(self.conversation_id)
                try:
                    collect_unsubscribe()
                except Exception:
                    pass
                if unsubscribe:
                    unsubscribe()
                if updater_task:
                    queue.put_nowait(None)  # sentinel → stop updater
                    await updater_task

    async def reset(self) -> None:
        async with self._lock:
            await self._close()
            _set_saved_session_id(self.conversation_id, None)

    async def ensure_ready(self) -> None:
        """Public wrapper to make sure a session exists (and its id is known)
        without sending a turn — used to announce a new thread's session id
        as the very first reply, before the first real Copilot response.
        """
        async with self._lock:
            await self._ensure_session()

    @property
    def session_id(self) -> str | None:
        return self._session.session_id if self._session is not None else None

    async def _close(self) -> None:
        if self._session is not None:
            try:
                await self._session.__aexit__(None, None, None)
            except Exception:
                pass
            self._session = None
        if self._client is not None:
            try:
                await self._client.__aexit__(None, None, None)
            except Exception:
                pass
            self._client = None

    @property
    def is_alive(self) -> bool:
        return self._session is not None


# conversation_id -> CopilotConversation
_convos: dict[str, CopilotConversation] = {}


# Recently-seen Slack event (channel, ts) pairs, used to ignore duplicate
# deliveries. Slack (Events API / Socket Mode) can redeliver the same event
# if our ack is slow or the connection briefly drops (e.g. VPN reconnects) —
# retried deliveries carry the same "ts", so this is a reliable dedup key.
# Bounded FIFO to avoid unbounded growth over a long-running process.
_seen_event_keys: "OrderedDict[tuple[str, str], None]" = OrderedDict()
_SEEN_EVENT_MAX = 500

from message_ref import first_sighting  # noqa: E402


def _is_duplicate_event(event: dict) -> bool:
    key = (event.get("channel", ""), event.get("ts", ""))
    if not key[1]:
        return False
    return not first_sighting(_seen_event_keys, key, _SEEN_EVENT_MAX)


def get_convo(conversation_id: str) -> CopilotConversation:
    if conversation_id not in _convos:
        _convos[conversation_id] = CopilotConversation(conversation_id)
    return _convos[conversation_id]


def get_convo_if_exists(conversation_id: str) -> CopilotConversation | None:
    """Look up a conversation without creating one as a side effect.

    Used by watchers.py's busy-check before injecting an automated message —
    a conversation that has never been touched in this process can't
    possibly be busy, and get_convo() would otherwise silently create a
    fresh (never-used) CopilotConversation just to answer that question.
    """
    return _convos.get(conversation_id)


def _thread_root(event: dict) -> str:
    """The Slack thread's root ts, used as this thread's session key.

    A brand-new top-level message has no ``thread_ts`` yet — its own ``ts``
    becomes the thread root going forward (and is also what we pass back to
    Slack as ``thread_ts`` on the first reply, which is what actually turns
    it into a real Slack thread). A reply within an existing thread already
    carries ``thread_ts`` pointing at that same root.
    """
    return event.get("thread_ts") or event.get("ts", "")


def _slack_key_for(channel: str, thread_ts: str) -> str:
    return f"{channel}:{thread_ts}"


def _first_session_id(conversation_id: str) -> str:
    """Copilot session id for a conversation's first CLI session: the
    conversation id itself when it is a valid UUID, else a random uuid4."""
    try:
        return str(uuid.UUID(str(conversation_id)))
    except ValueError:
        return str(uuid.uuid4())


def _conversation_id_for(channel: str, thread_ts: str) -> str:
    """Resolve a Slack thread to the bot-owned internal conversation UUID."""
    return message_store.get_or_create_bound_conversation(
        message_store.SLACK_PLATFORM,
        _slack_key_for(channel, thread_ts),
    )


def _is_new_conversation(conversation_id: str) -> bool:
    """True if this thread has no in-memory or persisted Copilot session yet
    — used to decide whether to announce a freshly-created session id as the
    first reply in the thread."""
    return conversation_id not in _convos and not _get_saved_session_id(conversation_id)
