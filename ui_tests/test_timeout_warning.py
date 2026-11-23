"""
Playwright test for the "approaching SESSION_TIMEOUT" warning
on the live elapsed-time counter (see webui/app.js's tickTurnElapsed()
and web_server.py's /api/health session_timeout_seconds field).
"""

from __future__ import annotations

import sys
from pathlib import Path

_BOT_DIR = Path(__file__).resolve().parent.parent
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))

import web_server  # noqa: E402


def test_elapsed_counter_shows_critical_warning_near_session_timeout(
    page, live_server, ui_test_password, monkeypatch
):
    # A tiny SESSION_TIMEOUT means the "remaining <= 10s" critical threshold
    # is already true the instant a turn starts, giving a fast/deterministic
    # assertion instead of needing to wait out most of a real timeout.
    #
    # This must be patched BEFORE the page's initial load (which fetches
    # /api/health once at init() and caches session_timeout_seconds
    # client-side, only refreshing every 30s) - hence not using the
    # logged_in_page fixture, which would already have loaded with the
    # unpatched value by the time this test body runs.
    monkeypatch.setattr(web_server.bot_config, "SESSION_TIMEOUT", 5.0)

    page.goto(f"{live_server}/login")
    page.fill("#password", ui_test_password)
    page.click("button[type=submit]")
    page.wait_for_url(f"{live_server}/")

    page.click("#new-session-btn")
    page.fill("#compose-text", "SLOW_TURN please think for a while")
    page.click("#compose-form button[type=submit]")

    page.wait_for_selector("#turn-elapsed.turn-elapsed-critical", timeout=5000)
    title = page.get_attribute("#turn-elapsed", "title")
    assert "5" in title and "timeout" in title.lower()

    page.click("#stop-turn-btn")
    page.wait_for_selector("#turn-elapsed", state="hidden", timeout=5000)
