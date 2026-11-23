"""
Playwright test for stopping/interrupting an in-progress turn
from the Web UI (parity with Slack's ":black_square_for_stop: Stop"
button) - see web_server.py's /api/sessions/{id}/stop endpoint and
webui/app.js's stop-turn-btn wiring.

Also covers the button's position: it lives inside #messages, re-parented
to the bottom of the live "Working..." status bubble (not the thread
header) so it stays visually accessible right where the user is
already looking.
"""

from __future__ import annotations

import time


def test_stop_button_shown_while_running_and_hidden_after(logged_in_page, live_server):
    page = logged_in_page
    page.click("#new-session-btn")
    page.fill("#compose-text", "SLOW_TURN please think for a while")
    page.click("#compose-form button[type=submit]")

    # Once the turn starts, the session becomes "running" and the Stop
    # button should appear.
    page.wait_for_selector("#stop-turn-btn[style*='block']", timeout=5000)
    assert page.is_visible("#stop-turn-btn")

    page.click("#stop-turn-btn")

    # After interrupt, the fake handler records a system message and the
    # button should disappear once the SSE stream reports running=false.
    page.wait_for_selector("#stop-turn-btn", state="hidden", timeout=5000)
    time.sleep(0.2)
    assert "Interrupted" in page.inner_text("#messages")


def test_stop_button_hidden_for_a_session_with_no_running_turn(logged_in_page, live_server):
    page = logged_in_page
    page.click("#new-session-btn")
    page.fill("#compose-text", "hello, quick reply please")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(".session-item.active", timeout=5000)

    # A fast/instant reply never sets running=true long enough to show the
    # button in a meaningful window - assert it's not stuck visible.
    time.sleep(0.3)
    assert not page.is_visible("#stop-turn-btn")


def test_stop_button_lives_inside_the_live_status_bubble(logged_in_page, live_server):
    """While a turn is running, #stop-turn-btn must be a descendant of the
    live status message bubble inside #messages (near the "Working..."
    text the user is looking at), not a sibling of #thread-title/health
    strip controls up in the header."""
    page = logged_in_page
    page.click("#new-session-btn")
    page.fill("#compose-text", "SLOW_TURN please think for a while")
    page.click("#compose-form button[type=submit]")

    page.wait_for_selector("#stop-turn-btn[style*='block']", timeout=5000)
    # The button must be inside #messages (i.e. inside the live status
    # bubble), not inside .thread-header.
    in_messages = page.eval_on_selector(
        "#messages", "(el) => el.contains(document.getElementById('stop-turn-btn'))"
    )
    in_header = page.eval_on_selector(
        ".thread-header", "(el) => el.contains(document.getElementById('stop-turn-btn'))"
    )
    assert in_messages, "Stop button should be re-parented into #messages while running"
    assert not in_header, "Stop button should no longer live in the thread header"

    page.click("#stop-turn-btn")
    page.wait_for_selector("#stop-turn-btn", state="hidden", timeout=5000)
