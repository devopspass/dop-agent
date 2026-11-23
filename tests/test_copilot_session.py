"""
Unit tests for copilot_session.py — CopilotConversation's pure/static
helper logic (tool-argument summarization, status-line rendering, event
classification) and the module-level conversation registry / thread /
event-dedup helpers, plus send() driven against fake sessions with
_ensure_session() stubbed. The real SDK session path (CopilotClient RPC) is
not exercised here.
"""

from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

import message_store
import copilot_session
from copilot_session import CopilotConversation
from copilot.generated.session_events import (
    AssistantMessageData,
    AssistantMessageToolRequest,
    AssistantIntentData,
    AssistantTurnStartData,
    ModelCallStartData,
    SessionModelChangeData,
    ToolExecutionProgressData,
    ToolExecutionStartData,
)


@pytest.fixture(autouse=True)
def _isolated_message_store(tmp_path, monkeypatch):
    """CopilotConversation reads/writes message_store (turn_generation persistence
    for session_turn watch restart-recovery); every test uses a throwaway sqlite
    file so the developer's real bot_store.db is never touched."""
    monkeypatch.setattr(message_store, "DB_PATH", tmp_path / "bot_store.db")
    message_store.init_db()


def _event(data):
    return SimpleNamespace(data=data)


# --- _summarize_tool_arguments --------------------------------------------

def test_summarize_tool_arguments_bash_with_description():
    result = CopilotConversation._summarize_tool_arguments(
        "bash", {"description": "List files", "command": "ls -la"}
    )
    assert result == "List files (`ls -la`)"


def test_summarize_tool_arguments_command_only():
    result = CopilotConversation._summarize_tool_arguments("shell", {"command": "pwd"})
    assert result == "`pwd`"


def test_summarize_tool_arguments_pattern_with_scope():
    result = CopilotConversation._summarize_tool_arguments("grep", {"pattern": "foo", "path": "src/"})
    assert result == "`foo` in src/"


def test_summarize_tool_arguments_path_with_view_range():
    result = CopilotConversation._summarize_tool_arguments(
        "view", {"path": "a.py", "view_range": [10, 20]}
    )
    assert result == "a.py (lines 10-20)"


def test_summarize_tool_arguments_url():
    result = CopilotConversation._summarize_tool_arguments("web_fetch", {"url": "https://example.com"})
    assert result == "https://example.com"


def test_summarize_tool_arguments_unknown_shape_fallback():
    result = CopilotConversation._summarize_tool_arguments("mystery", {"a": 1, "b": 2, "c": 3})
    assert result == "a=1, b=2"


def test_summarize_tool_arguments_non_dict_returns_none():
    assert CopilotConversation._summarize_tool_arguments("bash", "not-a-dict") is None


def test_summarize_tool_arguments_clips_long_values():
    long_cmd = "x" * 300
    result = CopilotConversation._summarize_tool_arguments("bash", {"command": long_cmd})
    assert len(result) <= CopilotConversation._ARG_SUMMARY_MAX + 5
    assert result.endswith("…`") or result.endswith("…")


# --- _build_todos_block ----------------------------------------------------

def test_build_todos_block_empty():
    assert CopilotConversation._build_todos_block([]) == ""


def test_build_todos_block_renders_lines():
    todos = [("Do thing", "done"), ("Other thing", "in_progress")]
    block = CopilotConversation._build_todos_block(todos)
    assert "Do thing" in block
    assert "Other thing" in block
    assert block.startswith(":clipboard: *To-dos:*")


# --- _event_to_status / _event_to_model ------------------------------------

def test_event_to_status_tool_execution_start():
    d = ToolExecutionStartData(tool_call_id="1", tool_name="bash", arguments={"command": "ls"})
    status = CopilotConversation._event_to_status(_event(d))
    assert status.startswith(":wrench:")
    assert "ls" in status


def test_event_to_status_tool_execution_progress():
    d = ToolExecutionProgressData(progress_message="halfway there", tool_call_id="1")
    status = CopilotConversation._event_to_status(_event(d))
    assert status == ":gear: halfway there"


def test_event_to_status_assistant_intent():
    d = AssistantIntentData(intent="Fix the bug")
    status = CopilotConversation._event_to_status(_event(d))
    assert status == ":dart: Fix the bug"


def test_event_to_status_assistant_intent_empty_returns_none():
    d = AssistantIntentData(intent="")
    assert CopilotConversation._event_to_status(_event(d)) is None


def test_event_to_status_turn_start():
    d = AssistantTurnStartData(turn_id="t1")
    status = CopilotConversation._event_to_status(_event(d))
    assert status == ":thought_balloon: Thinking..."


def test_event_to_status_unrecognized_returns_none():
    assert CopilotConversation._event_to_status(_event(object())) is None


def test_event_to_model_session_model_change():
    d = SessionModelChangeData(new_model="gpt-5")
    assert CopilotConversation._event_to_model(_event(d)) == "gpt-5"


def test_event_to_model_model_call_start():
    d = ModelCallStartData(turn_id="t1", model="claude")
    assert CopilotConversation._event_to_model(_event(d)) == "claude"


def test_event_to_model_none_for_other_events():
    d = AssistantTurnStartData(turn_id="t1")
    assert CopilotConversation._event_to_model(_event(d)) is None


# --- _final_text_from_turn / _final_text_from_assistant_message -----------

def test_final_text_from_assistant_message_uses_content_when_present():
    data = AssistantMessageData(content="hello world", message_id="m1")
    assert CopilotConversation._final_text_from_assistant_message(data) == "hello world"


def test_final_text_from_assistant_message_falls_back_to_task_complete_summary():
    """A turn ending in a bare `task_complete` tool call (no accompanying text)
    yields the tool call's summary argument instead of an empty string, so
    `handlers.py` does not show "_(no output)_"."""
    req = AssistantMessageToolRequest(
        name="task_complete",
        tool_call_id="tc1",
        arguments={"summary": "Did the thing"},
    )
    data = AssistantMessageData(content="", message_id="m2", tool_requests=[req])
    assert CopilotConversation._final_text_from_assistant_message(data) == "Did the thing"


def test_final_text_from_turn_reconstructs_report_dropped_by_later_steps():
    """The agent may write its full answer in one step and make further tool calls
    (e.g. a SQL todo update, then task_complete) in later steps of the same turn;
    send_and_wait() only returns that last, short/empty event.
    _final_text_from_turn() recovers the full answer by choosing the longest
    candidate, not by concatenating texts, so short progress narration is not
    glued onto the answer."""
    collected = ["Here is the full daily report...", "Updating todo status."]
    final_req = AssistantMessageToolRequest(
        name="task_complete", tool_call_id="tc2", arguments={"summary": "done"},
    )
    final_data = AssistantMessageData(content="", message_id="m3", tool_requests=[final_req])
    result = CopilotConversation._final_text_from_turn(collected, final_data)
    assert result == "Here is the full daily report..."
    assert "Updating todo status." not in result


def test_final_text_from_turn_does_not_concatenate_multiple_narration_steps():
    """A turn may emit several short status-narration texts (the bot's "post a brief
    progress update" convention) around the one real final answer, then end on an
    empty tool-call-only event. The delivered message is just the longest answer,
    not all narration glued together with blank lines."""
    collected = [
        "The bot restart completed successfully — that confirms the fix is live. Let's verify the daemon is actually healthy post-restart.",
        "Daemon is running cleanly, no tracebacks, and resumed this same session correctly. The dropped-report fix is committed and live.",
        "**Summary:** Root-caused the issue and shipped a fix. Added new unit tests, all green. Committed and restarted the bot daemon; it came back up cleanly.",
    ]
    final_data = AssistantMessageData(content="", message_id="m-prod-incident")
    result = CopilotConversation._final_text_from_turn(collected, final_data)
    assert result == collected[2]
    assert "Let's verify the daemon" not in result
    assert "\n\n" not in result or result.count("\n\n") < 2


def test_task_complete_summary_beats_progress_narration_so_user_need_not_send_question_mark():
    """When the agent narrates ("Now the edits (one per template).") and then ends
    with a bare `task_complete` whose summary is the real answer, the summary wins
    over the short narration, so the user gets the result without asking again."""
    summary = "Done. I shortened the description in all 10 driver skill templates and committed it."
    req = AssistantMessageToolRequest(
        name="task_complete", tool_call_id="tc3", arguments={"summary": summary},
    )
    final_data = AssistantMessageData(content="", message_id="m6", tool_requests=[req])
    collected = ["Now the edits (one per template)."] + CopilotConversation._message_texts(final_data)
    assert CopilotConversation._final_text_from_turn(collected, final_data) == summary


def test_final_text_from_assistant_message_prefers_longer_task_complete_summary_over_short_content():
    req = AssistantMessageToolRequest(
        name="task_complete", tool_call_id="tc4",
        arguments={"summary": "Committed abc123; tests pass; bot not restarted yet."},
    )
    data = AssistantMessageData(content="Now commit.", message_id="m7", tool_requests=[req])
    assert CopilotConversation._final_text_from_assistant_message(data).startswith("Committed abc123")


def test_final_text_from_turn_dedupes_consecutive_repeats():
    collected = ["same text", "same text"]
    final_data = AssistantMessageData(content="same text", message_id="m4")
    result = CopilotConversation._final_text_from_turn(collected, final_data)
    assert result == "same text"


def test_final_text_from_turn_falls_back_when_nothing_collected():
    """When no events were replayed via on() (e.g. a fake/test session), the
    final event's own content is used."""
    final_data = AssistantMessageData(content="only this", message_id="m5")
    assert CopilotConversation._final_text_from_turn([], final_data) == "only this"


# --- _is_broken_pipe_error --------------------------------------------------

def test_is_broken_pipe_error_true_for_brokenpipeerror():
    assert CopilotConversation._is_broken_pipe_error(BrokenPipeError()) is True


def test_is_broken_pipe_error_true_for_oserror_epipe():
    import errno
    exc = OSError(errno.EPIPE, "broken")
    assert CopilotConversation._is_broken_pipe_error(exc) is True


def test_is_broken_pipe_error_true_for_message_match():
    assert CopilotConversation._is_broken_pipe_error(Exception("Broken pipe occurred")) is True


def test_is_broken_pipe_error_false_for_unrelated_error():
    assert CopilotConversation._is_broken_pipe_error(ValueError("nope")) is False


# --- _read_session_todos -----------------------------------------------------

def test_read_session_todos_missing_session_id():
    assert CopilotConversation._read_session_todos(None) == []


def test_read_session_todos_missing_db(monkeypatch, tmp_path):
    monkeypatch.setattr(copilot_session, "SESSION_STATE_DIR", tmp_path)
    assert CopilotConversation._read_session_todos("no-such-session") == []


def test_read_session_todos_reads_rows(monkeypatch, tmp_path):
    monkeypatch.setattr(copilot_session, "SESSION_STATE_DIR", tmp_path)
    session_dir = tmp_path / "abc"
    session_dir.mkdir()
    db_path = session_dir / "session.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE todos (title TEXT, status TEXT, created_at TEXT)")
    conn.execute("INSERT INTO todos VALUES ('first', 'done', '2024-01-01')")
    conn.execute("INSERT INTO todos VALUES ('second', 'pending', '2024-01-02')")
    conn.commit()
    conn.close()

    rows = CopilotConversation._read_session_todos("abc")
    assert rows == [("first", "done"), ("second", "pending")]


def test_read_session_todos_corrupt_db_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(copilot_session, "SESSION_STATE_DIR", tmp_path)
    session_dir = tmp_path / "corrupt"
    session_dir.mkdir()
    (session_dir / "session.db").write_text("not a real sqlite file")

    assert CopilotConversation._read_session_todos("corrupt") == []


# --- module-level conversation registry / dedup helpers ---------------------

def test_is_duplicate_event_detects_repeats(monkeypatch):
    monkeypatch.setattr(copilot_session, "_seen_event_keys", __import__("collections").OrderedDict())
    event = {"channel": "C1", "ts": "111.222"}
    assert copilot_session._is_duplicate_event(event) is False
    assert copilot_session._is_duplicate_event(event) is True


def test_is_duplicate_event_no_ts_never_duplicate(monkeypatch):
    monkeypatch.setattr(copilot_session, "_seen_event_keys", __import__("collections").OrderedDict())
    event = {"channel": "C1", "ts": ""}
    assert copilot_session._is_duplicate_event(event) is False
    assert copilot_session._is_duplicate_event(event) is False


def test_is_duplicate_event_bounded_fifo(monkeypatch):
    from collections import OrderedDict
    monkeypatch.setattr(copilot_session, "_seen_event_keys", OrderedDict())
    monkeypatch.setattr(copilot_session, "_SEEN_EVENT_MAX", 2)

    copilot_session._is_duplicate_event({"channel": "C1", "ts": "1"})
    copilot_session._is_duplicate_event({"channel": "C1", "ts": "2"})
    copilot_session._is_duplicate_event({"channel": "C1", "ts": "3"})

    assert len(copilot_session._seen_event_keys) == 2
    # Oldest entry ("1") should have been evicted, so it registers as new again.
    assert copilot_session._is_duplicate_event({"channel": "C1", "ts": "1"}) is False


def test_get_convo_creates_and_caches(monkeypatch):
    monkeypatch.setattr(copilot_session, "_convos", {})
    convo1 = copilot_session.get_convo("conv-1")
    convo2 = copilot_session.get_convo("conv-1")
    assert convo1 is convo2
    assert isinstance(convo1, CopilotConversation)
    assert convo1.conversation_id == "conv-1"


def test_get_convo_if_exists_returns_none_without_creating(monkeypatch):
    """watchers.py's busy-check can look up a conversation without the side effect
    of creating one: an unknown conversation id returns None and _convos stays
    unchanged (get_convo() would add an entry)."""
    monkeypatch.setattr(copilot_session, "_convos", {})
    assert copilot_session.get_convo_if_exists("conv-never-seen") is None
    assert "conv-never-seen" not in copilot_session._convos


def test_get_convo_if_exists_returns_existing_instance(monkeypatch):
    convo = CopilotConversation("conv-real")
    monkeypatch.setattr(copilot_session, "_convos", {"conv-real": convo})
    assert copilot_session.get_convo_if_exists("conv-real") is convo


def test_thread_root_uses_thread_ts_if_present():
    assert copilot_session._thread_root({"thread_ts": "100.1", "ts": "200.2"}) == "100.1"


def test_thread_root_falls_back_to_ts():
    assert copilot_session._thread_root({"ts": "200.2"}) == "200.2"


def test_slack_key_for_format():
    assert copilot_session._slack_key_for("C1", "100.1") == "C1:100.1"


def test_conversation_id_for_uses_binding_aware_slack_lookup(monkeypatch):
    calls = []

    def fake_get_or_create(platform, key):
        calls.append((platform, key))
        return "conv-uuid-1"

    monkeypatch.setattr(copilot_session.message_store, "get_or_create_bound_conversation", fake_get_or_create)
    result = copilot_session._conversation_id_for("C1", "100.1")
    assert result == "conv-uuid-1"
    assert calls == [(copilot_session.message_store.SLACK_PLATFORM, "C1:100.1")]


def test_is_new_conversation_true_when_no_memory_or_saved_session(monkeypatch):
    monkeypatch.setattr(copilot_session, "_convos", {})
    monkeypatch.setattr(copilot_session, "_get_saved_session_id", lambda conv_id: None)
    assert copilot_session._is_new_conversation("conv-x") is True


def test_is_new_conversation_false_if_in_memory(monkeypatch):
    monkeypatch.setattr(copilot_session, "_convos", {"conv-x": object()})
    monkeypatch.setattr(copilot_session, "_get_saved_session_id", lambda conv_id: None)
    assert copilot_session._is_new_conversation("conv-x") is False


def test_is_new_conversation_false_if_saved_session_exists(monkeypatch):
    monkeypatch.setattr(copilot_session, "_convos", {})
    monkeypatch.setattr(copilot_session, "_get_saved_session_id", lambda conv_id: "saved-id")
    assert copilot_session._is_new_conversation("conv-x") is False


# --- interrupt() -------------------------------------------------------------

def test_interrupt_returns_false_when_nothing_running():
    convo = CopilotConversation("conv-nothing")
    import asyncio
    result = asyncio.run(convo.interrupt())
    assert result is False


def test_interrupt_aborts_running_turn():
    convo = CopilotConversation("conv-running")

    class FakeSession:
        def __init__(self):
            self.aborted = False

        async def abort(self):
            self.aborted = True

    fake_session = FakeSession()
    convo._session = fake_session

    async def run():
        async with convo._lock:
            # Simulate a turn in progress by holding the lock while calling
            # interrupt() from a separate task.
            task = asyncio.create_task(convo.interrupt())
            await asyncio.sleep(0.05)
            return await task

    result = asyncio.run(run())
    assert result is True
    assert fake_session.aborted is True
    assert convo._interrupted is True


# --- is_busy() -----------------------------------------------------------

def test_is_busy_false_when_idle():
    convo = CopilotConversation("conv-idle")
    assert convo.is_busy() is False


def test_is_busy_true_while_turn_in_progress():
    """is_busy() lets watchers.py's _inject_watch_message() avoid injecting an
    automated message that would abort an in-progress turn. It is as coarse as
    send()'s own busy check (self._lock.locked()): a turn blocked on a long tool
    call (e.g. `sleep 120` in bash) still counts as busy."""
    convo = CopilotConversation("conv-busy")

    async def run():
        async with convo._lock:
            # While the lock is held (simulating an in-progress turn), a
            # concurrent busy-check must see True.
            busy_while_held = convo.is_busy()
            await asyncio.sleep(0.01)
            return busy_while_held

    busy_while_held = asyncio.run(run())
    assert busy_while_held is True
    # Lock released after the `async with` block exits — not busy.
    assert convo.is_busy() is False


# --- current_turn_generation() / last_completed_turn_generation() --------
#
# These back the session_turn watch type: a watch self-subscribed mid-turn
# captures the *in-flight* turn's own generation as its baseline (not a
# yet-to-exist "next" value), so that turn's own completion is skipped and
# only a later turn fires it.

def test_turn_generation_zero_when_never_started():
    convo = CopilotConversation("conv-fresh")
    assert convo.current_turn_generation() is None
    assert convo.last_completed_turn_generation() == 0


def test_current_turn_generation_none_when_idle():
    convo = CopilotConversation("conv-idle-gen")
    convo._turn_generation = 3
    convo._last_completed_turn_generation = 3
    convo.turn_started_at = None
    assert convo.current_turn_generation() is None
    assert convo.last_completed_turn_generation() == 3


def test_current_turn_generation_set_while_turn_in_progress():
    convo = CopilotConversation("conv-inflight-gen")
    convo._turn_generation = 1
    convo._last_completed_turn_generation = 0
    convo.turn_started_at = 123.0
    assert convo.current_turn_generation() == 1


def test_turn_generation_restored_from_message_store_on_construction():
    """A new CopilotConversation is seeded with the turn generation persisted in
    bot_store.db for its conversation_id (not 0), so a session_turn watch's
    baseline comparison stays correct across a bot restart (see watchers.py's
    _poll_session_turn_watch)."""
    conversation_id = message_store.get_or_create_conversation(None)
    message_store.set_turn_generation(conversation_id, 4)

    convo = CopilotConversation(conversation_id)

    assert convo.last_completed_turn_generation() == 4
    assert convo.current_turn_generation() is None  # restored idle, not mid-turn
    # The in-flight counter is seeded with the same value so the next turn
    # continues the sequence instead of going below the restored baseline.
    assert convo._turn_generation == 4


def test_turn_generation_restored_as_zero_for_conversation_never_persisted():
    conversation_id = message_store.get_or_create_conversation(None)
    convo = CopilotConversation(conversation_id)
    assert convo.last_completed_turn_generation() == 0


class _QueueingFakeSession:
    def __init__(self):
        self.send_calls: list[str] = []
        self.aborted = False
        self._release = asyncio.Event()

    async def abort(self):
        self.aborted = True
        self._release.set()

    async def send_and_wait(self, message, agent_mode, timeout):
        self.send_calls.append(message)
        await self._release.wait()
        return SimpleNamespace(data=AssistantMessageData(content=f"done: {message}", message_id=f"msg-{message}"))

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    def on(self, handler):
        return lambda: None

    @property
    def session_id(self):
        return "fake-session"


def test_newer_message_supersedes_preflight_turn_before_send_and_wait_starts(monkeypatch):
    """If a newer message arrives while the older turn is
    still in preflight (lock held, session not yet ready), the older turn must
    be dropped before it ever reaches send_and_wait(), so only the newest input
    gets processed."""
    convo = CopilotConversation("conv-preflight")
    fake_session = _QueueingFakeSession()
    send_started = asyncio.Event()

    async def fake_ensure_session():
        send_started.set()
        await asyncio.sleep(0.05)
        convo._session = fake_session

    monkeypatch.setattr(convo, "_ensure_session", fake_ensure_session)

    async def run():
        first = asyncio.create_task(convo.send("older"))
        await send_started.wait()
        second = asyncio.create_task(convo.send("newer"))
        await asyncio.sleep(0.08)
        fake_session._release.set()
        return await first, await second

    first_result, second_result = asyncio.run(run())
    assert first_result is copilot_session.INTERRUPTED
    assert second_result == "done: newer"
    assert fake_session.send_calls == ["newer"]


def test_interrupt_during_preflight_returns_interrupted_without_wiping_next_turn(monkeypatch):
    """Pressing Stop while a turn is still getting
    ready (before a Copilot session exists) must still interrupt that turn, and
    the next message must reuse the conversation instead of forcing a reset."""
    convo = CopilotConversation("conv-stop-preflight")
    fake_session = _QueueingFakeSession()
    send_started = asyncio.Event()

    async def fake_ensure_session():
        send_started.set()
        await asyncio.sleep(0.05)
        convo._session = fake_session

    monkeypatch.setattr(convo, "_ensure_session", fake_ensure_session)

    async def run():
        first = asyncio.create_task(convo.send("to-stop"))
        await send_started.wait()
        interrupted = await convo.interrupt()
        first_result = await first
        fake_session._release.clear()
        second = asyncio.create_task(convo.send("after-stop"))
        await asyncio.sleep(0.08)
        fake_session._release.set()
        second_result = await second
        return interrupted, first_result, second_result

    interrupted, first_result, second_result = asyncio.run(run())
    assert interrupted is True
    assert first_result is copilot_session.INTERRUPTED
    assert second_result == "done: after-stop"
    assert fake_session.send_calls == ["after-stop"]


def test_stop_then_new_message_race_does_not_swallow_new_message(monkeypatch):
    """Stop on turn A sets `_interrupted`. If a newer message B arrives (bumping the
    request-id counter) before A's send_and_wait() unblocks, A returns early as
    superseded. Both the superseded branch and the interrupted-flag branch clear
    `_interrupted`, so it never leaks into B and B's message still reaches
    send_and_wait()."""
    convo = CopilotConversation("conv-race")
    fake_session = _QueueingFakeSession()

    async def abort_without_releasing():
        # abort() does not synchronously unblock send_and_wait(), so the test
        # controls when turn A wakes up relative to turn B starting.
        fake_session.aborted = True

    monkeypatch.setattr(fake_session, "abort", abort_without_releasing)
    convo._session = fake_session

    async def fake_ensure_session():
        return None

    monkeypatch.setattr(convo, "_ensure_session", fake_ensure_session)

    async def run():
        first = asyncio.create_task(convo.send("A"))
        while "A" not in fake_session.send_calls:
            await asyncio.sleep(0.01)

        interrupted = await convo.interrupt()  # Stop pressed on A
        assert interrupted is True

        second = asyncio.create_task(convo.send("B"))
        # Let B's pre-lock logic (bumping _latest_request_id, calling
        # abort() again) run while B is still queued behind A's lock.
        await asyncio.sleep(0.05)

        # Unblock A -- it wakes inside send_and_wait with a now-stale
        # request_id (superseded by B).
        fake_session._release.set()
        first_result = await first
        second_result = await second
        return first_result, second_result

    first_result, second_result = asyncio.run(run())
    assert first_result is copilot_session.INTERRUPTED
    assert second_result == "done: B"
    assert fake_session.send_calls == ["A", "B"]


class _MultiStepFakeSession:
    """Fake session whose `send_and_wait()` simulates a real multi-step
    autopilot turn: it replays a sequence of assistant.message events to
    every handler registered via `on()` (as the real SDK does while a turn
    is in flight), then returns only the LAST of those events, like the real SDK.
    """

    def __init__(self, events_data: list[AssistantMessageData]):
        self._events_data = events_data
        self._handlers: list = []

    async def abort(self):
        pass

    async def send_and_wait(self, message, agent_mode, timeout):
        for data in self._events_data:
            event = SimpleNamespace(data=data)
            for handler in list(self._handlers):
                handler(event)
        return SimpleNamespace(data=self._events_data[-1])

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    def on(self, handler):
        self._handlers.append(handler)
        return lambda: self._handlers.remove(handler)

    @property
    def session_id(self):
        return "fake-multistep-session"


def test_send_reconstructs_full_report_dropped_by_trailing_tool_calls(monkeypatch):
    """A turn writes the full report as text, then makes more tool calls (sql,
    task_complete) whose assistant.message events are empty/short. `send()` returns
    exactly the full report, not the last (empty) event's content and not the
    report glued to other narration."""
    report_text = "# Yesterday's Activities — Full Status & Next Steps\n...full report..."
    final_req = AssistantMessageToolRequest(
        name="task_complete", tool_call_id="tc1", arguments={"summary": "Delivered report"},
    )
    fake_session = _MultiStepFakeSession([
        AssistantMessageData(content=report_text, message_id="m1"),
        AssistantMessageData(content="", message_id="m2", tool_requests=[final_req]),
    ])

    convo = CopilotConversation("conv-dropped-report")
    convo._session = fake_session

    async def fake_ensure_session():
        return None

    monkeypatch.setattr(convo, "_ensure_session", fake_ensure_session)

    result = asyncio.run(convo.send("run the daily report"))
    assert result == report_text
