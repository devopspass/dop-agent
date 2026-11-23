"""1:1 ids between bot conversations and Copilot CLI sessions.

A bot ``conversation_id`` (Web UI links, watch targets) equals the CLI
``session_id`` (session_get, ~/.copilot/session-state/<id>/): a conversation's
FIRST CLI session reuses the conversation UUID; only a replacement session
(resume failed) gets a fresh uuid4, since the CLI already knows the old id.
session_get also resolves a conversation_id whose conversation has differing ids.
"""
import asyncio

import copilot_session as cs
import mcp_tools
import message_store
from test_model_routing import _convo_with_client


def test_first_cli_session_reuses_the_conversation_uuid(tmp_path, monkeypatch):
    convo, client, cid = _convo_with_client(tmp_path, monkeypatch, None)
    asyncio.run(convo._ensure_session())
    assert client.calls[0][0] == "create" and client.calls[0][1]["session_id"] == cid


def test_replacement_session_after_failed_resume_gets_a_fresh_id(tmp_path, monkeypatch):
    class Failing:
        calls = []

        async def create_session(self, **kw):
            self.calls.append(("create", kw))
            from test_model_routing import _FakeSDKSession
            return _FakeSDKSession()

        async def resume_session(self, sid, **kw):
            raise RuntimeError("expired")

    convo, _, cid = _convo_with_client(tmp_path, monkeypatch, None, saved_sid="old", client=Failing())
    asyncio.run(convo._ensure_session())
    assert Failing.calls[0][1]["session_id"] not in (cid, "old")


def test_non_uuid_conversation_id_falls_back_to_random_uuid():
    sid = cs._first_session_id("not-a-uuid")
    assert sid != "not-a-uuid" and len(sid) == 36


def test_session_get_resolves_a_conversation_id_with_a_different_cli_session_id(tmp_path, monkeypatch):
    import sqlite3
    monkeypatch.setattr(message_store, "DB_PATH", tmp_path / "b.db")
    monkeypatch.setattr(message_store, "_TITLES_FILE", tmp_path / "t.json")
    message_store.init_db()
    cid = message_store.get_or_create_conversation(None)
    message_store.set_copilot_session_id(cid, "cli-1") if hasattr(message_store, "set_copilot_session_id") else None
    store = tmp_path / "s.db"
    c = sqlite3.connect(store)
    c.executescript("CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT, repository TEXT, summary TEXT, created_at TEXT);"
                    "CREATE TABLE turns (session_id TEXT, turn_index INTEGER, user_message TEXT, assistant_response TEXT, timestamp TEXT);"
                    "CREATE TABLE checkpoints (session_id TEXT, checkpoint_number INTEGER, title TEXT, overview TEXT, work_done TEXT, next_steps TEXT, created_at TEXT);"
                    "INSERT INTO sessions VALUES ('cli-1','/x','r','s','2026-01-01');")
    c.commit(); c.close()
    monkeypatch.setattr(mcp_tools, "COPILOT_SESSION_STORE", store)
    with message_store._conn() as conn:
        conn.execute("UPDATE conversations SET copilot_session_id='cli-1' WHERE id=?", (cid,))
    assert mcp_tools.session_get(cid)["id"] == "cli-1"
    assert mcp_tools.session_get("nope") is None
