"""
Unit tests for mcp_tools.py — the pure business logic backing the bot's
in-process MCP server (watch_* wrappers around watchers.py, and
session_search/session_get/session_list joining the global Copilot CLI
session store with this bot's own bot_store.db).

Isolation strategy (mirrors tests/test_watchers.py and
tests/test_web_server.py): monkeypatch watchers.DB_PATH and
message_store.DB_PATH to per-test tmp_path sqlite files, plus
mcp_tools.COPILOT_SESSION_STORE to a throwaway sqlite file with a hand-built
sessions/turns/checkpoints schema mirroring the real
~/.copilot/session-store.db — so tests never touch the developer's real
global session history or bot_store.db.
"""

from __future__ import annotations

import sqlite3

import copilot_session
import message_store
import mcp_tools
import watchers


# --- fixtures ---------------------------------------------------------

def _use_tmp_stores(tmp_path, monkeypatch):
    """Isolate watchers.py's watchers.db, message_store's bot_store.db, and
    mcp_tools' view of the global Copilot CLI session-store.db."""
    monkeypatch.setattr(watchers, "DB_PATH", tmp_path / "watchers.db")
    watchers.init_db()
    # mcp_tools.watch_add doesn't expose watchers.add_watch's `announce`
    # kwarg (an MCP client always wants the real announcement behavior in
    # production) — stub out the live Slack post here so tests never hit
    # the network, mirroring how test_web_server.py passes announce=False
    # when calling watchers.add_watch directly.
    monkeypatch.setattr(watchers, "_post_watch_announcement", lambda *a, **kw: False)
    monkeypatch.setattr(watchers, "check_watch_resource_exists", lambda *a, **kw: None)

    monkeypatch.setattr(message_store, "DB_PATH", tmp_path / "bot_store.db")
    monkeypatch.setattr(message_store, "_TITLES_FILE", tmp_path / "session_titles.json")
    message_store.init_db()

    monkeypatch.setattr(mcp_tools, "COPILOT_SESSION_STORE", tmp_path / "session-store.db")


def _slack_conversation_id(channel: str, thread_ts: str) -> str:
    return copilot_session._conversation_id_for(channel, thread_ts)


def _make_session_store(path, sessions=(), turns=(), checkpoints=(), files=(), refs=(), skip_files_refs_tables=False):
    """Build a throwaway sqlite file matching the shape of the real
    ~/.copilot/session-store.db that mcp_tools.session_search/session_get
    query against (sessions/turns/checkpoints/session_files/session_refs).

    ``files``/``refs`` rows are tuples matching session_files/session_refs'
    real column order: (session_id, file_path, tool_name, turn_index,
    first_seen_at) and (session_id, ref_type, ref_value, turn_index,
    created_at) respectively.

    ``skip_files_refs_tables=True`` omits creating the session_files/
    session_refs tables entirely, simulating an older/foreign session
    store that predates them — used to verify session_get degrades to
    files=[]/refs=[] instead of raising.
    """
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT, repository TEXT, summary TEXT, created_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE turns (session_id TEXT, turn_index INTEGER, user_message TEXT, "
        "assistant_response TEXT, timestamp TEXT)"
    )
    conn.execute(
        "CREATE TABLE checkpoints (session_id TEXT, checkpoint_number INTEGER, title TEXT, "
        "overview TEXT, work_done TEXT, next_steps TEXT, created_at TEXT)"
    )
    if not skip_files_refs_tables:
        conn.execute(
            "CREATE TABLE session_files (session_id TEXT, file_path TEXT, tool_name TEXT, "
            "turn_index INTEGER, first_seen_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE session_refs (session_id TEXT, ref_type TEXT, ref_value TEXT, "
            "turn_index INTEGER, created_at TEXT)"
        )
        conn.executemany("INSERT INTO session_files VALUES (?, ?, ?, ?, ?)", files)
        conn.executemany("INSERT INTO session_refs VALUES (?, ?, ?, ?, ?)", refs)
    conn.executemany(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)", sessions
    )
    conn.executemany(
        "INSERT INTO turns VALUES (?, ?, ?, ?, ?)", turns
    )
    conn.executemany(
        "INSERT INTO checkpoints VALUES (?, ?, ?, ?, ?, ?, ?)", checkpoints
    )
    conn.commit()
    conn.close()


# --- watch_* ------------------------------------------------------------

def test_watch_add_returns_new_id(tmp_path, monkeypatch):
    _use_tmp_stores(tmp_path, monkeypatch)
    conversation_id = _slack_conversation_id("C123", "111.222")
    result = mcp_tools.watch_add("time", conversation_id, key="in:5m", label="t")
    assert isinstance(result, dict)
    assert isinstance(result["watch_id"], int)


def test_watch_list_returns_added_watch(tmp_path, monkeypatch):
    _use_tmp_stores(tmp_path, monkeypatch)
    conversation_id = _slack_conversation_id("C123", "111.222")
    added = mcp_tools.watch_add("time", conversation_id, key="in:5m", label="t")
    watches = mcp_tools.watch_list()
    assert any(w["id"] == added["watch_id"] for w in watches)


def test_watch_list_scoped_to_conversation(tmp_path, monkeypatch):
    _use_tmp_stores(tmp_path, monkeypatch)
    conversation_id_a = _slack_conversation_id("C1", "111.111")
    conversation_id_b = _slack_conversation_id("C1", "222.222")
    mcp_tools.watch_add("time", conversation_id_a, key="in:5m", label="t")
    mcp_tools.watch_add("time", conversation_id_b, key="in:6m", label="t")
    watches = mcp_tools.watch_list(session_id=conversation_id_a)
    assert len(watches) == 1
    assert watches[0]["conversation_id"] == conversation_id_a


def test_watch_get_found_and_missing(tmp_path, monkeypatch):
    _use_tmp_stores(tmp_path, monkeypatch)
    conversation_id = _slack_conversation_id("C1", "111.111")
    added = mcp_tools.watch_add("time", conversation_id, key="in:5m", label="t")
    watch = mcp_tools.watch_get(added["watch_id"])
    assert watch is not None
    assert watch["key"] == "in:5m"
    assert mcp_tools.watch_get(999999) is None


def test_watch_pause_and_resume(tmp_path, monkeypatch):
    _use_tmp_stores(tmp_path, monkeypatch)
    conversation_id = _slack_conversation_id("C1", "111.111")
    added = mcp_tools.watch_add("time", conversation_id, key="in:5m", label="t")
    watch_id = added["watch_id"]

    paused = mcp_tools.watch_pause(watch_id)
    assert paused == {"done": [watch_id], "skipped": []}
    assert mcp_tools.watch_get(watch_id)["status"] == "paused"

    resumed = mcp_tools.watch_resume([watch_id])
    assert resumed == {"done": [watch_id], "skipped": []}
    assert mcp_tools.watch_get(watch_id)["status"] == "active"

    assert mcp_tools.watch_pause(999999) == {"done": [], "skipped": [{"id": 999999, "reason": "not found"}]}


def test_watch_remove(tmp_path, monkeypatch):
    _use_tmp_stores(tmp_path, monkeypatch)
    conversation_id = _slack_conversation_id("C1", "111.111")
    added = mcp_tools.watch_add("time", conversation_id, key="in:5m", label="t")
    watch_id = added["watch_id"]

    result = mcp_tools.watch_remove(watch_id)
    assert result == {"done": [watch_id], "skipped": []}
    assert mcp_tools.watch_get(watch_id) is None

    assert mcp_tools.watch_remove(watch_id)["skipped"] == [{"id": watch_id, "reason": "not found"}]


def test_watch_pause_session_pauses_only_that_sessions_watches(tmp_path, monkeypatch):
    """Pauses every watch of one session and leaves other sessions' watches running."""
    _use_tmp_stores(tmp_path, monkeypatch)
    conversation_id_a = _slack_conversation_id("C1", "111.111")
    conversation_id_b = _slack_conversation_id("C1", "222.222")
    mcp_tools.watch_add("time", conversation_id_a, key="in:5m", label="t")
    mcp_tools.watch_add("time", conversation_id_a, key="in:6m", label="t")
    b = mcp_tools.watch_add("time", conversation_id_b, key="in:5m", label="t")["watch_id"]

    result = mcp_tools.watch_pause_session(conversation_id_a)
    assert result == {"paused_count": 2}
    assert all(
        w["status"] == "paused"
        for w in mcp_tools.watch_list(session_id=conversation_id_a, include_inactive=True)
    )
    assert mcp_tools.watch_get(b)["status"] == "active"


def test_watch_list_flags_broken_watch_with_broken_error(tmp_path, monkeypatch):
    """watch_list: a watch whose stored key no longer validates carries a
    `broken_error` (compact and full rows); valid watches carry none."""
    _use_tmp_stores(tmp_path, monkeypatch)
    conversation_id = _slack_conversation_id("C1", "111.111")
    watch_id = mcp_tools.watch_add("time", conversation_id, key="in:5m", label="t")["watch_id"]
    ok_id = mcp_tools.watch_add("time", conversation_id, key="in:6m", label="t")["watch_id"]

    # add_watch validates up front, so corrupt the key directly in the DB.
    with watchers._conn() as conn:
        conn.execute("UPDATE watches SET key = ? WHERE id = ?", ("not-a-valid-time-key", watch_id))

    for rows in (mcp_tools.watch_list(), mcp_tools.watch_list(compact=True)):
        by_id = {w["id"]: w for w in rows}
        assert by_id[watch_id]["broken_error"]
        assert "broken_error" not in by_id[ok_id]


# --- session_* ------------------------------------------------------------

def test_session_search_matches_cli_turns(tmp_path, monkeypatch):
    _use_tmp_stores(tmp_path, monkeypatch)
    _make_session_store(
        tmp_path / "session-store.db",
        sessions=[("sess-1", "/repo", "org/repo", "did some work", "2026-01-01T00:00:00Z")],
        turns=[
            ("sess-1", 0, "find the session where I set Remote Edge as default browser", None, "2026-01-01T00:00:00Z"),
        ],
    )

    results = mcp_tools.session_search("Remote Edge")
    assert len(results) == 1
    assert results[0]["source"] == "cli_turn"
    assert results[0]["session_id"] == "sess-1"
    assert "Remote Edge" in results[0]["snippet"]
    assert results[0]["is_bot_session"] is False
    assert results[0]["slack_key"] is None


def test_session_search_matches_bot_messages(tmp_path, monkeypatch):
    _use_tmp_stores(tmp_path, monkeypatch)
    conv_id = message_store.get_or_create_conversation("C123:111.111", copilot_session_id="sess-2")
    message_store.record_message(conv_id, "user", "please gather the inventory env matrix")

    results = mcp_tools.session_search("inventory env matrix")
    assert len(results) == 1
    assert results[0]["source"] == "bot_message"
    assert results[0]["session_id"] == "sess-2"
    assert results[0]["is_bot_session"] is True
    assert results[0]["slack_key"] == "C123:111.111"


def test_session_search_annotates_cli_turn_as_bot_session_when_linked(tmp_path, monkeypatch):
    _use_tmp_stores(tmp_path, monkeypatch)
    message_store.get_or_create_conversation("C123:222.222", copilot_session_id="sess-3")
    _make_session_store(
        tmp_path / "session-store.db",
        sessions=[("sess-3", "/repo", "org/repo", None, "2026-01-02T00:00:00Z")],
        turns=[("sess-3", 0, "run the gather script for all envs", None, "2026-01-02T00:00:00Z")],
    )

    results = mcp_tools.session_search("gather script")
    cli_hits = [r for r in results if r["source"] == "cli_turn"]
    assert len(cli_hits) == 1
    assert cli_hits[0]["is_bot_session"] is True
    assert cli_hits[0]["slack_key"] == "C123:222.222"


def test_session_search_empty_query_returns_empty(tmp_path, monkeypatch):
    _use_tmp_stores(tmp_path, monkeypatch)
    assert mcp_tools.session_search("") == []
    assert mcp_tools.session_search("   ") == []


def test_session_search_respects_limit_and_sort_order(tmp_path, monkeypatch):
    _use_tmp_stores(tmp_path, monkeypatch)
    _make_session_store(
        tmp_path / "session-store.db",
        sessions=[("sess-a", "/r", "r", None, "2026-01-01T00:00:00Z"),
                  ("sess-b", "/r", "r", None, "2026-01-02T00:00:00Z")],
        turns=[
            ("sess-a", 0, "topicword older", None, "2026-01-01T00:00:00Z"),
            ("sess-b", 0, "topicword newer", None, "2026-01-05T00:00:00Z"),
        ],
    )
    results = mcp_tools.session_search("topicword", limit=1)
    assert len(results) == 1
    assert results[0]["session_id"] == "sess-b"


def test_session_search_no_store_file_only_searches_bot_messages(tmp_path, monkeypatch):
    _use_tmp_stores(tmp_path, monkeypatch)
    # COPILOT_SESSION_STORE points at a tmp_path file that was never created
    # by _make_session_store, so .exists() is False.
    conv_id = message_store.get_or_create_conversation(None)
    message_store.record_message(conv_id, "user", "unique-marker-xyz")

    results = mcp_tools.session_search("unique-marker-xyz")
    assert len(results) == 1
    assert results[0]["source"] == "bot_message"


def test_session_get_found(tmp_path, monkeypatch):
    _use_tmp_stores(tmp_path, monkeypatch)
    _make_session_store(
        tmp_path / "session-store.db",
        sessions=[("sess-1", "/repo", "org/repo", "summary text", "2026-01-01T00:00:00Z")],
        turns=[("sess-1", 0, "hello", "hi there", "2026-01-01T00:00:00Z")],
        checkpoints=[("sess-1", 1, "Checkpoint 1", "overview", "work done", "next steps", "2026-01-01T00:01:00Z")],
    )

    result = mcp_tools.session_get("sess-1")
    assert result is not None
    assert result["id"] == "sess-1"
    assert len(result["turns"]) == 1
    assert result["turns"][0]["user_message"] == "hello"
    assert len(result["checkpoints"]) == 1
    assert result["checkpoints"][0]["title"] == "Checkpoint 1"
    assert result["is_bot_session"] is False
    assert result["bot_messages"] == []


def test_session_get_bot_session_includes_bot_messages(tmp_path, monkeypatch):
    _use_tmp_stores(tmp_path, monkeypatch)
    message_store.get_or_create_conversation("C1:333.333", copilot_session_id="sess-4")
    conv_id = message_store.resolve_conversation_by_slack_key("C1:333.333")
    message_store.record_message(conv_id, "user", "hi")
    message_store.record_message(conv_id, "assistant", "hello back")
    _make_session_store(
        tmp_path / "session-store.db",
        sessions=[("sess-4", "/repo", "org/repo", None, "2026-01-01T00:00:00Z")],
    )

    result = mcp_tools.session_get("sess-4")
    assert result is not None
    assert result["is_bot_session"] is True
    assert result["slack_key"] == "C1:333.333"
    assert len(result["bot_messages"]) == 2


def test_session_get_includes_touched_files_and_refs(tmp_path, monkeypatch):
    """session_get returns the files a past session touched and the MR/ticket/URL
    refs it extracted, so no raw SQL against the global store is needed."""
    _use_tmp_stores(tmp_path, monkeypatch)
    _make_session_store(
        tmp_path / "session-store.db",
        sessions=[("sess-5", "/repo", "org/repo", "summary text", "2026-01-01T00:00:00Z")],
        turns=[("sess-5", 0, "hello", "hi there", "2026-01-01T00:00:00Z")],
        files=[
            ("sess-5", "dop-agent/mcp_tools.py", "edit", 0, "2026-01-01T00:00:30Z"),
            ("sess-5", "dop-agent/tests/test_mcp_tools.py", "create", 1, "2026-01-01T00:01:00Z"),
        ],
        refs=[
            ("sess-5", "jira_ticket", "PRJ-12345", 0, "2026-01-01T00:00:15Z"),
            ("sess-5", "merge_request", "!106", 1, "2026-01-01T00:01:15Z"),
        ],
    )

    result = mcp_tools.session_get("sess-5")
    assert result is not None
    assert [f["file_path"] for f in result["files"]] == [
        "dop-agent/mcp_tools.py",
        "dop-agent/tests/test_mcp_tools.py",
    ]
    assert result["files"][0]["tool_name"] == "edit"
    assert [r["ref_value"] for r in result["refs"]] == ["PRJ-12345", "!106"]
    assert result["refs"][0]["ref_type"] == "jira_ticket"


def test_session_get_omits_files_and_refs_tables_gracefully(tmp_path, monkeypatch):
    """An older or foreign ~/.copilot/session-store.db may lack session_files/
    session_refs; session_get must degrade to empty lists instead of raising
    sqlite3.OperationalError."""
    _use_tmp_stores(tmp_path, monkeypatch)
    _make_session_store(
        tmp_path / "session-store.db",
        sessions=[("sess-6", "/repo", "org/repo", None, "2026-01-01T00:00:00Z")],
        turns=[("sess-6", 0, "hello", "hi there", "2026-01-01T00:00:00Z")],
        skip_files_refs_tables=True,
    )

    result = mcp_tools.session_get("sess-6")
    assert result is not None
    assert result["files"] == []
    assert result["refs"] == []


def test_session_get_not_found(tmp_path, monkeypatch):
    _use_tmp_stores(tmp_path, monkeypatch)
    _make_session_store(tmp_path / "session-store.db", sessions=[("sess-1", "/r", "r", None, "2026-01-01T00:00:00Z")])
    assert mcp_tools.session_get("does-not-exist") is None


def test_session_get_no_store_file(tmp_path, monkeypatch):
    _use_tmp_stores(tmp_path, monkeypatch)
    assert mcp_tools.session_get("anything") is None


def test_session_list_filters_archived_by_default(tmp_path, monkeypatch):
    _use_tmp_stores(tmp_path, monkeypatch)
    active_id = message_store.get_or_create_conversation("C1:1.1")
    archived_id = message_store.get_or_create_conversation("C1:2.2")
    message_store.set_archived(archived_id, True)

    convos = mcp_tools.session_list()
    ids = {c["conversation_id"] for c in convos}
    assert active_id in ids
    assert archived_id not in ids


def test_session_list_include_archived(tmp_path, monkeypatch):
    _use_tmp_stores(tmp_path, monkeypatch)
    archived_id = message_store.get_or_create_conversation("C1:2.2")
    message_store.set_archived(archived_id, True)

    convos = mcp_tools.session_list(include_archived=True)
    ids = {c["conversation_id"] for c in convos}
    assert archived_id in ids


def test_session_list_respects_limit(tmp_path, monkeypatch):
    _use_tmp_stores(tmp_path, monkeypatch)
    for i in range(5):
        message_store.get_or_create_conversation(f"C1:{i}.{i}")

    convos = mcp_tools.session_list(limit=2)
    assert len(convos) == 2


# --- token-trimming: compact output, batch helpers, group_manage -----------

def test_watch_list_compact_fields_and_truncation_keep_full_default_intact(tmp_path, monkeypatch):
    """Token saving: watch_list(compact=True) returns slim rows (long label
    cut to 80 chars, next_fire_at lifted out of meta, no channel/thread_ts/
    meta/timestamps) while the default stays the full row."""
    _use_tmp_stores(tmp_path, monkeypatch)
    cid = _slack_conversation_id("C1", "1.0")
    mcp_tools.watch_add("time", cid, key="at:2030-01-01T09:00", label="x" * 300)
    full = mcp_tools.watch_list()[0]
    assert "channel" in full and len(full["label"]) == 300
    row = mcp_tools.watch_list(compact=True)[0]
    assert set(row) <= set(mcp_tools.WATCH_COMPACT_FIELDS)
    assert len(row["label"]) == 80 and "meta" not in row and "channel" not in row
    assert list(mcp_tools.watch_list(fields=["id", "key"])[0]) == ["id", "key"]
    assert mcp_tools.watch_list(limit=0) == []


def test_watch_batch_pause_resume_remove_skip_protected_cron_watches(tmp_path, monkeypatch):
    """Daily watcher review: pause/resume/remove accept a list of ids in one
    call but must never touch standing cron watches unless force=True, and
    must report unknown ids instead of failing."""
    _use_tmp_stores(tmp_path, monkeypatch)
    cid = _slack_conversation_id("C1", "1.0")
    a = mcp_tools.watch_add("time", cid, key="at:2030-01-01T09:00", label="t")["watch_id"]
    b = mcp_tools.watch_add("time", cid, key="at:2030-01-02T09:00", label="t")["watch_id"]
    cron = mcp_tools.watch_add("time", cid, key="cron:mon 09:30", label="t")["watch_id"]
    res = mcp_tools.watch_pause([a, cron, 999999])
    assert res["done"] == [a]
    assert {s["id"] for s in res["skipped"]} == {cron, 999999}
    assert mcp_tools.watch_resume([a])["done"] == [a]
    res = mcp_tools.watch_remove([a, b, cron])
    assert res["done"] == [a, b] and [s["id"] for s in res["skipped"]] == [cron]
    assert mcp_tools.watch_get(cron) is not None
    assert mcp_tools.watch_remove([cron], force=True)["done"] == [cron]


def test_session_get_compact_keeps_last_turns_and_points_to_raw_log(tmp_path, monkeypatch):
    """Token saving: compact session_get returns only the last N turns with
    text cut to max_chars, plus a `truncated` hint naming the on-disk raw
    event log so the agent can grep instead of pulling everything."""
    _use_tmp_stores(tmp_path, monkeypatch)
    _make_session_store(
        tmp_path / "session-store.db",
        sessions=[("sess-c", "/x", "r", "sum", "2026-01-01")],
        turns=[("sess-c", i, f"q{i}", "a" * 5000, f"2026-01-01T00:0{i}") for i in range(5)],
    )
    r = mcp_tools.session_get("sess-c", compact=True, last_turns=2, max_chars=100)
    assert [t["turn_index"] for t in r["turns"]] == [3, 4]
    assert len(r["turns"][0]["assistant_response"]) < 150
    assert r["truncated"]["totals"]["turns"] == 5
    assert "events.jsonl" in r["truncated"]["hint"] and "sess-c" in r["truncated"]["hint"]
    assert "turns" not in mcp_tools.session_get("sess-c", compact=True, sections=["refs"])
    assert len(mcp_tools.session_get("sess-c")["turns"]) == 5


def test_session_list_compact_drops_preview_and_thread_fields(tmp_path, monkeypatch):
    _use_tmp_stores(tmp_path, monkeypatch)
    message_store.get_or_create_conversation(None)
    row = mcp_tools.session_list(compact=True)[0]
    assert "preview" not in row and "slack_key" not in row and "conversation_id" in row


def test_group_manage_actions_replace_the_seven_group_mutation_tools(tmp_path, monkeypatch):
    """group_manage(action=...) covers every group mutation; bad input raises with a fix hint."""
    import pytest
    _use_tmp_stores(tmp_path, monkeypatch)
    conv = message_store.get_or_create_conversation(None)
    g = mcp_tools.group_manage("create", title="G")["id"]
    assert mcp_tools.group_manage("update", group_id=g, title="G2")["title"] == "G2"
    assert mcp_tools.group_manage("assign", session_id=conv, group_id=g, make_master=True)["is_master"]
    assert mcp_tools.group_manage("set_master", group_id=g, session_id=None)["master_id"] is None
    assert mcp_tools.group_manage("archive", group_id=g)["sessions"] == 1
    assert mcp_tools.group_manage("delete", group_id=g)["unassigned"] == 1
    with pytest.raises(ValueError, match="use one of"):
        mcp_tools.group_manage("bogus")
    with pytest.raises(ValueError, match="group_manage"):
        mcp_tools.group_manage("archive", group_id="nope")
