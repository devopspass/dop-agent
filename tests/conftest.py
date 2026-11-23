"""
Shared pytest fixtures for dop-agent unit tests.

These tests exercise pure/isolated logic only (parsing, formatting,
persistence against a throwaway sqlite file) — nothing here talks to Slack,
the Copilot SDK, or any real network service, so the suite runs fast and
fully offline.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Make `import message_store`, `import dop_agent`, etc. work regardless of
# the directory pytest is invoked from (repo root or dop-agent/).
_BOT_DIR = Path(__file__).resolve().parent.parent
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))
_FAKES_DIR = Path(__file__).resolve().parent / "fakes"
if str(_FAKES_DIR) not in sys.path:
    sys.path.insert(0, str(_FAKES_DIR))

os.environ.setdefault("WEB_UI_ALLOWED_HOSTS", "testserver")
os.environ.setdefault("AUTO_TITLE", "0")  # never call a real model from unit tests
os.environ.setdefault("DOP_AGENT_CONFIG", "/nonexistent/dop-agent-test-config.yaml")

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_message_store(tmp_path_factory, monkeypatch):
    """Safety net: never let a test touch the real bot_store.db / titles file.

    Tests that need a specific store still re-patch these themselves (their
    monkeypatch runs after this one and wins).
    """
    import message_store

    tmp = tmp_path_factory.mktemp("isolated-store")
    monkeypatch.setattr(message_store, "DB_PATH", tmp / "bot_store.db")
    monkeypatch.setattr(message_store, "_TITLES_FILE", tmp / "session_titles.json")


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_global_adapter_registry():
    """web_server.configure()/watchers._registry() register adapters in the
    process-wide REGISTRY; snapshot and restore it so one test's fake Slack
    client can never leak into the next."""
    import adapters

    saved = dict(adapters.REGISTRY._adapters)
    yield
    adapters.REGISTRY._adapters.clear()
    adapters.REGISTRY._adapters.update(saved)


@pytest.fixture(autouse=True)
def _isolate_side_files(tmp_path_factory, monkeypatch):
    """Never let a test read-modify-write the LIVE crash-recovery side files
    (active_status.json / pending_transcriptions.json) that sit next to the
    running daemon, so test entries never show up in real startup recovery.
    Tests that need a specific path re-patch it."""
    import persistence

    tmp = tmp_path_factory.mktemp("isolated-side-files")
    monkeypatch.setattr(persistence, "ACTIVE_STATUS_FILE", tmp / "active_status.json")
    monkeypatch.setattr(persistence, "PENDING_TRANSCRIPTIONS_FILE", tmp / "pending_transcriptions.json")


@pytest.fixture(autouse=True)
def _isolate_watchers_db(tmp_path_factory, monkeypatch):
    """Never let a test add watches to the LIVE watchers.db."""
    import watchers

    tmp = tmp_path_factory.mktemp("isolated-watchers")
    monkeypatch.setattr(watchers, "DB_PATH", tmp / "watchers.db")


@pytest.fixture(autouse=True)
def _isolate_audio_store(tmp_path_factory, monkeypatch):
    """Never let a test write recordings into the real ~/.dop-agent/audio."""
    import audio_store

    monkeypatch.setattr(audio_store, "ROOT", tmp_path_factory.mktemp("isolated-audio"))


HERMETIC_INTEGRATIONS = {
    "jira": {"driver": "jira", "base_url": "https://jira.test", "auth": {"type": "token_file"},
             "options": {"email": "u@test.example", "token_file": "/tmp/jira-test-token"}},
    "gitlab": {"driver": "gitlab", "base_url": "https://gitlab.test",
               "auth": {"type": "private_token", "token": "t"}},
}


@pytest.fixture(autouse=True)
def _hermetic_integrations(monkeypatch):
    """Every test gets a fresh jira+gitlab registry with fake credentials, never the
    real config / mcp-config.json (drivers build lazily, so no client is made unless a test fakes one)."""
    from integrations import runtime
    from integrations.registry import build_registry

    monkeypatch.setattr(runtime, "_registry", build_registry(HERMETIC_INTEGRATIONS))
    monkeypatch.setattr(runtime, "_hooked", True)


@pytest.fixture(autouse=True)
def _webui_csrf_header(monkeypatch, request):
    """Web UI mutating /api routes need `X-Requested-With: dop` (what the real UI's csrf.js
    sends). Default it on every TestClient; tests of the guard itself override/remove it."""
    from fastapi.testclient import TestClient

    if request.module.__name__.endswith("test_settings_api"):
        return  # those tests assert the missing-header rejection themselves
    orig = TestClient.__init__

    def init(self, *a, **kw):
        orig(self, *a, **kw)
        self.headers.setdefault("X-Requested-With", "dop")

    monkeypatch.setattr(TestClient, "__init__", init)
