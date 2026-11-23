"""Switching to a session whose turn is running must show its transcript
immediately, not only after the next live update; and a send must not
steal the selection back after the user switched to another session."""

from __future__ import annotations

import time


def test_switching_back_to_running_session_shows_content(logged_in_page, live_server):
    page = logged_in_page
    page.click("#new-session-btn")
    page.fill("#compose-text", "first quick message")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector("#messages >> text=echo: first quick message", timeout=5000)
    page.wait_for_selector(".session-item.active", timeout=5000)
    a_id = page.eval_on_selector(".session-item.active", "el => el.dataset.conversationId")

    page.fill("#compose-text", "SLOW_TURN think")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector("#stop-turn-btn[style*='block']", timeout=5000)

    page.click("#new-session-btn")
    page.fill("#compose-text", "other session")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector("#messages >> text=echo: other session", timeout=5000)
    time.sleep(1)

    page.click(f".session-item[data-conversation-id='{a_id}']")
    page.wait_for_selector("#messages >> text=first quick message", timeout=1500)
    assert "SLOW_TURN think" in page.inner_text("#messages")
