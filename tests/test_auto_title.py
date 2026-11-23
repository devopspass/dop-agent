"""Auto-title: first message -> short model-generated title, never clobbering a custom one."""

from __future__ import annotations

import asyncio

import pytest

import auto_title
import config
import message_store


@pytest.fixture
def convo(tmp_path, monkeypatch):
    monkeypatch.setattr(message_store, "_TITLES_FILE", tmp_path / "titles.json")
    monkeypatch.setattr(auto_title, "enabled", lambda: True)
    monkeypatch.setattr(message_store, "get_conversation", lambda cid: {"id": cid})
    monkeypatch.setattr(message_store, "get_primary_binding", lambda *a, **k: None)
    monkeypatch.setattr(message_store, "_notify_change", lambda cid: None)
    return "c1"


def _run(cid, text, ask):
    return asyncio.run(auto_title.auto_title(cid, text, ask=ask))


def test_title_is_generated_and_stored(convo):
    async def ask(prompt):
        assert "fix the deploy" in prompt
        return 'Title: "Fix deploy pipeline."\n'

    assert _run(convo, "fix the deploy", ask) == "Fix deploy pipeline"
    assert message_store.get_title(convo) == "Fix deploy pipeline"


def test_existing_custom_title_is_not_overwritten(convo):
    async def ask(prompt):
        message_store.set_title(convo, "Mine")  # set while the model call is in flight
        return "Generated"

    assert _run(convo, "hello", ask) is None
    assert message_store.get_title(convo) == "Mine"


def test_failure_keeps_fallback(convo):
    async def ask(prompt):
        raise RuntimeError("boom")

    assert _run(convo, "hello", ask) is None
    assert message_store.get_title(convo) is None


def test_disabled_skips(convo, monkeypatch):
    monkeypatch.setattr(auto_title, "enabled", lambda: False)
    assert _run(convo, "hello", lambda p: None) is None


def test_clean_title_caps_length_and_takes_first_line():
    out = auto_title.clean_title("word " * 40 + "\nsecond line")
    assert len(out) <= auto_title.MAX_TITLE_LEN and "second" not in out


def test_title_session_is_toolless_denies_permissions_and_loads_no_instructions():
    """Title generation sends user text to the model, so its session must be the
    purest possible: no tools/MCP/skills/instructions, deny-all permissions,
    an empty scratch cwd and a replaced minimal system prompt (was approve_all
    with full tools in the repo workdir)."""
    kw = auto_title._session_kwargs("m")
    assert kw["available_tools"] == []
    assert kw["mcp_servers"] == {} and kw["enable_skills"] is False
    assert kw["skip_custom_instructions"] is True and kw["enable_config_discovery"] is False
    assert kw["system_message"]["mode"] == "replace"
    assert kw["working_directory"] != config.workdir()
    assert type(kw["on_permission_request"](None, {})).__name__ == "PermissionDecisionReject"
