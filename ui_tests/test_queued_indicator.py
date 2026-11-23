"""
Playwright test for the "queued / interrupting current turn"
badge shown when the user submits a new message while a turn is already
running for the open session (see the lastRunning tracking + queuedBadgeEl
wiring in webui/app.js's sendMessageText()).
"""

from __future__ import annotations


def test_queued_badge_shows_while_sending_during_active_turn(logged_in_page, live_server):
    page = logged_in_page
    page.click("#new-session-btn")
    page.fill("#compose-text", "SLOW_TURN please think for a while")
    page.click("#compose-form button[type=submit]")

    # Wait until the turn is actually running (Stop button visible) before
    # sending the interrupting follow-up, so lastRunning is true client-side.
    page.wait_for_selector("#stop-turn-btn:visible", timeout=5000)

    page.fill("#compose-text", "please interrupt and answer this instead")
    page.click("#compose-form button[type=submit]")

    page.wait_for_selector("#queued-badge:visible", timeout=5000)
    assert "queued" in page.inner_text("#queued-badge").lower()

    # Once the backend's next state update lands, the badge should clear.
    page.wait_for_selector("#queued-badge", state="hidden", timeout=5000)


def test_queued_badge_does_not_show_for_a_normal_send(logged_in_page, live_server):
    page = logged_in_page
    page.click("#new-session-btn")
    page.fill("#compose-text", "hello there")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector("text=echo: hello there", timeout=5000)

    assert not page.is_visible("#queued-badge")
