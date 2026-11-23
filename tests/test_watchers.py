"""
Unit tests for watchers.py — Teams/GitLab/Jira/time watch polling, the
new-watch announcer, and the WATCHERS.md snapshot writer.

Live Slack app/client and the core _handle() turn engine are late-bound via
watchers.configure(app, handle_fn) — tests fake both with simple stand-ins
and call configure() directly rather than importing dop_agent (avoids
pulling in the whole bot's startup side effects just to test this module).
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

import message_store
import watchers
import copilot_session
from integrations.runtime import get_registry


def _fake_gitlab(client):
    """Install `client` as the gitlab integration's (shared) API client."""
    get_registry().get("gitlab").driver()._client = client


def _fake_gitlab_factory(factory):
    get_registry().get("gitlab").driver().make_client = factory


def _fake_jira(client):
    get_registry().get("jira").driver()._client = client


# --- fakes -------------------------------------------------------------

class FakeClient:
    def __init__(self):
        self.posted = []
        self.permalinks = {}

    async def chat_postMessage(self, **kwargs):
        self.posted.append(kwargs)
        return {"ts": "111.222", "channel": kwargs.get("channel")}

    async def chat_getPermalink(self, **kwargs):
        return {"permalink": self.permalinks.get(kwargs.get("message_ts"), "https://example/perma")}


class FakeApp:
    def __init__(self, client):
        self.client = client


@pytest.fixture(autouse=True)
def _reset_configure(tmp_path, monkeypatch):
    """Every test gets a fresh fake app + a recording handle_fn, and an
    isolated message_store DB (since _chat_post_and_record/_announce_new_
    watches write real "visible message" rows) so tests don't collide with
    each other or the developer's real bot_store.db. The late-bound
    watchers globals are reset afterward so tests don't leak state."""
    monkeypatch.setattr(message_store, "DB_PATH", tmp_path / "bot_store.db")
    monkeypatch.setattr(message_store, "_TITLES_FILE", tmp_path / "session_titles.json")
    message_store.init_db()

    client = FakeClient()
    app = FakeApp(client)
    calls = []

    async def fake_handle(**kwargs):
        calls.append(kwargs)

    watchers.configure(app, fake_handle)
    yield SimpleNamespace(app=app, client=client, calls=calls)
    watchers.configure(None, None)


def _slack_conversation_id(channel: str, thread_ts: str) -> str:
    return copilot_session._conversation_id_for(channel, thread_ts)


def _watch(*, channel: str = "C1", thread_ts: str = "100.1", conversation_id: str | None = None, **fields):
    watch = {
        "channel": channel,
        "thread_ts": thread_ts,
        "conversation_id": conversation_id or _slack_conversation_id(channel, thread_ts),
    }
    watch.update(fields)
    return watch


# --- _fmt_watch_ts -------------------------------------------------------

def test_fmt_watch_ts_none():
    assert watchers._fmt_watch_ts(None) == "n/a"


def test_fmt_watch_ts_empty_string():
    assert watchers._fmt_watch_ts("") == "n/a"


def test_fmt_watch_ts_valid_iso():
    assert watchers._fmt_watch_ts("2026-08-06T15:52:00.123456") == "2026-08-06 15:52 UTC"


def test_fmt_watch_ts_invalid_returns_raw():
    assert watchers._fmt_watch_ts("not-a-date") == "not-a-date"


# --- configure() late binding -------------------------------------------

def test_configure_sets_module_globals():
    client = FakeClient()
    app = FakeApp(client)

    async def handle_fn(**kwargs):
        pass

    watchers.configure(app, handle_fn)
    assert watchers._app is app
    assert watchers._handle_fn is handle_fn


# --- _inject_watch_message -------------------------------------------

def test_inject_watch_message_posts_and_calls_handle(_reset_configure):
    fixture = _reset_configure
    conversation_id = _slack_conversation_id("C1", "100.1")

    async def run():
        await watchers._inject_watch_message(conversation_id, "hello from watcher")

    asyncio.run(run())

    assert len(fixture.client.posted) == 1
    assert fixture.client.posted[0]["text"] == "hello from watcher"
    assert fixture.client.posted[0]["channel"] == "C1"
    assert fixture.client.posted[0]["thread_ts"] == "100.1"

    assert len(fixture.calls) == 1
    assert fixture.calls[0]["text"] == "hello from watcher"
    assert fixture.calls[0]["thread_key"]["channel"] == "C1"
    assert fixture.calls[0]["thread_ts"] == "100.1"
    assert fixture.calls[0]["conversation_id"] == conversation_id
    assert fixture.calls[0]["record_user_message"] is False


def test_inject_watch_message_still_calls_handle_if_post_fails(_reset_configure, monkeypatch):
    fixture = _reset_configure
    conversation_id = _slack_conversation_id("C1", "100.1")

    async def broken_post(**kwargs):
        raise RuntimeError("slack down")

    fixture.client.chat_postMessage = broken_post

    async def run():
        await watchers._inject_watch_message(conversation_id, "hello")

    asyncio.run(run())
    # Even though posting the raw notice failed, the agent should still be
    # nudged with the text so it isn't silently lost.
    assert len(fixture.calls) == 1


def test_inject_watch_message_delivers_via_web_for_web_ui_only_conversation(_reset_configure):
    """A Web-UI-only conversation has no Slack (channel, thread_ts) identity, so a
    watch fire into it delivers via the Web UI path only, while still waking the
    correct conversation_id."""
    fixture = _reset_configure
    conversation_id = message_store.get_or_create_conversation(None)

    asyncio.run(watchers._inject_watch_message(conversation_id, "web-only watcher fired"))

    assert fixture.client.posted == []
    messages = message_store.get_messages(conversation_id)
    assert any(m["role"] == "system" and m["content"] == "web-only watcher fired" for m in messages)
    assert len(fixture.calls) == 1
    assert fixture.calls[0]["text"] == "web-only watcher fired"
    assert "channel" not in fixture.calls[0]
    assert fixture.calls[0]["thread_ts"] is None
    assert "client" not in fixture.calls[0]
    assert fixture.calls[0]["conversation_id"] == conversation_id


def test_inject_watch_message_enriches_text_when_conversation_busy(_reset_configure, monkeypatch):
    """A watcher fire landing while a thread is mid-turn interrupts that turn, so
    _inject_watch_message() detects the busy conversation and enriches the
    injected text with a note quoting the user's in-progress task and stating
    the watcher event supersedes it (address it now, resume the old task
    afterward only if still relevant)."""
    fixture = _reset_configure
    conversation_id = watchers._conversation_id_for("C1", "100.1")
    message_store.get_or_create_conversation(None)  # unrelated row, ensures init
    message_store.record_message(conversation_id, "user", "please deploy the stg environment")

    convo = copilot_session.get_convo(conversation_id)

    async def run():
        async with convo._lock:
            await watchers._inject_watch_message(conversation_id, "pipeline #123 succeeded")

    asyncio.run(run())

    assert len(fixture.calls) == 1
    text = fixture.calls[0]["text"]
    assert "pipeline #123 succeeded" in text
    assert "please deploy the stg environment" in text
    assert "supersedes" in text


def test_inject_watch_message_leaves_text_unchanged_when_conversation_idle(_reset_configure):
    """Control case for the busy-enrichment: a watcher firing into an idle
    thread gets no enrichment added — the busy-check is a no-op when there's
    nothing to protect."""
    fixture = _reset_configure
    conversation_id = watchers._conversation_id_for("C1", "100.1")
    message_store.record_message(conversation_id, "user", "please deploy the stg environment")
    # Conversation exists in message_store but no CopilotConversation object
    # has been created for it in this process — same as watcher fires that
    # land right after a restart, before any turn has run yet.

    asyncio.run(watchers._inject_watch_message(conversation_id, "pipeline #123 succeeded"))

    assert fixture.calls[0]["text"] == "pipeline #123 succeeded"


def test_inject_watch_message_no_enrichment_when_convo_exists_but_idle(_reset_configure):
    fixture = _reset_configure
    conversation_id = watchers._conversation_id_for("C1", "100.1")
    message_store.record_message(conversation_id, "user", "please deploy the stg environment")
    copilot_session.get_convo(conversation_id)  # touch it, but never lock it

    asyncio.run(watchers._inject_watch_message(conversation_id, "pipeline #123 succeeded"))

    assert fixture.calls[0]["text"] == "pipeline #123 succeeded"


# --- _last_turn_snippets / build_resume_instruction ---------------------

def test_last_turn_snippets_returns_last_user_and_assistant():
    conversation_id = message_store.get_or_create_conversation(None)
    message_store.record_message(conversation_id, "user", "first request")
    message_store.record_message(conversation_id, "assistant", "first reply")
    message_store.record_message(conversation_id, "user", "please deploy the stg environment")
    message_store.record_message(conversation_id, "assistant", "Retrying.")

    last_user, last_assistant = watchers._last_turn_snippets(conversation_id)
    assert last_user == "please deploy the stg environment"
    assert last_assistant == "Retrying."


def test_last_turn_snippets_truncates_long_text():
    conversation_id = message_store.get_or_create_conversation(None)
    message_store.record_message(conversation_id, "user", "x" * 500)

    last_user, _ = watchers._last_turn_snippets(conversation_id, max_len=50)
    assert len(last_user) == 51  # 50 chars + trailing ellipsis
    assert last_user.endswith("…")


def test_last_turn_snippets_missing_conversation_returns_none_none():
    last_user, last_assistant = watchers._last_turn_snippets("no-such-conversation")
    assert last_user is None
    assert last_assistant is None


def test_build_resume_instruction_quotes_last_user_and_assistant():
    """The resume prompt quotes the exact cut-off last user request + last
    assistant message (not a generic "check status" prompt), so the agent
    doesn't restate a fragment ("Retrying.") as a finished result. The quoted
    context must appear in the final prompt alongside RESUME_STATUS_INSTRUCTION."""
    conversation_id = message_store.get_or_create_conversation(None)
    message_store.record_message(conversation_id, "user", "please deploy the stg environment")
    message_store.record_message(conversation_id, "assistant", "Retrying.")

    text = watchers.build_resume_instruction(conversation_id)
    assert "please deploy the stg environment" in text
    assert "Retrying." in text
    assert watchers.RESUME_STATUS_INSTRUCTION in text


def test_build_resume_instruction_falls_back_to_bare_instruction_when_no_history():
    text = watchers.build_resume_instruction("brand-new-conversation-id")
    assert text == watchers.RESUME_STATUS_INSTRUCTION


# --- _fire_self_restart_watches -------------------------------------

def test_fire_self_restart_watches_marks_done_before_resume_attempt(_reset_configure, monkeypatch):
    """set_status(done) is applied before _inject_watch_message() (which can
    take a full agent turn), so a process killed mid-resume doesn't leave the
    watch 'active' and re-firing on every subsequent restart."""
    fixture = _reset_configure
    order = []
    monkeypatch.setattr(watchers, "due_self_restart_watches",
                         lambda boot_id: [_watch(id=42, channel="C1", thread_ts="1.1", label="test")])
    monkeypatch.setattr(watchers, "set_status",
                         lambda wid, status: order.append(("set_status", wid, status)))

    orig_inject = watchers._inject_watch_message

    async def spy_inject(conversation_id, text):
        order.append(("inject", conversation_id))
        return await orig_inject(conversation_id, text)

    monkeypatch.setattr(watchers, "_inject_watch_message", spy_inject)

    asyncio.run(watchers._fire_self_restart_watches("new-boot-id"))

    assert order[0] == ("set_status", 42, "done")
    assert order[1][0] == "inject"


def test_fire_self_restart_watches_marks_done_even_if_inject_fails(_reset_configure, monkeypatch):
    monkeypatch.setattr(watchers, "due_self_restart_watches",
                         lambda boot_id: [_watch(id=43, channel="C1", thread_ts="1.1", label=None)])
    marked = []
    monkeypatch.setattr(watchers, "set_status",
                         lambda wid, status: marked.append((wid, status)))

    async def broken_inject(conversation_id, text):
        raise RuntimeError("resume failed")

    monkeypatch.setattr(watchers, "_inject_watch_message", broken_inject)

    asyncio.run(watchers._fire_self_restart_watches("new-boot-id"))
    assert marked == [(43, "done")]


def test_fire_self_restart_watches_prompt_includes_status_instruction(_reset_configure, monkeypatch):
    """The prompt fed into the agent on a self_restart resume must ask it to
    check/report progress (done vs pending) and continue autonomously if
    work remains — not just a generic 'resuming as requested' ack that
    gives the agent no explicit instruction to self-report or continue."""
    fixture = _reset_configure
    monkeypatch.setattr(watchers, "due_self_restart_watches",
                         lambda boot_id: [_watch(id=44, channel="C1", thread_ts="1.1", label="my label")])
    monkeypatch.setattr(watchers, "set_status", lambda wid, status: None)

    asyncio.run(watchers._fire_self_restart_watches("new-boot-id"))

    assert len(fixture.calls) == 1
    text = fixture.calls[0]["text"]
    assert "my label" in text
    assert watchers.RESUME_STATUS_INSTRUCTION in text


def test_fire_self_restart_watches_no_due_watches(_reset_configure, monkeypatch):
    fixture = _reset_configure
    monkeypatch.setattr(watchers, "due_self_restart_watches", lambda boot_id: [])
    asyncio.run(watchers._fire_self_restart_watches("new-boot-id"))
    assert fixture.calls == []


def test_resume_status_instruction_guards_against_announced_but_unexecuted_plans():
    """RESUME_STATUS_INSTRUCTION tells the agent, on every restart/resume
    wake-up, to actually execute/continue any announced-but-unverified plan
    rather than just repeating that it's still "planned"."""
    text = watchers.RESUME_STATUS_INSTRUCTION.lower()
    assert "unfinished plan" in text
    assert "actually execute" in text or "actually run" in text


# --- _report_broken_watch -------------------------------------------

def test_report_broken_watch_posts_and_marks_broken(_reset_configure, monkeypatch):
    fixture = _reset_configure
    marked = []
    monkeypatch.setattr(watchers, "mark_broken", lambda wid, reason: marked.append((wid, reason)))

    w = _watch(id=7, type="mr")

    async def run():
        await watchers._report_broken_watch(w, "bad key format")

    asyncio.run(run())

    assert len(fixture.client.posted) == 1
    assert "bad key format" in fixture.client.posted[0]["text"]
    assert marked == [(7, "bad key format")]


# --- _poll_time_watch -------------------------------------------

def test_poll_time_watch_not_due_yet(_reset_configure, monkeypatch):
    fixture = _reset_configure
    from datetime import datetime, timedelta
    future = (datetime.now() + timedelta(hours=1)).isoformat()
    w = {"id": 1, "key": "in:1h", "label": "check later",
         "meta": json.dumps({"next_fire_at": future})}
    monkeypatch.setattr(watchers, "parse_time_key", lambda k: {"kind": "in"})

    asyncio.run(watchers._poll_time_watch(w))
    assert fixture.calls == []
    assert fixture.client.posted == []


def test_poll_time_watch_fires_one_shot(_reset_configure, monkeypatch):
    fixture = _reset_configure
    from datetime import datetime, timedelta
    past = (datetime.now() - timedelta(minutes=1)).isoformat()
    w = _watch(id=2, key="in:1m", label="ping me", meta=json.dumps({"next_fire_at": past}))
    monkeypatch.setattr(watchers, "parse_time_key", lambda k: {"kind": "in"})
    monkeypatch.setattr(watchers, "is_recurring_time_kind", lambda kind: False)
    done_calls = []
    monkeypatch.setattr(watchers, "set_status", lambda wid, status: done_calls.append((wid, status)))

    asyncio.run(watchers._poll_time_watch(w))

    assert len(fixture.calls) == 1
    assert "ping me" in fixture.calls[0]["text"]
    assert done_calls == [(2, "done")]


def test_poll_time_watch_recurring_advances_next_fire(_reset_configure, monkeypatch):
    fixture = _reset_configure
    from datetime import datetime, timedelta
    past = (datetime.now() - timedelta(minutes=1)).isoformat()
    new_next = datetime.now() + timedelta(days=1)
    w = _watch(id=3, key="cron:daily 09:00", label="daily check", meta=json.dumps({"next_fire_at": past}))
    monkeypatch.setattr(watchers, "parse_time_key", lambda k: {"kind": "cron_daily"})
    monkeypatch.setattr(watchers, "is_recurring_time_kind", lambda kind: True)
    monkeypatch.setattr(watchers, "advance_recurring", lambda parsed, next_fire, now: new_next)
    set_meta_calls = []
    monkeypatch.setattr(watchers, "set_meta", lambda wid, meta: set_meta_calls.append((wid, meta)))

    asyncio.run(watchers._poll_time_watch(w))

    assert len(fixture.calls) == 1
    assert len(set_meta_calls) == 1
    assert set_meta_calls[0][1]["next_fire_at"] == new_next.isoformat()


def test_poll_time_watch_broken_key(_reset_configure, monkeypatch):
    fixture = _reset_configure
    monkeypatch.setattr(watchers, "parse_time_key",
                         lambda k: (_ for _ in ()).throw(ValueError("bad key")))
    reported = []
    monkeypatch.setattr(watchers, "mark_broken", lambda wid, reason: reported.append((wid, reason)))
    w = _watch(id=4, type="time", key="garbage", label="x")

    asyncio.run(watchers._poll_time_watch(w))
    assert reported == [(4, "bad key")]
    assert fixture.calls == []


# --- _poll_job_watch -----------------------------------------------------

class FakeGitLabClientForJob:
    """Stand-in for gitlab_poll.GitLabClient in job-watch tests — only
    implements get_job(), which is all _poll_job_watch calls."""

    def __init__(self, job):
        self._job = job

    def get_job(self, project, job_id):
        return self._job


def _patch_job_gitlab_client(monkeypatch, job):
    """Wire watchers._get_gitlab_client()/_load_gitlab_poll_module() to a
    fake client + a fake module exposing JOB_TERMINAL, without touching the
    real gitlab_poll.py or hitting the network."""
    fake_module = SimpleNamespace(JOB_TERMINAL={"success", "failed", "canceled", "skipped"})
    _fake_gitlab(FakeGitLabClientForJob(job))


def test_poll_job_watch_does_not_refire_same_terminal_status(_reset_configure, monkeypatch):
    """Once a job's terminal status has already been notified (tracked via
    last_state), polling again with the exact same status AND finished_at
    must not post again — mirrors _poll_pipeline_watch's re-run dedup
    behavior."""
    fixture = _reset_configure
    job = {"id": 9876, "status": "failed", "name": "deploy:stg", "stage": "deploy", "web_url": "",
           "finished_at": "2026-09-22T10:00:00.000Z"}
    _patch_job_gitlab_client(monkeypatch, job)
    monkeypatch.setattr(watchers, "set_last_state", lambda wid, state: None)
    w = _watch(id=11, type="job", key="group/project#9876", label="deploy job",
         last_state=json.dumps({
             "job_id": 9876, "last_status": "failed", "finished_at": "2026-09-22T10:00:00.000Z",
         }))

    asyncio.run(watchers._poll_integration_watch(w))

    assert fixture.calls == []


def test_poll_job_watch_skips_while_job_still_running(_reset_configure, monkeypatch):
    """A job in a non-terminal state (e.g. 'running') must not fire yet —
    only PIPELINE-analogous terminal states should trigger a notification."""
    fixture = _reset_configure
    job = {"id": 9876, "status": "running", "name": "deploy:stg", "stage": "deploy", "web_url": "",
           "finished_at": None}
    _patch_job_gitlab_client(monkeypatch, job)
    monkeypatch.setattr(watchers, "set_last_state", lambda wid, state: None)
    w = _watch(id=12, type="job", key="group/project#9876", label="deploy job", last_state="{}")

    asyncio.run(watchers._poll_integration_watch(w))

    assert fixture.calls == []


# --- _poll_pipeline_watch --------------------------------------------------

class FakeGitLabClientForPipeline:
    """Stand-in for gitlab_poll.GitLabClient in pipeline-watch tests — only
    implements get_pipeline()/list_pipelines_for_ref(), which is all
    _poll_pipeline_watch calls."""

    def __init__(self, pipeline=None, pipelines_for_ref=None):
        self._pipeline = pipeline
        self._pipelines_for_ref = pipelines_for_ref or []

    def get_pipeline(self, project, pipeline_id):
        return self._pipeline

    def list_pipelines_for_ref(self, project, ref=None, sha=None):
        return self._pipelines_for_ref


def _patch_pipeline_gitlab_client(monkeypatch, pipeline=None, pipelines_for_ref=None):
    fake_module = SimpleNamespace(PIPELINE_TERMINAL={"success", "failed", "canceled", "skipped"})
    _fake_gitlab(FakeGitLabClientForPipeline(pipeline=pipeline, pipelines_for_ref=pipelines_for_ref),)


def test_poll_pipeline_watch_does_not_refire_same_run(_reset_configure, monkeypatch):
    """Polling again with the literal same run (same id/status/finished_at)
    must not post a duplicate notification."""
    fixture = _reset_configure
    pipeline = {"id": 5678, "status": "failed", "web_url": "",
                "finished_at": "2026-09-22T10:00:00.000Z"}
    _patch_pipeline_gitlab_client(monkeypatch, pipeline=pipeline)
    monkeypatch.setattr(watchers, "set_last_state", lambda wid, state: None)
    w = _watch(id=22, type="pipeline", key="group/project#5678", label="stg deploy",
         last_state=json.dumps({
             "pipeline_id": 5678, "last_status": "failed", "finished_at": "2026-09-22T10:00:00.000Z",
         }))

    asyncio.run(watchers._poll_integration_watch(w))

    assert fixture.calls == []


def test_poll_pipeline_watch_skips_while_running_and_does_not_touch_last_state(_reset_configure, monkeypatch):
    """A non-terminal poll (e.g. 'running') must not fire AND must not
    overwrite last_state — last_state now only tracks the last *fired* run,
    not the last observed tick, so a missed intermediate poll can't reset
    the dedup baseline."""
    fixture = _reset_configure
    pipeline = {"id": 5678, "status": "running", "web_url": "", "finished_at": None}
    _patch_pipeline_gitlab_client(monkeypatch, pipeline=pipeline)
    last_state_calls = []
    monkeypatch.setattr(watchers, "set_last_state", lambda wid, state: last_state_calls.append((wid, state)))
    w = _watch(id=23, type="pipeline", key="group/project#5678", label="stg deploy",
         last_state=json.dumps({
             "pipeline_id": 5678, "last_status": "failed", "finished_at": "2026-09-22T10:00:00.000Z",
         }))

    asyncio.run(watchers._poll_integration_watch(w))

    assert fixture.calls == []
    assert last_state_calls == []


# --- _is_gitlab_not_found_error / 404-triggered broken-watch reporting ----
#
# A `pipeline` watch whose key 'group/project#106' holds an MR *iid* (not a
# pipeline id; the numbering is unrelated) gets a permanent 404 from GitLab on
# every poll. A confirmed 404 (not a transient network blip) is routed through
# _report_broken_watch exactly once, like a malformed key.

class _FakeGitLabClientRaisingNotFound:
    """Raises on every call, formatted like gitlab_poll.ConfigError does for
    a real GitLab 404 (see GitLabClient._get) - used to simulate 'this
    pipeline/job/MR id genuinely doesn't exist in this project'."""

    def __init__(self, path):
        self._path = path

    def _raise(self):
        raise RuntimeError(f"GitLab API error 404 for {self._path}: {{\"message\":\"404 Not found\"}}")

    def get_pipeline(self, project, pipeline_id):
        self._raise()

    def list_pipelines_for_ref(self, project, ref=None, sha=None):
        self._raise()

    def get_job(self, project, job_id):
        self._raise()

    def list_mr_notes(self, project, mr_iid):
        self._raise()


# --- _announce_new_watches -------------------------------------------

def test_announce_new_watches_skips_already_announced(_reset_configure):
    fixture = _reset_configure
    w = _watch(id=1, type="mr", channel="C1", thread_ts="1.1",
         meta=json.dumps({"announced": True}))
    asyncio.run(watchers._announce_new_watches([w]))
    assert fixture.client.posted == []


def test_announce_new_watches_posts_and_marks_announced(_reset_configure, monkeypatch):
    fixture = _reset_configure
    w = _watch(id=2, type="pipeline", channel="C1", thread_ts="1.1",
         key="group/proj#5", label="watch it", created_at=None,
         meta="{}")
    set_meta_calls = []
    monkeypatch.setattr(watchers, "set_meta", lambda wid, meta: set_meta_calls.append((wid, meta)))

    asyncio.run(watchers._announce_new_watches([w]))

    assert len(fixture.client.posted) == 1
    assert "New watcher registered" in fixture.client.posted[0]["text"]
    assert set_meta_calls == [(2, {"announced": True})]


def test_announce_new_watches_marks_orphan_watch_broken_instead_of_retrying(_reset_configure, monkeypatch):
    """A watch whose conversation row no longer exists would hit a FOREIGN KEY
    failure on every tick forever; it must be marked broken once instead."""
    fixture = _reset_configure
    w = _watch(id=7, type="time", conversation_id="gone-conversation", meta="{}")
    broken = []
    monkeypatch.setattr(watchers, "mark_broken", lambda wid, reason: broken.append((wid, reason)))

    asyncio.run(watchers._announce_new_watches([w]))

    assert [b[0] for b in broken] == [7]
    assert fixture.client.posted == []


def test_announce_new_watches_keeps_unannounced_on_post_failure(_reset_configure):
    fixture = _reset_configure

    async def broken_post(**kwargs):
        raise RuntimeError("down")

    fixture.client.chat_postMessage = broken_post
    w = _watch(id=3, type="teams", channel="C1", thread_ts="1.1", meta="{}")
    # Should not raise even though posting fails.
    asyncio.run(watchers._announce_new_watches([w]))


# --- _write_watchers_md -------------------------------------------

def test_write_watchers_md_no_watches(tmp_path, monkeypatch, _reset_configure):
    md_path = tmp_path / "WATCHERS.md"
    monkeypatch.setattr(watchers, "WATCHERS_MD_PATH", md_path)
    asyncio.run(watchers._write_watchers_md([]))
    content = md_path.read_text()
    assert "no active watches" in content


def test_write_watchers_md_with_watch(tmp_path, monkeypatch, _reset_configure):
    md_path = tmp_path / "WATCHERS.md"
    monkeypatch.setattr(watchers, "WATCHERS_MD_PATH", md_path)
    w = {"id": 9, "type": "mr", "status": "active", "channel": "C1",
         "thread_ts": "1.1", "label": "my mr", "key": "group/proj#5",
         "created_at": None, "updated_at": None}
    asyncio.run(watchers._write_watchers_md([w]))
    content = md_path.read_text()
    assert "Watch #9" in content
    assert "my mr" in content
    assert "group/proj#5" in content


# --- check_watch_resource_exists (registration-time existence validation) --
#
# A watch registered with a wrong MR iid, pipeline id or job id is rejected at
# add_watch() time with a clear, actionable message.

class _FakeGitLabClientResourceCheck:
    """Stand-in for gitlab_poll.GitLabClient for check_watch_resource_exists
    tests. `exists=False` simulates a confirmed GitLab 404 (formatted like
    the real GitLabClient._get does); `raise_non_404=True` simulates a
    transient failure (5xx/timeout/etc.) that must NOT block registration."""

    def __init__(self, exists=True, raise_non_404=False):
        self._exists = exists
        self._raise_non_404 = raise_non_404

    def _resolve(self, path):
        if self._raise_non_404:
            raise RuntimeError(f"GitLab API error 500 for {path}: boom")
        if not self._exists:
            raise RuntimeError(f"GitLab API error 404 for {path}: {{\"message\":\"404 Not found\"}}")
        return {"id": 1}

    def get_merge_request(self, project, mr_iid):
        return self._resolve(f"/projects/x/merge_requests/{mr_iid}")

    def get_pipeline(self, project, pipeline_id):
        return self._resolve(f"/projects/x/pipelines/{pipeline_id}")

    def get_job(self, project, job_id):
        return self._resolve(f"/projects/x/jobs/{job_id}")


def test_check_watch_resource_exists_allows_through_pipeline_ref_form(monkeypatch):
    """The '@ref' pipeline form is never existence-checked - an empty
    result for a brand new branch with no pipelines yet is legitimate, not
    an error, so no GitLab call should even be attempted."""
    def _boom():
        raise AssertionError("should not be called for the @ref form")
    _fake_gitlab_factory(_boom)
    watchers.check_watch_resource_exists("pipeline", "group/project@main")  # must not raise


def test_check_watch_resource_exists_allows_through_on_transient_error(monkeypatch):
    """A non-404 failure (network blip, 500, timeout) must not block
    registration - only a confirmed 404 is a real, permanent 'this id is
    wrong' signal; anything else just logs a warning and lets it through."""
    _fake_gitlab(_FakeGitLabClientResourceCheck(raise_non_404=True))
    watchers.check_watch_resource_exists("mr", "group/project#1")  # must not raise


def test_check_watch_resource_exists_allows_through_when_client_unavailable(monkeypatch):
    """If the GitLab client itself can't be constructed (missing token,
    import error, etc.) registration must still proceed - existence
    checking is a best-effort nicety, not a hard gate."""
    def _boom():
        raise RuntimeError("no token configured")
    _fake_gitlab_factory(_boom)
    watchers.check_watch_resource_exists("job", "group/project#1")  # must not raise


def test_check_watch_resource_exists_accepts_real_resource(monkeypatch):
    """The success path: a real, existing resource must not raise."""
    _fake_gitlab(_FakeGitLabClientResourceCheck(exists=True))
    watchers.check_watch_resource_exists("pipeline", "group/project#106")  # must not raise


def test_add_watch_rejects_nonexistent_mr(tmp_path, monkeypatch):
    """End-to-end: add_watch() itself (not just the helper in isolation)
    must reject registering an mr watch for a confirmed-nonexistent MR
    before it's ever written to the watches DB."""
    monkeypatch.setattr(watchers, "DB_PATH", tmp_path / "watchers.db")
    watchers.init_db()
    _fake_gitlab(_FakeGitLabClientResourceCheck(exists=False))
    with pytest.raises(ValueError):
        conversation_id = _slack_conversation_id("C1", "100.1")
        watchers.add_watch("mr", conversation_id, key="group/project#999", announce=False)
    assert watchers.list_watches(conversation_id=conversation_id) == []


# --- _poll_mr_watch pipeline tracking --------------------------------------
#
# Design: an 'mr' watch now also tracks its own MR's latest pipeline (via
# list_mr_pipelines, deliberately NOT list_pipelines_for_ref - a branch can
# accumulate multiple unrelated pipelines, only the MR-scoped endpoint
# unambiguously returns "the pipeline(s) GitLab associates with this exact
# MR") and fires a distinct Slack message when that pipeline reaches a new
# terminal status - so a single mr watch covers both "new review comment"
# and "did my MR's pipeline finish" without a second, separately-registered
# pipeline watch.

class FakeGitLabClientForMrPipeline:
    """Stand-in supporting list_mr_notes (returns [] so only the pipeline
    half is exercised in isolation) and list_mr_pipelines (returns a fixed,
    already-newest-first list, mirroring the real GitLab endpoint)."""

    def __init__(self, pipelines):
        self._pipelines = pipelines

    def list_mr_notes(self, project, mr_iid):
        return []

    def list_mr_pipelines(self, project, mr_iid):
        return self._pipelines


def _patch_mr_pipeline_gitlab_client(monkeypatch, pipelines):
    fake_module = SimpleNamespace(
        PIPELINE_TERMINAL={"success", "failed", "canceled", "skipped"},
        parse_gitlab_ts=lambda s: __import__("datetime").datetime.fromisoformat(s),
    )
    _fake_gitlab(FakeGitLabClientForMrPipeline(pipelines))


def test_poll_mr_watch_does_not_refire_same_pipeline_run(_reset_configure, monkeypatch):
    """Polling again with the same (pipeline_id, status, finished_at) must
    not post a duplicate pipeline-status message - same dedup guarantee as
    a standalone pipeline watch, reused via the shared helper."""
    fixture = _reset_configure
    pipeline = {"id": 5000, "status": "failed", "web_url": "",
                "finished_at": "2026-09-23T09:00:00.000Z"}
    _patch_mr_pipeline_gitlab_client(monkeypatch, pipelines=[pipeline])
    monkeypatch.setattr(watchers, "set_last_state", lambda wid, state: None)
    w = _watch(id=31, type="mr", key="group/project#42", label="!42",
         last_state=json.dumps({
             "last_seen_at": "2026-09-22T00:00:00+00:00",
             "pipeline": {"pipeline_id": 5000, "last_status": "failed",
                          "finished_at": "2026-09-23T09:00:00.000Z"},
         }),
         meta="{}", created_at=None)

    asyncio.run(watchers._poll_integration_watch(w))

    assert fixture.calls == []


# --- watcher_loop tick ordering + _poll_watch_group concurrency ---------
#
# Tick ordering: `time` watches (pure local timestamp checks) are checked first
# in watcher_loop(), so a slow network-bound section (teams/mr/pipeline/job/
# ticket) cannot delay them; every network-bound group is polled concurrently
# via _poll_watch_group(), so a slow group doesn't stack up latency per watch.

class _StopWatcherLoop(Exception):
    """Sentinel used to break out of watcher_loop()'s `while True` after
    exactly one tick, by raising from a patched asyncio.sleep() on the
    second iteration. watcher_loop() only wraps the tick *body* in its own
    try/except Exception — the `await asyncio.sleep(...)` call itself is
    outside that block, so this propagates out of the coroutine instead of
    being swallowed and logged like a tick failure."""


def _make_watch(watch_id: int, type_: str) -> dict:
    return _watch(id=watch_id, type=type_, status="active", key=None,
            label=None, created_at=None, meta="{}", last_state=None)


def test_time_watches_polled_before_network_bound_groups(monkeypatch):
    """watcher_loop() checks `time` watches first, before teams/mr/
    pipeline/job/ticket, so network latency can't delay them. The recorded poll
    order starts with "time" and only then contains the network-bound groups
    (order among those doesn't matter; they run concurrently via
    _poll_watch_group())."""
    order = []

    async def record(name):
        order.append(name)

    watches = [
        _make_watch(1, "time"),
        _make_watch(2, "teams"),
        _make_watch(3, "mr"),
        _make_watch(4, "pipeline"),
        _make_watch(5, "job"),
        _make_watch(6, "ticket"),
    ]

    monkeypatch.setattr(watchers, "list_watches", lambda *a, **kw: watches)
    monkeypatch.setattr(watchers, "_announce_new_watches", lambda w: record("announce"))
    monkeypatch.setattr(watchers, "_write_watchers_md", lambda w: record("write_md"))
    monkeypatch.setattr(watchers, "_poll_time_watch", lambda w: record("time"))
    monkeypatch.setattr(watchers, "_poll_integration_watch", lambda w: record(w["type"]))

    sleep_calls = {"n": 0}

    async def fake_sleep(_):
        sleep_calls["n"] += 1
        if sleep_calls["n"] > 1:
            raise _StopWatcherLoop()

    monkeypatch.setattr(watchers.asyncio, "sleep", fake_sleep)

    with pytest.raises(_StopWatcherLoop):
        asyncio.run(watchers.watcher_loop())

    assert order[0] == "announce"
    assert order[1] == "time", f"expected 'time' polled right after announce, got order={order}"
    assert set(order[2:-1]) == {"teams", "mr", "pipeline", "job", "ticket"}
    assert order[-1] == "write_md"


def test_poll_watch_group_runs_watches_concurrently_not_sequentially(monkeypatch):
    """_poll_watch_group() runs a group's watches concurrently
    (asyncio.gather): N watches each taking ~T seconds take ~T seconds total
    (not N*T), so one slow group doesn't delay unrelated watches in the same
    tick."""
    async def slow_poll(w):
        await asyncio.sleep(0.2)

    watches = [_make_watch(i, "mr") for i in range(5)]

    async def run():
        start = asyncio.get_event_loop().time()
        await watchers._poll_watch_group(slow_poll, watches, "mr")
        return asyncio.get_event_loop().time() - start

    elapsed = asyncio.run(run())
    # 5 watches * 0.2s would be ~1.0s if sequential; concurrent should stay
    # close to a single 0.2s call. Generous upper bound for CI slowness.
    assert elapsed < 0.6, f"expected concurrent polling (~0.2s), took {elapsed:.2f}s"


def test_poll_watch_group_one_failing_watch_does_not_block_or_skip_others(monkeypatch):
    """A single watch raising inside its poll call is caught and
    logged, and does NOT prevent the other watches in the same group from
    being polled (asyncio.gather() would otherwise cancel/orphan siblings on
    the first unhandled exception)."""
    polled = []

    async def flaky_poll(w):
        if w["id"] == 2:
            raise RuntimeError("simulated GitLab failure")
        polled.append(w["id"])

    watches = [_make_watch(1, "mr"), _make_watch(2, "mr"), _make_watch(3, "mr")]

    asyncio.run(watchers._poll_watch_group(flaky_poll, watches, "mr"))

    assert sorted(polled) == [1, 3]


def test_poll_watch_group_does_not_cancel_a_slow_poll_fn(monkeypatch):
    """_poll_watch_group() lets a poll_fn run as long as it needs: a fire can
    kick off an open-ended agent turn (bounded only by SESSION_TIMEOUT), which
    must not be cancelled by WATCH_POLL_TIMEOUT mid-turn. That timeout applies
    only to the individual network fetches inside each poll_fn (see
    test_fetch_with_timeout_abandons_a_hung_network_call)."""
    monkeypatch.setattr(watchers, "WATCH_POLL_TIMEOUT", 0.05)
    finished = []

    async def slow_but_legitimate(w):
        # Simulates a poll_fn whose fire triggered a real (slower than
        # WATCH_POLL_TIMEOUT) agent turn via _inject_watch_message — this
        # must be allowed to run to completion, not cancelled.
        await asyncio.sleep(0.2)
        finished.append(w["id"])

    watches = [_make_watch(1, "mr")]

    async def run():
        start = asyncio.get_event_loop().time()
        await watchers._poll_watch_group(slow_but_legitimate, watches, "mr")
        return asyncio.get_event_loop().time() - start

    elapsed = asyncio.run(run())
    assert elapsed >= 0.2, f"poll_fn was cancelled early (elapsed={elapsed:.3f}s), expected it to run to completion"
    assert finished == [1], "poll_fn must complete even though it ran past WATCH_POLL_TIMEOUT"


def test_fetch_with_timeout_abandons_a_hung_network_call(monkeypatch):
    """_fetch_with_timeout() bounds a single blocking GitLab/Jira client call
    (e.g. get_pipeline) by WATCH_POLL_TIMEOUT, in case a broken DNS/VPN state
    hangs longer than the HTTP client's own timeout. It is scoped to just the
    fetch, so a hang must still time out without touching anything downstream
    (no agent turn has been triggered yet at that point)."""
    monkeypatch.setattr(watchers, "WATCH_POLL_TIMEOUT", 0.05)

    def hangs_forever(*args, **kwargs):
        # Deliberately short (not e.g. 10s): asyncio.wait_for() only abandons
        # the *await*, it can't kill the underlying OS thread spawned by
        # asyncio.to_thread() - that thread keeps running this sleep() to
        # completion in the background regardless of the timeout below, and
        # the interpreter has to join it before the test process can exit.
        # 0.3s (6x the monkeypatched WATCH_POLL_TIMEOUT) is enough to
        # prove the timeout fires before this "fetch" would complete,
        # without leaving a long-lived straggler thread that slows the suite.
        time.sleep(0.3)  # pragma: no cover - should never finish before timeout

    async def run():
        start = asyncio.get_event_loop().time()
        with pytest.raises(asyncio.TimeoutError):
            await watchers._fetch_with_timeout(hangs_forever)
        return asyncio.get_event_loop().time() - start

    elapsed = asyncio.run(run())
    assert elapsed < 1.0


def test_concurrent_watch_meta_writes_do_not_raise_database_locked(tmp_path, monkeypatch):
    """With watch groups polled concurrently, multiple watches' poll
    calls can call set_meta()/set_last_state() (both write to watchers.db
    via _conn()) at effectively the same moment. _conn() sets
    PRAGMA busy_timeout so a writer that finds another writer's short-lived
    transaction still open retries instead of immediately raising
    'database is locked'. This spins up several real threads (matching how
    asyncio.to_thread offloads each poll's blocking work) hammering
    set_meta()/set_last_state() concurrently and asserts none of them
    raise."""
    import threading

    monkeypatch.setattr(watchers, "DB_PATH", tmp_path / "watchers.db")
    watchers.init_db()
    conversation_id = _slack_conversation_id("C1", "100.1")
    watch_id = watchers.add_watch("self_restart", conversation_id, announce=False)
    errors = []
    barrier = threading.Barrier(8)

    def hammer(n):
        try:
            barrier.wait(timeout=5)
            for i in range(15):
                watchers.set_meta(watch_id, {"n": n, "i": i})
                watchers.set_last_state(watch_id, json.dumps({"n": n, "i": i}))
        except Exception as exc:  # pragma: no cover - failure path under test
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == [], f"expected no 'database is locked' errors, got: {errors}"


def test_poll_once_dispatches_to_the_right_poll_fn_for_active_watch(tmp_path, monkeypatch):
    """'Re-check now' Watches-panel row action: poll_once()
    calls the exact poll function watcher_loop() would call
    for this watch's type — verified by
    monkeypatching _poll_time_watch and asserting it
    ran against this specific watch dict."""
    monkeypatch.setattr(watchers, "DB_PATH", tmp_path / "watchers.db")
    watchers.init_db()
    conversation_id = _slack_conversation_id("C1", "100.1")
    watch_id = watchers.add_watch("time", conversation_id, key="in:5m", label="p", announce=False)
    watch = watchers.get_watch(watch_id)

    called = []

    async def fake_poll(w):
        called.append(w["id"])

    monkeypatch.setattr(watchers, "_poll_time_watch", fake_poll)

    asyncio.run(watchers.poll_once(watch))

    assert called == [watch_id]


def test_poll_once_rejects_self_restart_watches(tmp_path, monkeypatch):
    """self_restart watches are only ever resolved once at process boot
    (see main()) — poll_once() refuses to run them on demand rather
    than silently no-op-ing or crashing on a missing dict entry."""
    monkeypatch.setattr(watchers, "DB_PATH", tmp_path / "watchers.db")
    watchers.init_db()
    conversation_id = _slack_conversation_id("C1", "100.1")
    watch_id = watchers.add_watch("self_restart", conversation_id, announce=False)
    watch = watchers.get_watch(watch_id)

    with pytest.raises(ValueError, match="self_restart"):
        asyncio.run(watchers.poll_once(watch))


def test_poll_once_rejects_non_active_status(tmp_path, monkeypatch):
    """A paused/done/broken watch isn't forced to poll via
    'recheck now' — resume (or re-add) it first, mirroring how the
    scheduled watcher_loop() only ever polls status == 'active' watches."""
    monkeypatch.setattr(watchers, "DB_PATH", tmp_path / "watchers.db")
    monkeypatch.setattr(watchers, "check_watch_resource_exists", lambda *a, **kw: None)
    watchers.init_db()
    conversation_id = _slack_conversation_id("C1", "100.1")
    watch_id = watchers.add_watch("time", conversation_id, key="in:5m", label="m", announce=False)
    watchers.set_status(watch_id, "paused")
    watch = watchers.get_watch(watch_id)

    with pytest.raises(ValueError, match="paused"):
        asyncio.run(watchers.poll_once(watch))


# --- session_turn watch type ----------------------------------------------
#
# Subscribes to another (or its own) session's turn completion. The
# critical property under test: a watch self-subscribed *mid-turn* must
# fire on THAT turn's own completion (not an extra, later one), while never
# firing before that turn has truly finished (see add_watch()'s
# baseline_generation capture and _poll_session_turn_watch()'s docstring).

@pytest.fixture(autouse=True)
def _reset_convos(monkeypatch):
    """Isolate the module-level CopilotConversation registry per test."""
    monkeypatch.setattr(copilot_session, "_convos", {})
    yield
    monkeypatch.setattr(copilot_session, "_convos", {})


def test_add_watch_session_turn_resolves_self_to_conversation_id(tmp_path, monkeypatch):
    monkeypatch.setattr(watchers, "DB_PATH", tmp_path / "watchers.db")
    watchers.init_db()
    conversation_id = message_store.get_or_create_conversation(None)

    watch_id = watchers.add_watch("session_turn", conversation_id, key="self", announce=False)
    watch = watchers.get_watch(watch_id)

    assert watch["key"] == conversation_id  # never stored as the literal "self"


def test_add_watch_session_turn_rejects_nonexistent_target(tmp_path, monkeypatch):
    monkeypatch.setattr(watchers, "DB_PATH", tmp_path / "watchers.db")
    watchers.init_db()
    conversation_id = message_store.get_or_create_conversation(None)

    with pytest.raises(ValueError, match="does not exist"):
        watchers.add_watch("session_turn", conversation_id, key="conv-never-seen", announce=False)


def test_add_watch_session_turn_self_watch_mid_turn_uses_last_completed_as_baseline(tmp_path, monkeypatch):
    """"Subscribe to my own end of turn" means fire once the CURRENT
    (registering) turn finishes, not some extra later one. Registering
    mid-turn snapshots the target's last *completed* generation (one
    behind the in-flight turn) as the baseline — not the in-flight turn's
    own generation — so that turn's own completion satisfies the
    strict `>` check in _poll_session_turn_watch()."""
    monkeypatch.setattr(watchers, "DB_PATH", tmp_path / "watchers.db")
    watchers.init_db()
    conversation_id = message_store.get_or_create_conversation(None)

    convo = copilot_session.get_convo(conversation_id)
    convo._turn_generation = 1
    convo._last_completed_turn_generation = 0
    convo.turn_started_at = 123.0  # turn in flight right now (the registering turn)

    watch_id = watchers.add_watch("session_turn", conversation_id, key="self", announce=False)
    meta = json.loads(watchers.get_watch(watch_id)["meta"])

    assert meta["baseline_generation"] == 0  # last completed, NOT the in-flight generation (1)
    assert meta["max_chain"] == 10  # self-targeting watches are chain-capped
    assert meta["chain_count"] == 0

    # Now simulate that in-flight (registering) turn actually completing —
    # this must be exactly what fires the watch, no extra turn needed.
    convo._last_completed_turn_generation = 1
    convo.turn_started_at = None
    assert convo.last_completed_turn_generation() > meta["baseline_generation"]


def test_add_watch_session_turn_baseline_when_idle(tmp_path, monkeypatch):
    monkeypatch.setattr(watchers, "DB_PATH", tmp_path / "watchers.db")
    watchers.init_db()
    conversation_id = message_store.get_or_create_conversation(None)

    convo = copilot_session.get_convo(conversation_id)
    convo._turn_generation = 4
    convo._last_completed_turn_generation = 4
    convo.turn_started_at = None  # idle

    watch_id = watchers.add_watch("session_turn", conversation_id, key="self", announce=False)
    meta = json.loads(watchers.get_watch(watch_id)["meta"])

    assert meta["baseline_generation"] == 4


def test_add_watch_session_turn_baseline_zero_when_target_never_ran(tmp_path, monkeypatch):
    """No in-memory CopilotConversation yet for the target in this process
    generation — nothing has run, so its very first completed turn fires."""
    monkeypatch.setattr(watchers, "DB_PATH", tmp_path / "watchers.db")
    watchers.init_db()
    conversation_id = message_store.get_or_create_conversation(None)

    watch_id = watchers.add_watch("session_turn", conversation_id, key="self", announce=False)
    meta = json.loads(watchers.get_watch(watch_id)["meta"])

    assert meta["baseline_generation"] == 0


def test_add_watch_session_turn_on_other_session_already_mid_turn_fires_on_that_turn(tmp_path, monkeypatch):
    """Watching a DIFFERENT (not self) session whose turn is already in flight
    at registration time (e.g. session_create() kicks off a turn immediately)
    does not treat that in-flight turn as "the turn that called add_watch" -
    that only holds for self-subscription. The watcher is an outside observer
    and wants to be notified when this very turn (the target's only turn so
    far) finishes, so baseline_generation is the target's last completed one."""
    monkeypatch.setattr(watchers, "DB_PATH", tmp_path / "watchers.db")
    watchers.init_db()
    watcher_conversation_id = message_store.get_or_create_conversation(None)
    target_conversation_id = message_store.get_or_create_conversation(None)

    target_convo = copilot_session.get_convo(target_conversation_id)
    target_convo._turn_generation = 1
    target_convo._last_completed_turn_generation = 0
    target_convo.turn_started_at = 123.0  # target's own first turn in flight

    watch_id = watchers.add_watch(
        "session_turn", watcher_conversation_id, key=target_conversation_id, announce=False,
    )
    meta = json.loads(watchers.get_watch(watch_id)["meta"])

    # Must be 0 (the target's last *completed* generation), not 1 (the
    # target's in-flight generation) - so when that in-flight turn finishes
    # (last_completed_turn_generation becomes 1), 1 > 0 correctly fires.
    assert meta["baseline_generation"] == 0


def _session_turn_watch_dict(**overrides):
    base = {
        "id": 1,
        "type": "session_turn",
        "key": "conv-target",
        "label": "resume me",
        "conversation_id": "conv-watcher",
        "status": "active",
        "meta": json.dumps({
            "baseline_generation": 1,
            "boot_id_at_creation": "boot-1",
            "recurring": False,
            "max_chain": 10,
            "chain_count": 0,
        }),
    }
    base.update(overrides)
    return base


def test_poll_session_turn_watch_skips_own_inflight_turns_completion(monkeypatch):
    """Must NOT fire when the in-flight turn that registered it completes —
    last_completed_turn_generation becomes equal to (not greater than) the
    captured baseline."""
    monkeypatch.setattr(watchers, "current_boot_id", lambda: "boot-1")
    convo = copilot_session.get_convo("conv-target")
    convo._turn_generation = 1
    convo._last_completed_turn_generation = 1  # the registering turn just finished
    convo.turn_started_at = None

    injected = []
    monkeypatch.setattr(watchers, "_inject_watch_message", lambda *a, **kw: injected.append(a))
    statuses = []
    monkeypatch.setattr(watchers, "set_status", lambda wid, status: statuses.append(status))

    w = _session_turn_watch_dict(meta=json.dumps({
        "baseline_generation": 1, "boot_id_at_creation": "boot-1",
        "recurring": False, "max_chain": 10, "chain_count": 0,
    }))
    asyncio.run(watchers._poll_session_turn_watch(w))

    assert injected == []
    assert statuses == []


def test_poll_session_turn_watch_fires_on_genuinely_next_turn(monkeypatch):
    monkeypatch.setattr(watchers, "current_boot_id", lambda: "boot-1")
    convo = copilot_session.get_convo("conv-target")
    convo._turn_generation = 2
    convo._last_completed_turn_generation = 2  # a LATER turn than baseline=1 completed
    convo.turn_started_at = None

    injected = []

    async def fake_inject(conversation_id, text):
        injected.append((conversation_id, text))

    monkeypatch.setattr(watchers, "_inject_watch_message", fake_inject)
    statuses = []
    monkeypatch.setattr(watchers, "set_status", lambda wid, status: statuses.append((wid, status)))

    w = _session_turn_watch_dict()
    asyncio.run(watchers._poll_session_turn_watch(w))

    assert len(injected) == 1
    assert injected[0][0] == "conv-watcher"
    assert "resume me" in injected[0][1]
    assert statuses == [(1, "done")]  # one-shot: marked done after firing


def test_poll_session_turn_watch_survives_restart_via_persisted_generation(tmp_path, monkeypatch):
    """last_completed_turn_generation is persisted to bot_store.db on
    every turn completion (message_store.set_turn_generation, called from
    copilot_session.py's send() finally block) and restored when a
    CopilotConversation is (re)constructed. A bot restart therefore does
    NOT invalidate an existing session_turn watch — it just re-stamps
    meta.boot_id_at_creation and keeps comparing against the restored
    generation counter."""
    monkeypatch.setattr(watchers, "DB_PATH", tmp_path / "watchers.db")
    watchers.init_db()
    monkeypatch.setattr(copilot_session, "_convos", {})
    monkeypatch.setattr(watchers, "current_boot_id", lambda: "boot-1")

    watcher_conv = message_store.get_or_create_conversation(None)
    target_conv = message_store.get_or_create_conversation(None)

    # target_conv completed turn #1 (persisted to bot_store.db), and a
    # watch registered against it captured baseline=1.
    convo = copilot_session.get_convo(target_conv)
    convo._turn_generation = 1
    convo._last_completed_turn_generation = 1
    message_store.set_turn_generation(target_conv, 1)

    watch_id = watchers.add_watch(
        "session_turn", watcher_conv, key=target_conv, label="resume me", announce=False,
    )
    watchers.set_meta(watch_id, {
        "baseline_generation": 1, "boot_id_at_creation": "boot-1",
        "recurring": False, "max_chain": 10, "chain_count": 0,
    })

    # Simulate a restart: fresh in-memory conversation registry (generation
    # resets to 0 unless restored) + a new boot id.
    monkeypatch.setattr(copilot_session, "_convos", {})
    monkeypatch.setattr(watchers, "current_boot_id", lambda: "boot-2")

    notices = []

    async def fake_notice(conversation_id, text):
        notices.append((conversation_id, text))

    monkeypatch.setattr(watchers, "_deliver_system_notice", fake_notice)
    injected = []

    async def fake_inject(*a, **kw):
        injected.append(a)

    monkeypatch.setattr(watchers, "_inject_watch_message", fake_inject)

    w = watchers.get_watch(watch_id)
    asyncio.run(watchers._poll_session_turn_watch(w))

    # Not invalidated: no "invalidated"/"restarted" notice, watch stays active.
    assert notices == []
    assert injected == []
    assert watchers.get_watch(watch_id)["status"] == "active"
    assert json.loads(watchers.get_watch(watch_id)["meta"])["boot_id_at_creation"] == "boot-2"

    # Confirm the restored (not reset-to-0) counter was actually used, not
    # silently coincidental:
    restored_convo = copilot_session.get_convo(target_conv)
    assert restored_convo.last_completed_turn_generation() == 1

    # Now the target genuinely completes a NEW turn post-restart — the
    # restored counter must correctly recognize it as strictly newer than
    # the pre-restart baseline and fire.
    restored_convo._turn_generation = 2
    restored_convo._last_completed_turn_generation = 2
    w = watchers.get_watch(watch_id)
    asyncio.run(watchers._poll_session_turn_watch(w))

    assert len(injected) == 1
    assert watchers.get_watch(watch_id)["status"] == "done"


def test_poll_session_turn_watch_recurring_rearms_with_new_baseline(monkeypatch):
    monkeypatch.setattr(watchers, "current_boot_id", lambda: "boot-1")
    convo = copilot_session.get_convo("conv-target")
    convo._turn_generation = 5
    convo._last_completed_turn_generation = 5
    convo.turn_started_at = None

    async def fake_inject(conversation_id, text):
        pass

    monkeypatch.setattr(watchers, "_inject_watch_message", fake_inject)
    set_meta_calls = []
    monkeypatch.setattr(watchers, "set_meta", lambda wid, meta: set_meta_calls.append((wid, meta)))
    statuses = []
    monkeypatch.setattr(watchers, "set_status", lambda wid, status: statuses.append(status))

    w = _session_turn_watch_dict(meta=json.dumps({
        "baseline_generation": 1, "boot_id_at_creation": "boot-1",
        "recurring": True, "max_chain": 10, "chain_count": 3,
    }))
    asyncio.run(watchers._poll_session_turn_watch(w))

    assert statuses == []  # stays active, never marked done
    assert len(set_meta_calls) == 1
    new_meta = set_meta_calls[0][1]
    assert new_meta["baseline_generation"] == 5  # advanced to the generation that just fired
    assert new_meta["chain_count"] == 4


def test_poll_session_turn_watch_stops_at_max_chain(monkeypatch):
    monkeypatch.setattr(watchers, "current_boot_id", lambda: "boot-1")
    convo = copilot_session.get_convo("conv-target")
    convo._turn_generation = 9
    convo._last_completed_turn_generation = 9
    convo.turn_started_at = None

    injected = []
    monkeypatch.setattr(watchers, "_inject_watch_message", lambda *a, **kw: injected.append(a))
    notices = []

    async def fake_notice(conversation_id, text):
        notices.append((conversation_id, text))

    monkeypatch.setattr(watchers, "_deliver_system_notice", fake_notice)
    statuses = []
    monkeypatch.setattr(watchers, "set_status", lambda wid, status: statuses.append((wid, status)))

    w = _session_turn_watch_dict(meta=json.dumps({
        "baseline_generation": 1, "boot_id_at_creation": "boot-1",
        "recurring": True, "max_chain": 2, "chain_count": 2,
    }))
    asyncio.run(watchers._poll_session_turn_watch(w))

    assert injected == []  # stopped before firing again
    assert statuses == [(1, "done")]
    assert len(notices) == 1
    assert "max_chain" in notices[0][1]


def test_poll_session_turn_watch_noop_when_target_never_ran_in_process(monkeypatch):
    """No in-memory CopilotConversation for the target yet (e.g. bot just
    restarted and target hasn't been messaged again) — not an error, just
    nothing to compare against yet."""
    monkeypatch.setattr(watchers, "current_boot_id", lambda: "boot-1")
    injected = []
    monkeypatch.setattr(watchers, "_inject_watch_message", lambda *a, **kw: injected.append(a))
    statuses = []
    monkeypatch.setattr(watchers, "set_status", lambda wid, status: statuses.append(status))

    w = _session_turn_watch_dict(key="conv-never-ran")
    asyncio.run(watchers._poll_session_turn_watch(w))

    assert injected == []
    assert statuses == []


# --- session_turn staleness notice (P1) ------------------------------------
#
# A session_turn watch whose target just hasn't had a later turn yet is not an
# error; a single one-time notice is posted once SESSION_TURN_STALE_AFTER has
# elapsed since registration, without closing the watch (a legitimately long
# wait is not a bug).

def test_poll_session_turn_watch_stale_still_waiting_posts_one_time_notice(monkeypatch):
    monkeypatch.setattr(watchers, "current_boot_id", lambda: "boot-1")
    old_created = (watchers.datetime.now(watchers.timezone.utc) - watchers.timedelta(days=8)).isoformat()

    notices = []

    async def fake_notice(conversation_id, text):
        notices.append((conversation_id, text))

    monkeypatch.setattr(watchers, "_deliver_system_notice", fake_notice)
    set_meta_calls = []
    monkeypatch.setattr(watchers, "set_meta", lambda wid, meta: set_meta_calls.append((wid, meta)))
    statuses = []
    monkeypatch.setattr(watchers, "set_status", lambda wid, status: statuses.append(status))

    # target never ran in this process generation -> still-waiting branch
    w = _session_turn_watch_dict(key="conv-never-ran", created_at=old_created)
    asyncio.run(watchers._poll_session_turn_watch(w))

    assert len(notices) == 1
    assert "still waiting" in notices[0][1]
    assert statuses == []  # NOT closed — still active, just notified once
    assert set_meta_calls[0][1]["stale_notice_sent"] is True


def test_poll_session_turn_watch_stale_notice_not_repeated(monkeypatch):
    """Once stale_notice_sent is set in meta, must not notify again on
    every subsequent poll tick."""
    monkeypatch.setattr(watchers, "current_boot_id", lambda: "boot-1")
    old_created = (watchers.datetime.now(watchers.timezone.utc) - watchers.timedelta(days=30)).isoformat()

    notices = []

    async def fake_notice(conversation_id, text):
        notices.append((conversation_id, text))

    monkeypatch.setattr(watchers, "_deliver_system_notice", fake_notice)
    monkeypatch.setattr(watchers, "set_meta", lambda wid, meta: None)

    w = _session_turn_watch_dict(key="conv-never-ran", created_at=old_created, meta=json.dumps({
        "baseline_generation": 1, "boot_id_at_creation": "boot-1",
        "recurring": False, "max_chain": 10, "chain_count": 0,
        "stale_notice_sent": True,
    }))
    asyncio.run(watchers._poll_session_turn_watch(w))

    assert notices == []


def test_poll_session_turn_watch_not_yet_stale_no_notice(monkeypatch):
    monkeypatch.setattr(watchers, "current_boot_id", lambda: "boot-1")
    recent_created = watchers.datetime.now(watchers.timezone.utc).isoformat()

    notices = []
    monkeypatch.setattr(watchers, "_deliver_system_notice", lambda *a, **kw: notices.append(a))

    w = _session_turn_watch_dict(key="conv-never-ran", created_at=recent_created)
    asyncio.run(watchers._poll_session_turn_watch(w))

    assert notices == []


# --- session_turn max_chain override via meta (P3) -------------------------

def test_add_watch_session_turn_max_chain_overridable_via_meta(tmp_path, monkeypatch):
    """max_chain's default of 10 must be overridable by the caller at
    registration time (not just via a post-hoc set_meta() call) — passing
    meta={"max_chain": N} to add_watch()/watch_add() takes precedence over
    the built-in default."""
    monkeypatch.setattr(watchers, "DB_PATH", tmp_path / "watchers.db")
    watchers.init_db()
    conversation_id = message_store.get_or_create_conversation(None)

    watch_id = watchers.add_watch(
        "session_turn", conversation_id, key="self", meta={"max_chain": 3}, announce=False,
    )
    meta = json.loads(watchers.get_watch(watch_id)["meta"])

    assert meta["max_chain"] == 3


def test_add_watch_session_turn_max_chain_default_when_not_overridden(tmp_path, monkeypatch):
    monkeypatch.setattr(watchers, "DB_PATH", tmp_path / "watchers.db")
    watchers.init_db()
    conversation_id = message_store.get_or_create_conversation(None)

    watch_id = watchers.add_watch("session_turn", conversation_id, key="self", announce=False)
    meta = json.loads(watchers.get_watch(watch_id)["meta"])

    assert meta["max_chain"] == watchers.SESSION_TURN_DEFAULT_MAX_CHAIN


# --- session_turn concurrency confirmation (P5) ----------------------------
#
# Confirmation test (not a behavior change): session_turn's generation
# bookkeeping must stay correct per-conversation when multiple independent
# conversations advance "turns" concurrently via asyncio.gather — no
# cross-talk where one conversation's turn start/finish interleaving
# corrupts another's baseline/fire math. Low risk by construction (asyncio's
# single-threaded event loop serializes all actual attribute mutation), but
# this is still worth a dedicated confirmation rather than assuming it from
# the single-conversation tests above. Deliberately mimics send()'s own
# critical section (lock -> turn_started_at + generation increment -> await
# -> finally: last_completed_turn_generation) rather than faking the entire
# Copilot SDK session/event-stream surface, consistent with this test
# module's existing convention of not exercising the full send() path (see
# test_copilot_session.py's module docstring).

async def _simulate_turn(convo, delay: float) -> None:
    async with convo._lock:
        convo.turn_started_at = 123.0
        convo._turn_generation += 1
        try:
            await asyncio.sleep(delay)
        finally:
            convo.turn_started_at = None
            convo._last_completed_turn_generation = convo._turn_generation


def test_session_turn_generation_bookkeeping_is_independent_across_conversations(monkeypatch):
    """Two unrelated conversations each run several concurrently-interleaved
    simulated turns (asyncio.gather, staggered delays to force real
    interleaving) — each conversation's final generation count must match
    its own number of turns exactly, with no generation stolen/duplicated
    across conversations."""
    monkeypatch.setattr(copilot_session, "_convos", {})
    convo_a = copilot_session.get_convo("conv-a")
    convo_b = copilot_session.get_convo("conv-b")

    async def run_all():
        await asyncio.gather(
            _simulate_turn(convo_a, 0.01),
            _simulate_turn(convo_b, 0.005),
            _simulate_turn(convo_a, 0.002),
            _simulate_turn(convo_b, 0.015),
            _simulate_turn(convo_a, 0.008),
        )

    asyncio.run(run_all())

    assert convo_a.last_completed_turn_generation() == 3  # 3 turns simulated on conv-a
    assert convo_b.last_completed_turn_generation() == 2  # 2 turns simulated on conv-b
    assert convo_a.current_turn_generation() is None  # idle once all turns finish
    assert convo_b.current_turn_generation() is None


def test_session_turn_poll_fires_correctly_after_concurrent_interleaved_turns(monkeypatch):
    """End-to-end confirmation: a session_turn watch registered against one
    of two concurrently-active conversations fires based only on THAT
    conversation's generation advancing, unaffected by the other
    conversation's concurrently-interleaved turns."""
    monkeypatch.setattr(copilot_session, "_convos", {})
    monkeypatch.setattr(watchers, "current_boot_id", lambda: "boot-1")
    convo_a = copilot_session.get_convo("conv-a")
    convo_b = copilot_session.get_convo("conv-b")

    # Baseline captured as if a watch were registered on conv-a while idle.
    baseline_a = convo_a.last_completed_turn_generation()

    async def run_all():
        await asyncio.gather(
            _simulate_turn(convo_a, 0.01),
            _simulate_turn(convo_b, 0.003),
            _simulate_turn(convo_b, 0.012),
        )

    asyncio.run(run_all())

    injected = []

    async def fake_inject(conversation_id, text):
        injected.append(conversation_id)

    monkeypatch.setattr(watchers, "_inject_watch_message", fake_inject)
    monkeypatch.setattr(watchers, "set_status", lambda wid, status: None)

    w = _session_turn_watch_dict(key="conv-a", meta=json.dumps({
        "baseline_generation": baseline_a, "boot_id_at_creation": "boot-1",
        "recurring": False, "max_chain": 10, "chain_count": 0,
    }))
    asyncio.run(watchers._poll_session_turn_watch(w))

    assert injected == ["conv-watcher"]  # fired exactly once, correctly


def test_long_running_time_watch_turn_does_not_block_other_watch_polling():
    """A long-running fired `time` watch turn runs as a background task, so it
    doesn't block mr/pipeline/ticket polling (approvals and pipeline results
    aren't delayed). Time/session_turn polls run one at a time per watch."""
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def slow_poll(w):
        calls.append(w["id"])
        started.set()
        await release.wait()

    async def run():
        watchers._spawn_background_poll({"id": 99991}, "time", slow_poll)
        await started.wait()
        # returns immediately even though the poll is still running
        watchers._spawn_background_poll({"id": 99991}, "time", slow_poll)  # no double-run
        assert calls == [99991]
        release.set()
        await asyncio.gather(*list(watchers._background_tasks))
        assert ("time", 99991) not in watchers._background_inflight

    asyncio.run(run())


def test_slow_time_watch_turn_does_not_block_mr_poll_in_same_loop_tick(monkeypatch):
    """Real watcher_loop tick: a time watch whose turn never finishes must not
    stop the mr poll in that same tick from running."""
    mr_polled = []
    release = asyncio.Event()

    async def slow_time(w):
        await release.wait()

    async def mr_poll(w):
        mr_polled.append(w["id"])

    async def noop(*a, **kw):
        return None

    watches = [_make_watch(71, "time"), _make_watch(72, "mr")]
    monkeypatch.setattr(watchers, "list_watches", lambda *a, **kw: watches)
    monkeypatch.setattr(watchers, "_announce_new_watches", noop)
    monkeypatch.setattr(watchers, "_write_watchers_md", noop)
    monkeypatch.setattr(watchers, "_poll_time_watch", slow_time)
    monkeypatch.setattr(watchers, "_poll_integration_watch", mr_poll)
    monkeypatch.setattr(watchers, "_check_outage", noop)
    monkeypatch.setattr(watchers, "_background_inflight", set())
    calls = {"n": 0}

    async def fake_sleep(_):
        calls["n"] += 1
        if calls["n"] > 1:
            raise _StopWatcherLoop()

    monkeypatch.setattr(watchers.asyncio, "sleep", fake_sleep)

    async def run():
        with pytest.raises(_StopWatcherLoop):
            await watchers.watcher_loop()
        assert mr_polled == [72]
        release.set()
        await asyncio.gather(*list(watchers._background_tasks))

    asyncio.run(run())


def test_background_polls_for_same_conversation_run_one_after_another(monkeypatch):
    """Two time/session_turn watches on one conversation must not start
    concurrent turns (the second would interrupt the first); different
    conversations still run in parallel."""
    monkeypatch.setattr(watchers, "_background_inflight", set())
    monkeypatch.setattr(watchers, "_conversation_locks", {})
    active = {}
    peak = {}

    async def poll(w):
        c = w["conversation_id"]
        active[c] = active.get(c, 0) + 1
        peak[c] = max(peak.get(c, 0), active[c])
        await asyncio.sleep(0.02)
        active[c] -= 1

    async def run():
        for i, c in enumerate(["a", "a", "b"], start=1):
            watchers._spawn_background_poll({"id": 800 + i, "conversation_id": c}, "time", poll)
        await asyncio.gather(*list(watchers._background_tasks))

    asyncio.run(run())
    assert peak == {"a": 1, "b": 1}


def test_outage_notice_is_sent_once_after_threshold_and_recovery_once(monkeypatch):
    """A single failure never notifies; after OUTAGE_NOTICE_SECS of
    continuous failure exactly one notice goes out (not one per tick), and
    one recovery notice follows the first success. Wording (GitLab / MR/pipeline/job)
    comes from the tracked driver, so it is unchanged for GitLab watches."""
    sent = []

    async def fake_notice(conv, text):
        sent.append(text)

    monkeypatch.setattr(watchers, "_deliver_system_notice", fake_notice)
    monkeypatch.setattr(watchers, "_primary_notice_conversation", lambda: "conv-x")
    monkeypatch.setattr(watchers, "_blind_since", 1000.0)
    monkeypatch.setattr(watchers, "_outage_notified", False)
    monkeypatch.setattr(watchers, "_outage_title", "GitLab")
    monkeypatch.setattr(watchers, "_outage_label", "MR/pipeline/job")

    async def run():
        await watchers._check_outage(3, now=1000.0 + 60)
        assert sent == []
        await watchers._check_outage(3, now=1000.0 + 601)
        await watchers._check_outage(3, now=1000.0 + 700)
        assert len(sent) == 1 and "3 GitLab watches blind" in sent[0]
        watchers._blind_since = None
        await watchers._check_outage(3, now=1000.0 + 800)
        await watchers._check_outage(3, now=1000.0 + 860)
        assert len(sent) == 2 and "GitLab is reachable again - MR/pipeline/job watches" in sent[1]

    asyncio.run(run())


def test_reachability_success_clears_and_failure_opens_blind_window(monkeypatch):
    monkeypatch.setattr(watchers, "_blind_since", None)
    manifest = SimpleNamespace(title="Svc")
    tracking = SimpleNamespace(tracks_outage=True, outage_label="things", integration=SimpleNamespace(manifest=manifest))
    watchers._note_reachability(tracking, False)
    assert watchers._blind_since is not None
    watchers._note_reachability(tracking, True)
    assert watchers._blind_since is None
    watchers._note_reachability(SimpleNamespace(tracks_outage=False), False)
    assert watchers._blind_since is None


def _patch_merged_mr_client(monkeypatch, pipelines, state="merged"):
    _patch_mr_pipeline_gitlab_client(monkeypatch, pipelines=pipelines)
    client = FakeGitLabClientForMrPipeline(pipelines)
    client.get_merge_request = lambda project, iid: {"state": state}
    _fake_gitlab(client)


def _reported_state(pipeline):
    return json.dumps({"last_seen_at": "2026-09-22T00:00:00+00:00",
                       "pipeline": {"pipeline_id": pipeline["id"], "last_status": pipeline["status"],
                                    "finished_at": pipeline["finished_at"]}})


def test_mr_watch_stays_active_when_merged_but_last_pipeline_failed(_reset_configure, monkeypatch):
    fixture = _reset_configure
    pipeline = {"id": 9, "status": "failed", "web_url": "", "finished_at": "2026-09-23T09:00:00.000Z"}
    _patch_merged_mr_client(monkeypatch, [pipeline])
    statuses = []
    monkeypatch.setattr(watchers, "set_last_state", lambda wid, state: None)
    monkeypatch.setattr(watchers, "set_status", lambda wid, st: statuses.append(st))
    w = _watch(id=62, type="mr", key="group/project#42", label="!42",
               last_state=_reported_state(pipeline), meta="{}", created_at=None)

    asyncio.run(watchers._poll_integration_watch(w))

    assert fixture.calls == [] and statuses == []


def test_mr_watch_stays_active_while_mr_is_open(_reset_configure, monkeypatch):
    fixture = _reset_configure
    pipeline = {"id": 10, "status": "success", "web_url": "", "finished_at": "2026-09-23T09:00:00.000Z"}
    _patch_merged_mr_client(monkeypatch, [pipeline], state="opened")
    statuses = []
    monkeypatch.setattr(watchers, "set_last_state", lambda wid, state: None)
    monkeypatch.setattr(watchers, "set_status", lambda wid, st: statuses.append(st))
    w = _watch(id=63, type="mr", key="group/project#42", label="!42",
               last_state=_reported_state(pipeline), meta="{}", created_at=None)

    asyncio.run(watchers._poll_integration_watch(w))

    assert statuses == []
