"""
Playwright (Python) browser tests for the DOP Agent Web UI.

These are deliberately kept separate from dop-agent/tests/ (pure-Python
unit tests, no browser, no network) because they:
  - actually launch a real Chromium browser via Playwright and drive
    http://127.0.0.1:<port>/ like a real user would,
  - start a real uvicorn instance of web_server.app in a background thread,
    backed by a throwaway sqlite DB under tmp_path (never bot_store.db),
  - are slower and heavier than the unit suite, so they're run separately:
      /opt/homebrew/bin/python3.13 -m pytest dop-agent/ui_tests -q
  - require the Playwright Chromium browser to be installed once via
      /opt/homebrew/bin/python3.13 -m playwright install chromium

Manual/exploratory UI checks during development should instead use the
already-running live bot's Web UI driven via CDP against MS Edge (see the
dop-agent-improvement skill) — these automated Playwright tests are the
persisted, repeatable coverage that runs after every change.

None of this touches Slack, the real Copilot SDK, or the live bot_store.db —
`_handle_fn` is replaced with a fake that synchronously records a canned
assistant reply, so tests are fully offline and deterministic.
"""

from __future__ import annotations

import asyncio
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

_BOT_DIR = Path(__file__).resolve().parent.parent
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))

import message_store  # noqa: E402
import web_server  # noqa: E402


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _FakeSlackClient:
    async def chat_postMessage(self, *args, **kwargs):
        return {"ts": "1234567890.000001"}

    async def auth_test(self, *args, **kwargs):
        return {"ok": True}


class _FakeConvo:
    """Minimal stand-in for handlers.CopilotConversation, just enough for
    web_server._is_running() (checks `_lock.locked()`) and the
    `/api/sessions/{id}/stop` endpoint (calls `interrupt()`) to work against
    a real, observable "turn in progress" state during a UI test."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self.interrupted = False
        self._interrupt_event = asyncio.Event()
        self.turn_started_at: float | None = None

    async def interrupt(self) -> bool:
        if not self._lock.locked():
            return False
        self.interrupted = True
        self._interrupt_event.set()
        return True


_slow_turn_convos: dict = {}


async def _fake_handle_fn(*, conversation_id, text, **kwargs):
    """Stand-in for handlers.handle-turn.

    Messages containing "SLOW_TURN" simulate a long-running turn that stays
    "running" (lock held) until either a few seconds pass or someone calls
    stop/interrupt on its _FakeConvo - this lets ui_tests exercise the Stop
    button against real running/not-running state instead of an instant
    echo. Any other message instantly "answers".
    """
    if "SLOW_TURN" in text:
        convo = _FakeConvo()
        _slow_turn_convos[conversation_id] = convo
        web_server._convos[conversation_id] = convo
        convo.turn_started_at = time.time()
        async with convo._lock:
            try:
                await asyncio.wait_for(convo._interrupt_event.wait(), timeout=5)
                message_store.record_message(conversation_id, "system", "Interrupted — stopped early")
            except TimeoutError:
                message_store.record_message(conversation_id, "assistant", f"echo: {text}")
        return
    if "FAIL_TURN" in text:
        # Simulates handlers.py's convo.send() except-block wording for a
        # timed-out turn, so ui_tests can exercise the "Retry last message"
        # button without needing to actually make a real turn time out.
        message_store.record_message(
            conversation_id,
            "assistant",
            ":warning: DOP Agent request timed out (a tool call likely got stuck). "
            "Your thread history is preserved — send another message to resume.",
        )
        return
    message_store.record_message(conversation_id, "assistant", f"echo: {text}")


@pytest.fixture(scope="session")
def ui_test_password() -> str:
    return "ui-test-password"


@pytest.fixture(scope="session")
def live_server(tmp_path_factory, ui_test_password):
    """Start web_server.app on a real port, backed by a scratch DB."""
    tmp_path = tmp_path_factory.mktemp("webui-db")
    web_server.message_store.DB_PATH = tmp_path / "bot_store.db"
    web_server.message_store._TITLES_FILE = tmp_path / "session_titles.json"
    web_server.WEB_UI_PASSWORD = ui_test_password
    web_server._initialized = False  # force re-registration against the new DB

    import watchers
    watchers.DB_PATH = tmp_path / "watchers.db"
    watchers.init_db()

    # ui_tests exercise add_watch() with deliberately fake GitLab keys
    # (e.g. 'group/project#1') that don't correspond to any real MR/
    # pipeline/job - check_watch_resource_exists() would otherwise make a
    # real GitLab API call for each and either 404 (rejecting the test's own
    # registration) or hang/error without credentials. Session-scoped patch
    # (same stub as in dop-agent/tests/*.py fixtures) skips the live check.
    watchers.check_watch_resource_exists = lambda *a, **kw: None

    web_server.init(
        slack_client=_FakeSlackClient(),
        handle_fn=_fake_handle_fn,
        convos={},
        is_authorized=lambda user_id: True,
        allowed_user_id="U_TEST",
    )

    port = _free_port()
    web_server.ALLOWED_HOSTS = frozenset(web_server.ALLOWED_HOSTS | {f"127.0.0.1:{port}"})
    config = uvicorn.Config(web_server.app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("uvicorn server did not start in time")

    base_url = f"http://127.0.0.1:{port}"
    yield base_url

    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    # Playwright's wait_for_function evals strings in-page, which the strict
    # script-src 'self' CSP blocks; CSP itself is checked in tests/test_web_server.py.
    return {**browser_context_args, "bypass_csp": True}


@pytest.fixture
def logged_in_page(page, live_server, ui_test_password):
    """A Playwright page that has already logged into the Web UI."""
    page.goto(f"{live_server}/login")
    page.fill("#password", ui_test_password)
    page.click("button[type=submit]")
    page.wait_for_url(f"{live_server}/")
    return page


@pytest.fixture(autouse=True)
def _fail_on_console_errors(page):
    """Fails the test if the browser logs a JS console error or an uncaught
    exception while it runs.

    A JS runtime error thrown while wiring up one feature's event listeners
    (e.g. a null-reference from a mistyped element id) can silently prevent
    every subsequent top-level statement in app.js from running — including
    unrelated buttons' click handlers — while the page still looks fine.
    Isolated single-feature tests miss that, but it shows up as a console
    error. Applies to every ui_tests test (autouse).
    """
    errors: list[str] = []
    page.on("console", lambda msg: errors.append(f"console.error: {msg.text}") if msg.type == "error" else None)
    page.on("pageerror", lambda exc: errors.append(f"uncaught exception: {exc}"))
    yield errors
    assert not errors, "Unexpected browser console error(s):\n" + "\n".join(errors)
