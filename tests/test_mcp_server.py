"""
Tests for the in-process MCP server mounted into web_server.py at /mcp.

Two layers of coverage:
  1. The MCP registration layer itself (web_server.mcp_server.list_tools() /
     call_tool()) — validates that all expected tools are registered with the
     right names and that the FastMCP argument-binding/schema layer
     correctly forwards calls into mcp_tools.py's pure functions (not just
     that the pure functions work, which test_mcp_tools.py already covers).
  2. The HTTP transport + auth gate (_McpAuthMiddleware) via FastAPI's
     TestClient against the real /mcp mount: no token configured -> 500,
     wrong/missing Authorization header -> 401, correct bearer token -> a
     real MCP JSON-RPC response.

Same DB-isolation pattern as test_mcp_tools.py/test_web_server.py:
monkeypatch watchers.DB_PATH, message_store.DB_PATH, and
mcp_tools.COPILOT_SESSION_STORE to per-test tmp_path files.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import copilot_session
import mcp_tools
import message_store
import watchers
import web_server


EXPECTED_TOOL_NAMES = {
    "watch_add",
    "watch_script",
    "watch_list",
    "watch_get",
    "watch_pause",
    "watch_resume",
    "watch_remove",
    "watch_pause_session",
    "session_search",
    "session_get",
    "session_list",
    "session_create",
    "session_send",
    "session_pin",
    # session groups
    "group_list",
    "group_manage",
}


def _use_tmp_stores(tmp_path, monkeypatch):
    monkeypatch.setattr(watchers, "DB_PATH", tmp_path / "watchers.db")
    watchers.init_db()
    monkeypatch.setattr(watchers, "_post_watch_announcement", lambda *a, **kw: False)
    monkeypatch.setattr(watchers, "check_watch_resource_exists", lambda *a, **kw: None)

    monkeypatch.setattr(message_store, "DB_PATH", tmp_path / "bot_store.db")
    monkeypatch.setattr(message_store, "_TITLES_FILE", tmp_path / "session_titles.json")
    message_store.init_db()

    monkeypatch.setattr(mcp_tools, "COPILOT_SESSION_STORE", tmp_path / "session-store.db")


def _slack_conversation_id(channel: str, thread_ts: str) -> str:
    return copilot_session._conversation_id_for(channel, thread_ts)


def _first_text_result(result):
    """FastMCP's call_tool() has an inconsistent return shape depending on
    the tool's return type annotation:
      - Plain `dict` returns (e.g. watch_add) -> a bare list with one
        TextContent block containing the JSON-serialized dict.
      - `dict | None`, `list[dict]`, or other non-plain-object returns
        (e.g. watch_get, watch_list, session_search) -> a
        (content_blocks, structured_content) tuple, where
        structured_content["result"] already holds the real Python value
        (dict/list/None) — no need to re-parse the text blocks (which,
        for multi-item lists, are split one-JSON-object-per-block anyway).
    Normalize both shapes to the actual returned Python value."""
    if isinstance(result, tuple):
        _, structured = result
        return structured["result"]
    content = result
    assert content, "expected at least one content block"
    return json.loads(content[0].text)


# --- registration/schema layer -------------------------------------------

@pytest.mark.asyncio
async def test_all_fourteen_tools_registered():
    tools = await web_server.mcp_server.list_tools()
    names = {t.name for t in tools}
    assert names == EXPECTED_TOOL_NAMES


@pytest.mark.asyncio
async def test_call_tool_watch_add_and_list(tmp_path, monkeypatch):
    _use_tmp_stores(tmp_path, monkeypatch)

    conversation_id = _slack_conversation_id("C1", "111.111")
    added = await web_server.mcp_server.call_tool(
        "watch_add", {"type": "time", "session_id": conversation_id, "key": "in:5m", "label": "t"}
    )
    payload = _first_text_result(added)
    assert isinstance(payload["watch_id"], int)

    listed = await web_server.mcp_server.call_tool("watch_list", {})
    watches = _first_text_result(listed)
    assert any(w["id"] == payload["watch_id"] for w in watches)


@pytest.mark.asyncio
async def test_call_tool_watch_get_pause_resume_remove(tmp_path, monkeypatch):
    _use_tmp_stores(tmp_path, monkeypatch)

    conversation_id = _slack_conversation_id("C1", "111.111")
    added = await web_server.mcp_server.call_tool(
        "watch_add", {"type": "time", "session_id": conversation_id, "key": "in:5m", "label": "t"}
    )
    watch_id = _first_text_result(added)["watch_id"]

    got = await web_server.mcp_server.call_tool("watch_get", {"watch_id": watch_id})
    assert _first_text_result(got)["key"] == "in:5m"

    paused = await web_server.mcp_server.call_tool("watch_pause", {"ids": watch_id})
    assert _first_text_result(paused) == {"done": [watch_id], "skipped": []}

    resumed = await web_server.mcp_server.call_tool("watch_resume", {"ids": [watch_id]})
    assert _first_text_result(resumed) == {"done": [watch_id], "skipped": []}

    removed = await web_server.mcp_server.call_tool("watch_remove", {"ids": [watch_id]})
    assert _first_text_result(removed) == {"done": [watch_id], "skipped": []}

    got_after_remove = await web_server.mcp_server.call_tool("watch_get", {"watch_id": watch_id})
    assert _first_text_result(got_after_remove) is None


@pytest.mark.asyncio
async def test_call_tool_watch_pause_session(tmp_path, monkeypatch):
    _use_tmp_stores(tmp_path, monkeypatch)

    conversation_id = _slack_conversation_id("C1", "111.111")
    await web_server.mcp_server.call_tool(
        "watch_add", {"type": "time", "session_id": conversation_id, "key": "in:5m", "label": "t"}
    )
    await web_server.mcp_server.call_tool(
        "watch_add", {"type": "time", "session_id": conversation_id, "key": "in:6m", "label": "t"}
    )

    off = await web_server.mcp_server.call_tool("watch_pause_session", {"session_id": conversation_id})
    assert _first_text_result(off) == {"paused_count": 2}


@pytest.mark.asyncio
async def test_call_tool_session_search_and_get(tmp_path, monkeypatch):
    _use_tmp_stores(tmp_path, monkeypatch)
    conv_id = message_store.get_or_create_conversation("C1:1.1", copilot_session_id="sess-1")
    message_store.record_message(conv_id, "user", "find this via mcp call_tool test marker")

    searched = await web_server.mcp_server.call_tool("session_search", {"query": "call_tool test marker"})
    results = _first_text_result(searched)
    assert len(results) == 1
    assert results[0]["session_id"] == "sess-1"

    listed = await web_server.mcp_server.call_tool("session_list", {})
    convos = _first_text_result(listed)
    assert any(c["conversation_id"] == conv_id for c in convos)

    got = await web_server.mcp_server.call_tool("session_get", {"session_id": "does-not-exist"})
    assert _first_text_result(got) is None


# --- HTTP transport + auth gate ------------------------------------------
#
# All four scenarios below share ONE TestClient/lifespan cycle: FastMCP's
# StreamableHTTPSessionManager.run() (wired into web_server._lifespan) can
# only be entered once per mcp_server instance for the life of the process
# — a second `with TestClient(web_server.app)` block would hit "session
# manager .run() can only be called once per instance". Since mcp_server is
# a module-level singleton shared by the whole test process, every /mcp
# HTTP-transport case must be exercised inside a single `with` block.

def _mcp_initialize_body():
    return {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "t", "version": "1"}},
    }


def test_mcp_endpoint_auth_scenarios(monkeypatch):
    accept_headers = {"Accept": "application/json, text/event-stream"}

    with TestClient(web_server.app) as client:
        # No token configured at all -> 500, never silently unauthenticated.
        monkeypatch.setattr(web_server, "MCP_AUTH_TOKEN", "")
        resp = client.post("/mcp", json=_mcp_initialize_body(), headers=accept_headers)
        assert resp.status_code == 500

        monkeypatch.setattr(web_server, "MCP_AUTH_TOKEN", "test-token-abc")

        # Missing Authorization header -> 401.
        resp = client.post("/mcp", json=_mcp_initialize_body(), headers=accept_headers)
        assert resp.status_code == 401

        # Wrong token -> 401.
        resp = client.post(
            "/mcp", json=_mcp_initialize_body(),
            headers={**accept_headers, "Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401

        # Correct token -> a real MCP JSON-RPC response.
        resp = client.post(
            "/mcp", json=_mcp_initialize_body(),
            headers={**accept_headers, "Authorization": "Bearer test-token-abc"},
        )
        assert resp.status_code == 200
        assert "dop-agent" in resp.text


# --- Host allow-list (DNS-rebinding defence) -----------------------------

@pytest.mark.parametrize("path", ["/mcp", "/api/login", "/login", "/api/sessions"])
def test_rebinding_host_rejected(path):
    client = TestClient(web_server.app)
    for host in ("evil.example", "evil.example:10818", "localhost:1"):
        resp = client.get(path, headers={"Host": host})
        assert resp.status_code == 403


def test_loopback_host_allowed():
    client = TestClient(web_server.app)
    resp = client.get("/mcp", headers={"Host": f"localhost:{web_server.WEB_UI_PORT}"})
    assert resp.status_code != 403


def test_mcp_token_has_no_password_fallback():
    import inspect
    assert "WEB_UI_PASSWORD" not in inspect.getsource(web_server._McpAuthMiddleware)
    assert web_server.MCP_AUTH_TOKEN == "" or web_server.MCP_AUTH_TOKEN != web_server.WEB_UI_PASSWORD


# --- watcher exposure follows enabled integrations -----------------------

def _registry_with(monkeypatch, **enabled):
    from integrations import runtime
    from integrations.registry import build_registry
    from integration_defaults import default_test_integrations as default_integrations
    cfg = default_integrations()
    for name, on in enabled.items():
        cfg[name]["enabled"] = on
    monkeypatch.setattr(runtime, "_registry", build_registry(cfg))
    monkeypatch.setattr(runtime, "_hooked", True)


async def _watch_add_description():
    tools = await web_server.mcp_server.list_tools()
    return next(t for t in tools if t.name == "watch_add").description


@pytest.mark.asyncio
async def test_watch_add_description_lists_builtin_types_only(tmp_path, monkeypatch):
    _use_tmp_stores(tmp_path, monkeypatch)
    _registry_with(monkeypatch, jira=True, gitlab=True)
    desc = await _watch_add_description()
    for t in ("time:", "script:", "session_turn:", "self_restart:"):
        assert t in desc
    for t in ("mr:", "pipeline:", "job:", "ticket:"):
        assert t not in desc


@pytest.mark.asyncio
async def test_watch_add_rejects_type_of_disabled_integration(tmp_path, monkeypatch):
    _use_tmp_stores(tmp_path, monkeypatch)
    _registry_with(monkeypatch, jira=False, gitlab=False)
    with pytest.raises(Exception, match="Unknown watch type"):
        await web_server.mcp_server.call_tool(
            "watch_add", {"type": "mr", "session_id": _slack_conversation_id("C1", "1.1"),
                          "key": "group/project#1"})
