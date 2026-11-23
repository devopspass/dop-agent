"""
Unit tests for bot_config.py — environment-driven configuration constants,
MCP server config loading, and optional-image-dependency detection.
"""

from __future__ import annotations

import importlib
import json
import os

import bot_config


def test_interrupted_is_a_stable_sentinel():
    # Must be a single object identity, not e.g. re-created per import,
    # since callers compare `result is INTERRUPTED`.
    assert bot_config.INTERRUPTED is bot_config.INTERRUPTED


def test_todo_status_icon_covers_all_known_states():
    assert set(bot_config._TODO_STATUS_ICON) == {"done", "in_progress", "pending", "blocked"}
    for icon in bot_config._TODO_STATUS_ICON.values():
        assert icon.startswith(":") and icon.endswith(":")


def test_slack_block_max_chars_smaller_than_slack_formatting_limit():
    from slack_formatting import SLACK_MAX_CHARS

    # The live status block has a tighter cap than the plain-text fallback.
    assert bot_config.SLACK_BLOCK_MAX_CHARS < SLACK_MAX_CHARS


def test_missing_image_render_deps_all_present(monkeypatch):
    monkeypatch.setattr(bot_config, "fitz", object())
    monkeypatch.setattr(bot_config, "Image", object())
    monkeypatch.setattr(bot_config, "ImageChops", object())
    monkeypatch.setattr(bot_config, "WeasyHTML", object())
    assert bot_config.missing_image_render_deps() == []


def test_missing_image_render_deps_reports_each_missing_package(monkeypatch):
    monkeypatch.setattr(bot_config, "fitz", None)
    monkeypatch.setattr(bot_config, "Image", None)
    monkeypatch.setattr(bot_config, "ImageChops", None)
    monkeypatch.setattr(bot_config, "WeasyHTML", None)
    missing = bot_config.missing_image_render_deps()
    assert missing == ["PyMuPDF (fitz)", "Pillow", "WeasyPrint"]


def test_missing_image_render_deps_partial(monkeypatch):
    monkeypatch.setattr(bot_config, "fitz", object())
    monkeypatch.setattr(bot_config, "Image", None)
    monkeypatch.setattr(bot_config, "ImageChops", None)
    monkeypatch.setattr(bot_config, "WeasyHTML", object())
    assert bot_config.missing_image_render_deps() == ["Pillow"]


def test_load_mcp_servers_returns_none_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_config, "MCP_CONFIG_FILE", tmp_path / "does-not-exist.json")
    assert bot_config._load_mcp_servers() is None


def test_load_mcp_servers_returns_none_on_invalid_json(tmp_path, monkeypatch):
    cfg = tmp_path / "mcp-config.json"
    cfg.write_text("{not valid json")
    monkeypatch.setattr(bot_config, "MCP_CONFIG_FILE", cfg)
    assert bot_config._load_mcp_servers() is None


def test_load_mcp_servers_returns_none_when_empty(tmp_path, monkeypatch):
    cfg = tmp_path / "mcp-config.json"
    cfg.write_text(json.dumps({"mcpServers": {}}))
    monkeypatch.setattr(bot_config, "MCP_CONFIG_FILE", cfg)
    assert bot_config._load_mcp_servers() is None


def test_load_mcp_servers_returns_servers_dict(tmp_path, monkeypatch):
    servers = {"gitlab": {"type": "local", "command": "gitlab-mcp"}}
    cfg = tmp_path / "mcp-config.json"
    cfg.write_text(json.dumps({"mcpServers": servers}))
    monkeypatch.setattr(bot_config, "MCP_CONFIG_FILE", cfg)
    assert bot_config._load_mcp_servers() == servers


def test_build_session_mcp_servers_adds_self_entry_when_token_set(monkeypatch):
    monkeypatch.setattr(bot_config, "_SELF_MCP_AUTH_TOKEN", "some-token")
    monkeypatch.setattr(bot_config, "_SELF_MCP_WEB_UI_PORT", 12345)
    external = {"gitlab": {"type": "local", "command": "gitlab-mcp"}}
    merged = bot_config._build_session_mcp_servers(external)
    assert merged["gitlab"] == external["gitlab"]
    assert merged["dop-agent"] == {
        "type": "http",
        "url": "http://127.0.0.1:12345/mcp",
        "headers": {"Authorization": "Bearer some-token"},
        "tools": ["*"],
    }
    # external dict itself must not be mutated.
    assert "dop-agent" not in external


def test_build_session_mcp_servers_omits_self_entry_without_token(monkeypatch):
    monkeypatch.setattr(bot_config, "_SELF_MCP_AUTH_TOKEN", "")
    merged = bot_config._build_session_mcp_servers({"gitlab": {"type": "local"}})
    assert "dop-agent" not in merged


def test_build_session_mcp_servers_handles_no_external_servers(monkeypatch):
    monkeypatch.setattr(bot_config, "_SELF_MCP_AUTH_TOKEN", "some-token")
    merged = bot_config._build_session_mcp_servers(None)
    assert set(merged.keys()) == {"dop-agent"}


def test_probe_mcp_server_statuses_no_secrets_leaked(monkeypatch):
    # A config entry carrying a real-looking secret in `env` must never be
    # echoed back — only name/type/status/detail are allowed out.
    monkeypatch.setattr(bot_config, "MCP_SERVERS", {
        "gitlab": {
            "type": "local",
            "command": "/bin/echo",
            "args": [],
            "env": {"GITLAB_PERSONAL_ACCESS_TOKEN": "glpat-super-secret-value"},
        },
    })
    monkeypatch.setenv("MCP_AUTH_TOKEN", "some-token")
    statuses = bot_config.probe_mcp_server_statuses()
    dumped = json.dumps(statuses)
    assert "glpat-super-secret-value" not in dumped
    assert set().union(*(entry.keys() for entry in statuses)) == {"name", "type", "status", "detail"}


def test_probe_mcp_server_statuses_ok_when_command_resolves(monkeypatch):
    monkeypatch.setattr(bot_config, "MCP_SERVERS", {
        "echo-server": {"type": "local", "command": "/bin/echo", "args": ["hi"]},
    })
    monkeypatch.setenv("MCP_AUTH_TOKEN", "some-token")
    statuses = {e["name"]: e for e in bot_config.probe_mcp_server_statuses()}
    assert statuses["echo-server"]["status"] == "ok"


def test_probe_mcp_server_statuses_error_when_command_missing(monkeypatch):
    monkeypatch.setattr(bot_config, "MCP_SERVERS", {
        "broken": {"type": "local", "command": "/definitely/not/a/real/binary-xyz"},
    })
    monkeypatch.setenv("MCP_AUTH_TOKEN", "some-token")
    statuses = {e["name"]: e for e in bot_config.probe_mcp_server_statuses()}
    assert statuses["broken"]["status"] == "error"
    assert "not found" in statuses["broken"]["detail"]


def test_probe_mcp_server_statuses_error_when_script_arg_missing(monkeypatch):
    monkeypatch.setattr(bot_config, "MCP_SERVERS", {
        "gitlab": {"type": "local", "command": "/bin/echo", "args": ["/no/such/path/index.js"]},
    })
    monkeypatch.setenv("MCP_AUTH_TOKEN", "some-token")
    statuses = {e["name"]: e for e in bot_config.probe_mcp_server_statuses()}
    assert statuses["gitlab"]["status"] == "error"
    assert "script not found" in statuses["gitlab"]["detail"]


def test_probe_mcp_server_statuses_unknown_for_remote_transport(monkeypatch):
    monkeypatch.setattr(bot_config, "MCP_SERVERS", {
        "remote-thing": {"type": "http", "url": "https://example.com/mcp"},
    })
    monkeypatch.setenv("MCP_AUTH_TOKEN", "some-token")
    statuses = {e["name"]: e for e in bot_config.probe_mcp_server_statuses()}
    assert statuses["remote-thing"]["status"] == "unknown"


def test_probe_mcp_server_statuses_always_includes_self_entry(monkeypatch):
    monkeypatch.setattr(bot_config, "MCP_SERVERS", {})
    monkeypatch.setenv("MCP_AUTH_TOKEN", "some-token")
    statuses = bot_config.probe_mcp_server_statuses()
    self_entries = [e for e in statuses if "this bot" in e["name"]]
    assert len(self_entries) == 1
    assert self_entries[0]["status"] == "ok"


def test_probe_mcp_server_statuses_self_entry_errors_without_token(monkeypatch):
    monkeypatch.setattr(bot_config, "MCP_SERVERS", {})
    monkeypatch.setenv("MCP_AUTH_TOKEN", "")
    monkeypatch.delenv("WEB_UI_PASSWORD", raising=False)
    statuses = bot_config.probe_mcp_server_statuses()
    self_entries = [e for e in statuses if "this bot" in e["name"]]
    assert self_entries[0]["status"] == "error"
    assert "not set" in self_entries[0]["detail"]


def test_audio_auto_send_parses_truthy_env_values(monkeypatch):
    for value, expected in [("true", True), ("YES", True), ("1", True), ("on", True),
                             ("false", False), ("0", False), ("", False), ("nope", False)]:
        monkeypatch.setenv("AUDIO_AUTO_SEND", value)
        importlib.reload(bot_config)
        assert bot_config.AUDIO_AUTO_SEND is expected, f"value={value!r}"
    # Restore module state for any tests that run after this one in-process.
    monkeypatch.delenv("AUDIO_AUTO_SEND", raising=False)
    importlib.reload(bot_config)


def test_table_render_mode_falls_back_to_ascii_when_render_extra_not_installed():
    """Fresh interpreter with the render extra (fitz/PIL/weasyprint) absent."""
    import subprocess
    import sys
    from pathlib import Path

    code = (
        "import sys\n"
        "for m in ('fitz', 'PIL', 'weasyprint'):\n"
        "    sys.modules[m] = None\n"  # makes `import m` raise ModuleNotFoundError
        "import bot_config, render_tables, render_markdown\n"
        "print(bot_config.TABLE_RENDER_MODE, len(bot_config.MISSING_IMAGE_DEPS))\n"
    )
    env = {**os.environ, "TABLE_RENDER_MODE": "image"}
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parent.parent,
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["ascii", "3"]

    importlib.reload(bot_config)


def test_self_mcp_token_ignores_web_ui_password(monkeypatch):
    import importlib
    monkeypatch.setenv("MCP_AUTH_TOKEN", "")
    monkeypatch.setenv("WEB_UI_PASSWORD", "weak")
    try:
        reloaded = importlib.reload(bot_config)
        assert reloaded._SELF_MCP_AUTH_TOKEN == ""
    finally:
        monkeypatch.undo()
        importlib.reload(bot_config)
