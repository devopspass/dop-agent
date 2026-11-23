"""
Playwright test for the live "Working... Ns" elapsed-time
counter shown next to the Stop button while a turn is running (see
copilot_session.CopilotConversation.turn_started_at and
webui/app.js's startTurnElapsedTicker()/tickTurnElapsed()).
"""

from __future__ import annotations

import re
import time


def test_elapsed_counter_ticks_up_while_running_and_hides_after(logged_in_page, live_server):
    page = logged_in_page
    page.click("#new-session-btn")
    page.fill("#compose-text", "SLOW_TURN please think for a while")
    page.click("#compose-form button[type=submit]")

    page.wait_for_selector("#turn-elapsed[style*='inline']", timeout=5000)
    first_text = page.inner_text("#turn-elapsed")
    assert re.match(r"⏱ Working\.\.\. \d+s", first_text)

    # Let a couple of real seconds pass and confirm the counter actually
    # ticks upward client-side (not just rendered once).
    time.sleep(2.2)
    second_text = page.inner_text("#turn-elapsed")
    first_seconds = int(re.search(r"(\d+)s", first_text).group(1))
    second_seconds = int(re.search(r"(\d+)s", second_text).group(1))
    assert second_seconds > first_seconds

    page.click("#stop-turn-btn")
    page.wait_for_selector("#turn-elapsed", state="hidden", timeout=5000)


def test_elapsed_counter_hidden_for_a_session_with_no_running_turn(logged_in_page, live_server):
    page = logged_in_page
    page.click("#new-session-btn")
    page.fill("#compose-text", "hello, quick reply please")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(".session-item.active", timeout=5000)

    time.sleep(0.3)
    assert not page.is_visible("#turn-elapsed")
