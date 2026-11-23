"""
Unit tests for message_store.py, run against a throwaway sqlite DB and
titles file (monkeypatched module globals) — never touches the real
bot_store.db / session_titles.json used by the live bot.
"""

from __future__ import annotations

import json

import config
import message_store


def _use_tmp_store(tmp_path, monkeypatch):
    """Point message_store at a fresh DB + titles file inside tmp_path."""
    monkeypatch.delenv("ALLOWED_USER_ID", raising=False)
    monkeypatch.delenv("WEB_UI_PASSWORD", raising=False)
    monkeypatch.setattr(message_store, "DB_PATH", tmp_path / "bot_store.db")
    monkeypatch.setattr(message_store, "_TITLES_FILE", tmp_path / "session_titles.json")
    message_store.init_db()


def test_get_or_create_conversation_is_idempotent_per_slack_key(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)

    conv1 = message_store.get_or_create_conversation("C123:1111.0001")
    conv2 = message_store.get_or_create_conversation("C123:1111.0001")

    assert conv1 == conv2


def test_get_or_create_conversation_persists_parent_conversation_id(tmp_path, monkeypatch):
    """Backing store for both the Web UI's "Continue in new session" button
    and the session_create MCP tool's parent_session_id: a brand-new
    standalone conversation created with parent_conversation_id=<old id>
    must record that link so a "Continued from"/"Created by" backlink can
    be rendered without a separate side table."""
    _use_tmp_store(tmp_path, monkeypatch)
    parent_id = message_store.get_or_create_conversation(None)

    child_id = message_store.get_or_create_conversation(None, parent_conversation_id=parent_id)

    convo = message_store.get_conversation(child_id)
    assert convo["parent_conversation_id"] == parent_id


def test_get_or_create_conversation_without_parent_leaves_column_null(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)

    conversation_id = message_store.get_or_create_conversation(None)

    convo = message_store.get_conversation(conversation_id)
    assert convo["parent_conversation_id"] is None


def test_get_or_create_conversation_distinct_slack_keys_get_distinct_ids(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)

    conv1 = message_store.get_or_create_conversation("C123:1111.0001")
    conv2 = message_store.get_or_create_conversation("C123:2222.0002")

    assert conv1 != conv2


def test_get_or_create_conversation_web_ui_only_has_no_slack_key(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)

    conv = message_store.get_or_create_conversation(None)
    row = message_store.get_conversation(conv)

    assert row is not None
    assert not row.get("slack_key")


def test_set_and_get_title_custom_override(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv = message_store.get_or_create_conversation(None)

    message_store.set_title(conv, "My custom title")

    assert message_store.get_title(conv) == "My custom title"
    # Custom titles persist to the on-disk titles file too.
    saved = json.loads((tmp_path / "session_titles.json").read_text())
    assert saved[conv] == "My custom title"


def test_set_pinned_and_pin_order(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv_a = message_store.get_or_create_conversation("C:aaa")
    conv_b = message_store.get_or_create_conversation("C:bbb")

    message_store.set_pinned(conv_a, True)
    message_store.set_pinned(conv_b, True)
    message_store.set_pin_order([conv_b, conv_a])

    convs = {c["conversation_id"]: c for c in message_store.list_conversations()}
    assert convs[conv_a]["pinned"] is True
    assert convs[conv_b]["pinned"] is True
    assert convs[conv_b]["pin_order"] < convs[conv_a]["pin_order"]


def test_new_conversations_default_to_not_archived(tmp_path, monkeypatch):
    """New sessions start visible (archived=False) so they show in the default sidebar view."""
    _use_tmp_store(tmp_path, monkeypatch)
    conv = message_store.get_or_create_conversation(None)

    row = message_store.get_conversation(conv)

    assert bool(row["archived"]) is False


def test_set_archived_hides_session_from_default_list_conversations(tmp_path, monkeypatch):
    """Archiving a session (feature: archive/hide session) removes it from
    the front of list_conversations()'s combined pinned+unpinned groups -
    the Web UI relies on this to hide archived sessions from the default
    sidebar view without a 'show archived' toggle enabled."""
    _use_tmp_store(tmp_path, monkeypatch)
    conv_active = message_store.get_or_create_conversation("C:active")
    conv_archived = message_store.get_or_create_conversation("C:archived")

    message_store.set_archived(conv_archived, True)

    convs = {c["conversation_id"]: c for c in message_store.list_conversations()}
    assert convs[conv_active]["archived"] is False
    assert convs[conv_archived]["archived"] is True
    # Archived conversations still come back from list_conversations() (so
    # the "show archived" toggle can display them) - they're just ordered
    # after every active (pinned or unpinned) conversation.
    ids_in_order = [c["conversation_id"] for c in message_store.list_conversations()]
    assert ids_in_order.index(conv_archived) > ids_in_order.index(conv_active)


def test_set_archived_can_be_reversed(tmp_path, monkeypatch):
    """Unarchiving (set_archived(..., False)) must restore a session to the
    normal active ordering, not leave it permanently marked archived."""
    _use_tmp_store(tmp_path, monkeypatch)
    conv = message_store.get_or_create_conversation(None)
    message_store.set_archived(conv, True)

    message_store.set_archived(conv, False)

    row = message_store.get_conversation(conv)
    assert bool(row["archived"]) is False


def test_record_message_and_get_messages_roundtrip(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv = message_store.get_or_create_conversation(None)

    message_store.record_message(conv, "user", "hello")
    message_store.record_message(conv, "assistant", "hi there")

    messages = message_store.get_messages(conv)
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert [m["content"] for m in messages] == ["hello", "hi there"]


def test_find_stuck_status_conversations_detects_unfinalized_turn(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    stuck_conv = message_store.get_or_create_conversation("C1:1")
    done_conv = message_store.get_or_create_conversation("C2:2")

    # A conversation whose last message is still 'status' (Working...) is stuck.
    message_store.record_message(
        stuck_conv, "status", "Working...",
        external_ref=message_store.slack_external_ref("C1", "1111.2222"),
    )
    # A conversation that got a real final answer is not stuck.
    message_store.record_message(done_conv, "status", "Working...")
    message_store.record_message(done_conv, "assistant", "Here's your answer")

    stuck = message_store.find_stuck_status_conversations()
    stuck_ids = {s["conversation_id"] for s in stuck}

    assert stuck_conv in stuck_ids
    assert done_conv not in stuck_ids

    entry = next(s for s in stuck if s["conversation_id"] == stuck_conv)
    assert entry["status_channel"] == "C1"
    assert entry["status_ts"] == "1111.2222"


def test_slack_external_ref_format():
    assert message_store.slack_external_ref("C1", "123.456") == "slack:C1:123.456"


def test_init_db_migrates_legacy_slack_key_into_conversation_bindings_idempotently(tmp_path, monkeypatch):
    """Rows keyed only by conversations.slack_key get an equivalent
    conversation_bindings row on init_db(); re-running init_db() does not duplicate it."""
    monkeypatch.delenv("ALLOWED_USER_ID", raising=False)
    monkeypatch.delenv("WEB_UI_PASSWORD", raising=False)
    db_path = tmp_path / "bot_store.db"
    monkeypatch.setattr(message_store, "DB_PATH", db_path)
    monkeypatch.setattr(message_store, "_TITLES_FILE", tmp_path / "session_titles.json")

    with message_store._conn() as conn:
        conn.execute(
            """
            CREATE TABLE conversations (
                id TEXT PRIMARY KEY,
                slack_key TEXT UNIQUE,
                copilot_session_id TEXT,
                pinned INTEGER NOT NULL DEFAULT 0,
                pin_order INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO conversations(id, slack_key, copilot_session_id, pinned, created_at, updated_at)
            VALUES ('conv-1', 'C123:1111.0001', 'sess-1', 0, '2026-09-21T00:00:00Z', '2026-09-21T00:00:00Z')
            """
        )

    message_store.init_db()
    message_store.init_db()

    bindings = message_store.list_conversation_bindings("conv-1", message_store.SLACK_PLATFORM)
    assert bindings == [{"platform": "slack", "external_key": "C123:1111.0001"}]
    assert message_store.resolve_conversation_by_slack_key("C123:1111.0001") == "conv-1"


def test_init_db_seeds_allowed_identities_from_env(tmp_path, monkeypatch):
    """init_db() seeds the allow-list from the ALLOWED_USER_ID env var plus the
    Web UI's password-protected identity, so the owner is never locked out."""
    monkeypatch.setenv("ALLOWED_USER_ID", "U_OWNER")
    monkeypatch.setenv("WEB_UI_PASSWORD", "secret123")
    monkeypatch.setattr(message_store, "DB_PATH", tmp_path / "bot_store.db")
    monkeypatch.setattr(message_store, "_TITLES_FILE", tmp_path / "session_titles.json")

    message_store.init_db()

    assert message_store.is_allowed_identity("slack", "U_OWNER") is True
    assert message_store.is_allowed_identity("web", "web-ui") is True


def test_web_password_from_config_file_ref_seeds_web_identity(tmp_path, monkeypatch):
    """A Web UI password configured via a config.yaml ${file:...}/${env:...} ref
    allow-lists the Web UI identity (resolved through config.secret_value like web_server)."""
    monkeypatch.delenv("WEB_UI_PASSWORD", raising=False)
    monkeypatch.setenv("MY_WEB_PW", "pw-from-ref")
    cfg = tmp_path / "config.yaml"
    cfg.write_text('adapters:\n  web:\n    password: "${env:MY_WEB_PW}"\n')
    cfg.chmod(0o600)
    monkeypatch.setenv("DOP_AGENT_CONFIG", str(cfg))
    config.reload()
    try:
        monkeypatch.setattr(message_store, "DB_PATH", tmp_path / "bot_store.db")
        monkeypatch.setattr(message_store, "_TITLES_FILE", tmp_path / "session_titles.json")
        message_store.init_db()
        assert message_store.is_allowed_identity("web", "web-ui") is True
    finally:
        monkeypatch.undo()
        config.reload()


def test_get_or_create_bound_conversation_prefers_binding_table_over_blank_legacy_slack_key(tmp_path, monkeypatch):
    """Resolving a Slack thread uses conversation_bindings even when the
    conversations.slack_key column is blank."""
    _use_tmp_store(tmp_path, monkeypatch)
    conv = message_store.get_or_create_conversation(None)
    message_store.bind_conversation(conv, message_store.SLACK_PLATFORM, "C123:1111.0001")

    resolved = message_store.get_or_create_bound_conversation(
        message_store.SLACK_PLATFORM,
        "C123:1111.0001",
    )

    assert resolved == conv
    assert message_store.get_primary_binding(conv, message_store.SLACK_PLATFORM) == "C123:1111.0001"


def test_get_or_create_bound_conversation_notifies_listeners_on_existing_binding(tmp_path, monkeypatch):
    """get_or_create_bound_conversation() calls _notify_change() on the
    already-bound path too, so SSE/Web-UI listeners learn about updates
    (e.g. a copilot_session_id change) to an existing conversation."""
    _use_tmp_store(tmp_path, monkeypatch)
    conv = message_store.get_or_create_conversation(None)
    message_store.bind_conversation(conv, message_store.SLACK_PLATFORM, "C123:1111.0001")

    notified: list[str] = []
    message_store.register_change_listener(notified.append)

    resolved = message_store.get_or_create_bound_conversation(
        message_store.SLACK_PLATFORM,
        "C123:1111.0001",
        copilot_session_id="sess-123",
    )

    assert resolved == conv
    assert conv in notified


def test_search_messages_finds_matches_across_multiple_conversations(tmp_path, monkeypatch):
    """search_messages() finds a substring match in any conversation, is
    case-insensitive, and omits conversations without a match."""
    _use_tmp_store(tmp_path, monkeypatch)

    conv1 = message_store.get_or_create_conversation("C1:1.0001")
    conv2 = message_store.get_or_create_conversation("C1:2.0002")
    message_store.record_message(conv1, "user", "please bump example-app to 0.19.3")
    message_store.record_message(conv1, "assistant", "Done, bumped across all config bundles")
    message_store.record_message(conv2, "user", "what's the status of the FC14 STG environment")

    results = message_store.search_messages("example-app")

    conversation_ids = {r["conversation_id"] for r in results}
    assert conv1 in conversation_ids
    assert conv2 not in conversation_ids


def test_search_messages_is_case_insensitive(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv = message_store.get_or_create_conversation("C1:1.0001")
    message_store.record_message(conv, "user", "Please Check The GITLAB Pipeline")

    results = message_store.search_messages("gitlab pipeline")

    assert any(r["conversation_id"] == conv for r in results)


def test_search_messages_empty_query_returns_no_results(tmp_path, monkeypatch):
    """An empty/whitespace-only query must not act as a wildcard that
    dumps every message in the store - it should just return nothing."""
    _use_tmp_store(tmp_path, monkeypatch)
    conv = message_store.get_or_create_conversation("C1:1.0001")
    message_store.record_message(conv, "user", "hello world")

    assert message_store.search_messages("") == []
    assert message_store.search_messages("   ") == []


def test_search_messages_snippet_is_centered_on_match(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv = message_store.get_or_create_conversation("C1:1.0001")
    long_text = ("padding " * 30) + "THE-MATCH-TERM" + (" padding" * 30)
    message_store.record_message(conv, "user", long_text)

    results = message_store.search_messages("THE-MATCH-TERM")

    assert len(results) == 1
    assert "THE-MATCH-TERM" in results[0]["snippet"]
    # The snippet should be meaningfully shorter than the full message -
    # i.e. it's actually an excerpt, not the whole content dumped back.
    assert len(results[0]["snippet"]) < len(long_text)


def test_search_messages_literal_percent_and_underscore_are_not_wildcards(tmp_path, monkeypatch):
    """Regression guard: a query containing literal SQL LIKE wildcard
    characters (% or _) must be matched literally, not treated as a
    pattern that matches unrelated content."""
    _use_tmp_store(tmp_path, monkeypatch)
    conv_a = message_store.get_or_create_conversation("C1:1.0001")
    conv_b = message_store.get_or_create_conversation("C1:2.0002")
    message_store.record_message(conv_a, "user", "coverage is at 85% today")
    message_store.record_message(conv_b, "user", "coverage is at XYZ today")

    results = message_store.search_messages("85%")

    conversation_ids = {r["conversation_id"] for r in results}
    assert conv_a in conversation_ids
    assert conv_b not in conversation_ids


# --------------------------------------------------------------------------
# P4 feature: bookmark/star a specific message within a thread
# --------------------------------------------------------------------------


def test_get_bookmarked_message_ids_defaults_to_empty_list(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv = message_store.get_or_create_conversation(None)

    assert message_store.get_bookmarked_message_ids(conv) == []


def test_set_message_bookmarked_adds_and_removes(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv = message_store.get_or_create_conversation(None)
    msg_id = message_store.record_message(conv, "user", "important thing")

    message_store.set_message_bookmarked(conv, msg_id, True)
    assert message_store.get_bookmarked_message_ids(conv) == [msg_id]

    message_store.set_message_bookmarked(conv, msg_id, False)
    assert message_store.get_bookmarked_message_ids(conv) == []


def test_set_message_bookmarked_is_idempotent_when_bookmarking_twice(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv = message_store.get_or_create_conversation(None)
    msg_id = message_store.record_message(conv, "user", "important thing")

    message_store.set_message_bookmarked(conv, msg_id, True)
    message_store.set_message_bookmarked(conv, msg_id, True)

    assert message_store.get_bookmarked_message_ids(conv) == [msg_id]


def test_bookmarks_are_ordered_oldest_first(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv = message_store.get_or_create_conversation(None)
    msg1 = message_store.record_message(conv, "user", "first")
    msg2 = message_store.record_message(conv, "user", "second")

    message_store.set_message_bookmarked(conv, msg2, True)
    message_store.set_message_bookmarked(conv, msg1, True)

    assert message_store.get_bookmarked_message_ids(conv) == [msg2, msg1]


def test_set_message_bookmarked_does_not_touch_conversation_updated_at(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv = message_store.get_or_create_conversation(None)
    msg_id = message_store.record_message(conv, "user", "important thing")
    before = message_store.get_conversation(conv)["updated_at"]

    message_store.set_message_bookmarked(conv, msg_id, True)

    after = message_store.get_conversation(conv)["updated_at"]
    assert after == before


def test_set_message_bookmarked_raises_keyerror_for_unknown_conversation(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    import pytest

    with pytest.raises(KeyError):
        message_store.set_message_bookmarked("does-not-exist", "some-msg-id", True)


def test_bookmarks_are_scoped_per_conversation(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv_a = message_store.get_or_create_conversation("C1:1.0001")
    conv_b = message_store.get_or_create_conversation("C1:2.0002")
    msg_a = message_store.record_message(conv_a, "user", "in conv a")
    msg_b = message_store.record_message(conv_b, "user", "in conv b")

    message_store.set_message_bookmarked(conv_a, msg_a, True)

    assert message_store.get_bookmarked_message_ids(conv_a) == [msg_a]
    assert message_store.get_bookmarked_message_ids(conv_b) == []


# --- Server-side read/unread tracking -----------------------------------
# Read state is stored server-side; see message_store.mark_read()/get_last_read().

def test_get_last_read_defaults_to_none_for_unread_conversation(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv = message_store.get_or_create_conversation(None)

    assert message_store.get_last_read(conv) is None


def test_mark_read_with_no_explicit_when_uses_current_updated_at(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv = message_store.get_or_create_conversation(None)
    message_store.record_message(conv, "user", "hello")
    updated_at = message_store.get_conversation(conv)["updated_at"]

    result = message_store.mark_read(conv)

    assert result == updated_at
    assert message_store.get_last_read(conv) == updated_at


def test_mark_read_accepts_explicit_timestamp(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv = message_store.get_or_create_conversation(None)

    result = message_store.mark_read(conv, "2026-01-01T00:00:00")

    assert result == "2026-01-01T00:00:00"
    assert message_store.get_last_read(conv) == "2026-01-01T00:00:00"


def test_mark_read_does_not_touch_conversation_updated_at(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv = message_store.get_or_create_conversation(None)
    message_store.record_message(conv, "user", "hello")
    before = message_store.get_conversation(conv)["updated_at"]

    message_store.mark_read(conv)

    after = message_store.get_conversation(conv)["updated_at"]
    assert after == before


def test_mark_read_raises_keyerror_for_unknown_conversation(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    import pytest

    with pytest.raises(KeyError):
        message_store.mark_read("does-not-exist")


def test_mark_read_is_scoped_per_conversation(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv_a = message_store.get_or_create_conversation("C1:1.0001")
    conv_b = message_store.get_or_create_conversation("C1:2.0002")

    message_store.mark_read(conv_a)

    assert message_store.get_last_read(conv_a) is not None
    assert message_store.get_last_read(conv_b) is None


def test_list_conversations_includes_last_read_at(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv = message_store.get_or_create_conversation(None)
    message_store.record_message(conv, "user", "hello")

    unread_result = next(r for r in message_store.list_conversations() if r["conversation_id"] == conv)
    assert unread_result["last_read_at"] is None

    message_store.mark_read(conv)

    read_result = next(r for r in message_store.list_conversations() if r["conversation_id"] == conv)
    assert read_result["last_read_at"] is not None


def test_record_attachment_and_get_attachments_for_message(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv = message_store.get_or_create_conversation(None)
    msg_id = message_store.record_message(conv, "user", "here's a screenshot")

    attachment_id = message_store.record_attachment(
        msg_id, conv, kind="image", local_path="/tmp/fake.png",
        slack_file_id="F123", filename="fake.png", mimetype="image/png", size=42,
    )

    attachments = message_store.get_attachments_for_message(msg_id)
    assert len(attachments) == 1
    assert attachments[0]["id"] == attachment_id
    assert attachments[0]["filename"] == "fake.png"
    assert attachments[0]["mimetype"] == "image/png"
    assert attachments[0]["size"] == 42


def test_get_attachments_for_conversation_groups_by_message_id(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv = message_store.get_or_create_conversation(None)
    msg_a = message_store.record_message(conv, "user", "first")
    msg_b = message_store.record_message(conv, "user", "second")

    message_store.record_attachment(msg_a, conv, kind="image", local_path="/tmp/a.png")
    message_store.record_attachment(msg_b, conv, kind="image", local_path="/tmp/b.png")
    message_store.record_attachment(msg_b, conv, kind="image", local_path="/tmp/c.png")

    by_message = message_store.get_attachments_for_conversation(conv)
    assert len(by_message[msg_a]) == 1
    assert len(by_message[msg_b]) == 2


def test_get_attachment_is_scoped_to_conversation(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv_a = message_store.get_or_create_conversation("C1:1.0001")
    conv_b = message_store.get_or_create_conversation("C1:2.0002")
    msg_id = message_store.record_message(conv_a, "user", "pic")
    attachment_id = message_store.record_attachment(msg_id, conv_a, kind="image", local_path="/tmp/a.png")

    assert message_store.get_attachment(attachment_id, conv_a) is not None
    # A different conversation's bearer token must not be able to read
    # this attachment by guessing/enumerating its id.
    assert message_store.get_attachment(attachment_id, conv_b) is None


def test_deleting_conversation_cascades_to_attachments(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    conv = message_store.get_or_create_conversation(None)
    msg_id = message_store.record_message(conv, "user", "pic")
    message_store.record_attachment(msg_id, conv, kind="image", local_path="/tmp/a.png")

    with message_store._conn() as db:
        db.execute("DELETE FROM conversations WHERE id = ?", (conv,))

    assert message_store.get_attachments_for_message(msg_id) == []


def test_turn_generation_defaults_to_zero_for_unknown_or_fresh_conversation(tmp_path, monkeypatch):
    """A conversation whose turn_generation was never set (or that doesn't
    exist) reads back as 0, matching CopilotConversation's in-memory default."""
    _use_tmp_store(tmp_path, monkeypatch)
    conv = message_store.get_or_create_conversation(None)

    assert message_store.get_turn_generation(conv) == 0
    assert message_store.get_turn_generation("does-not-exist") == 0


def test_turn_generation_persists_and_is_scoped_per_conversation(tmp_path, monkeypatch):
    """Setting turn_generation for one conversation must not leak into or
    overwrite another conversation's own counter — this is the data layer
    session_turn's restart-recovery relies on (watchers.py's
    _poll_session_turn_watch via CopilotConversation.__init__)."""
    _use_tmp_store(tmp_path, monkeypatch)
    conv_a = message_store.get_or_create_conversation(None)
    conv_b = message_store.get_or_create_conversation(None)

    message_store.set_turn_generation(conv_a, 5)
    message_store.set_turn_generation(conv_b, 1)

    assert message_store.get_turn_generation(conv_a) == 5
    assert message_store.get_turn_generation(conv_b) == 1

    message_store.set_turn_generation(conv_a, 6)
    assert message_store.get_turn_generation(conv_a) == 6
    assert message_store.get_turn_generation(conv_b) == 1  # untouched


def test_set_turn_generation_is_a_noop_for_conversation_with_no_row(tmp_path, monkeypatch):
    """set_turn_generation() must not insert a partial/malformed
    conversations row for an id that was never created via
    get_or_create_conversation() — best-effort persistence, not a backdoor
    conversation-creation path."""
    _use_tmp_store(tmp_path, monkeypatch)
    message_store.set_turn_generation("never-created", 3)

    assert message_store.get_conversation("never-created") is None
    assert message_store.get_turn_generation("never-created") == 0


def test_new_live_message_unarchives_an_archived_session(tmp_path, monkeypatch):
    """A new live user/assistant message unarchives an archived session so it
    doesn't stay hidden while active; system messages and explicit-timestamp
    backfills do not."""
    _use_tmp_store(tmp_path, monkeypatch)
    conv = message_store.get_or_create_conversation(None)
    message_store.set_archived(conv, True)

    message_store.record_message(conv, "system", "note")
    message_store.record_message(conv, "user", "old", created_at="2026-01-01T00:00:00.000Z")
    assert bool(message_store.get_conversation(conv)["archived"]) is True

    message_store.record_message(conv, "user", "hello again")
    assert bool(message_store.get_conversation(conv)["archived"]) is False


def test_store_calls_reuse_one_connection_per_thread_and_init_db_runs_once(tmp_path, monkeypatch):
    """init_db runs once per DB path and each thread reuses one connection, so
    an idle bot doesn't burn CPU on repeated SQLite opens."""
    _use_tmp_store(tmp_path, monkeypatch)
    calls = []
    real_connect = message_store.sqlite3.connect
    monkeypatch.setattr(message_store.sqlite3, "connect", lambda *a, **k: calls.append(1) or real_connect(*a, **k))
    message_store._local.__dict__.clear()
    message_store._initialized_paths.clear()

    conv = message_store.get_or_create_conversation(None)
    for _ in range(20):
        message_store.get_conversation(conv)

    assert len(calls) == 1


def test_nested_conn_blocks_commit_only_at_outermost_level(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    message_store.init_db()
    conv = message_store.get_or_create_conversation(None)
    try:
        with message_store._conn() as conn:
            conn.execute("UPDATE conversations SET note='x' WHERE id=?", (conv,))
            with message_store._conn():
                pass
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert (message_store.get_conversation(conv).get("note") or "") != "x"
