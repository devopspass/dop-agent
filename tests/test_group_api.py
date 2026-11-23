"""
REST + MCP surface of the session-groups feature (see test_session_groups.py
for the data layer): /api/groups CRUD, assigning sessions, master, overview,
group fields in /api/sessions, and session_create's default group inheritance
(children join the parent's group unless inherit_group=False; group_id overrides).
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

import message_store
import web_server
from test_web_server import _FakeSlackClient, _login, _setup_session_create_runtime, _use_tmp_store


def _client(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    client = TestClient(web_server.app)
    _login(client, monkeypatch)
    return client


def test_group_crud_assign_and_sessions_payload_carries_group_fields(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    cid = message_store.get_or_create_conversation(None)
    gid = client.post("/api/groups", json={"title": "Bot dev", "emoji": "🤖"}).json()["id"]
    assert client.put(f"/api/sessions/{cid}/group", json={"group_id": gid}).status_code == 200
    assert client.put(f"/api/groups/{gid}/master", json={"conversation_id": cid}).json()["master_id"] == cid

    data = client.get("/api/sessions").json()
    item = next(s for s in data["sessions"] if s["conversation_id"] == cid)
    assert item["group_id"] == gid and item["group_title"] == "Bot dev" and item["is_group_master"]
    grp = next(g for g in data["groups"] if g["id"] == gid)
    assert grp["session_count"] == 1 and grp["master_id"] == cid and grp["status"] in ("idle", "unread", "running", "errored")

    assert client.patch(f"/api/groups/{gid}", json={"title": "Renamed"}).json()["title"] == "Renamed"
    assert client.delete(f"/api/groups/{gid}").json() == {"unassigned": 1}


def test_group_endpoints_404_and_400_on_bad_input(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    cid = message_store.get_or_create_conversation(None)
    assert client.put(f"/api/sessions/{cid}/group", json={"group_id": "nope"}).status_code == 404
    assert client.post("/api/groups", json={"title": " "}).status_code == 400
    assert client.get("/api/groups/nope/overview").status_code == 404
    gid = client.post("/api/groups", json={"title": "G"}).json()["id"]
    assert client.put(f"/api/groups/{gid}/master", json={"conversation_id": cid}).status_code == 400


def test_group_archive_endpoint_cascades_to_member_sessions(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    cid = message_store.get_or_create_conversation(None)
    gid = client.post("/api/groups", json={"title": "G"}).json()["id"]
    client.put(f"/api/sessions/{cid}/group", json={"group_id": gid})
    assert client.put(f"/api/groups/{gid}/archive", json={"archived": True}).json()["sessions"] == 1
    assert next(s for s in client.get("/api/sessions").json()["sessions"] if s["conversation_id"] == cid)["archived"]


def test_overview_and_suggest_endpoints(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    gid = client.post("/api/groups", json={"title": "PRJ-900 proxy"}).json()["id"]
    ov = client.get(f"/api/groups/{gid}/overview").json()
    assert ov["group"]["id"] == gid and ov["sessions"] == [] and "artifacts" in ov
    sug = client.post("/api/groups/suggest", json={"prompt": "continue PRJ-900 work"}).json()["suggestion"]
    assert sug["group_id"] == gid
    assert client.post("/api/groups/suggest", json={"prompt": "unrelated"}).json()["suggestion"] is None


def test_session_create_inherits_parent_group_by_default_unless_opted_out(tmp_path, monkeypatch):
    """A child inherits its parent's group by default (opt out with
    inherit_group=False). If the parent has no group, one is auto-created
    from the parent's title with the parent as master."""
    _use_tmp_store(tmp_path, monkeypatch)
    parent = message_store.get_or_create_conversation(None)
    message_store.set_title(parent, "Master task")
    scheduled = _setup_session_create_runtime(monkeypatch, _FakeSlackClient())

    opt_out = asyncio.run(web_server.session_create("child a", parent_session_id=parent, inherit_group=False))
    assert opt_out["group_id"] is None and message_store.get_session_group_id(parent) is None

    inh = asyncio.run(web_server.session_create("child b", parent_session_id=parent))
    for coro in scheduled:
        coro.close()
    gid = inh["group_id"]
    assert gid and message_store.get_session_group_id(inh["conversation_id"]) == gid
    assert message_store.list_groups()[0]["master_id"] == parent


def test_session_create_inherit_requires_parent_and_group_id_must_exist(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    _setup_session_create_runtime(monkeypatch, _FakeSlackClient())
    with pytest.raises(ValueError, match="parent_session_id"):
        asyncio.run(web_server.session_create("x", inherit_group=True))
    with pytest.raises(ValueError, match="group_list"):
        asyncio.run(web_server.session_create("x", group_id="nope"))


def test_group_mcp_tools_and_session_list_filter(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    a = message_store.get_or_create_conversation(None)
    b = message_store.get_or_create_conversation(None)
    g = web_server.group_manage("create", title="G")["id"]
    web_server.group_manage("assign", session_id=a, group_id=g, make_master=True)
    assert [c["conversation_id"] for c in web_server.session_list(group_id=g)] == [a]
    assert [c["conversation_id"] for c in web_server.session_list(group_id="none")] == [b]
    assert web_server.group_list()[0]["master_id"] == a
    with pytest.raises(ValueError, match="group_manage"):
        web_server.group_manage("assign", session_id=a, group_id="bad")
    assert web_server.group_manage("archive", group_id=g)["sessions"] == 1
    assert web_server.group_manage("delete", group_id=g)["unassigned"] == 1


def test_mark_group_read_clears_unread_for_all_member_sessions_only(tmp_path, monkeypatch):
    """Feature: group menu "Mark all as read" -> PUT /api/groups/{id}/read marks
    every unread member session read in one call, leaving non-members untouched."""
    client = _client(tmp_path, monkeypatch)
    a = message_store.get_or_create_conversation(None)
    b = message_store.get_or_create_conversation(None)
    other = message_store.get_or_create_conversation(None)
    gid = client.post("/api/groups", json={"title": "G"}).json()["id"]
    for cid in (a, b):
        client.put(f"/api/sessions/{cid}/group", json={"group_id": gid})
    assert client.put(f"/api/groups/{gid}/read").json()["marked"] >= 0
    for cid in (a, b):
        assert message_store.get_last_read(cid) == message_store.get_conversation(cid)["updated_at"]
    assert message_store.get_last_read(other) is None
    assert client.put("/api/groups/nope/read").status_code == 404


def test_group_read_route_is_registered_and_authed_client_never_gets_401(tmp_path, monkeypatch):
    """Bug: right after the feature shipped, the running daemon still had old code, so
    PUT /api/groups/{id}/read fell through to the catch-all /mcp mount, which answers
    401 {"error": "unauthorized"}; the UI's api() read any 401 as "session expired"
    and logged the user out ("Could not mark group read: unauthenticated").
    Guards: the route is registered on the app itself (not the mount), an authenticated
    client gets 200 (not 401), and an anonymous one gets our own 401 'Not authenticated'
    detail, which is what the UI uses to tell a real logout from a foreign 401."""
    client = _client(tmp_path, monkeypatch)
    assert any(getattr(r, "path", "") == "/api/groups/{group_id}/read" for r in web_server.app.routes)
    gid = client.post("/api/groups", json={"title": "G"}).json()["id"]
    assert client.put(f"/api/groups/{gid}/read").status_code == 200
    anon = TestClient(web_server.app)
    resp = anon.put(f"/api/groups/{gid}/read", headers={"X-Requested-With": "dop"})
    assert resp.status_code == 401 and resp.json()["detail"] == "Not authenticated"
