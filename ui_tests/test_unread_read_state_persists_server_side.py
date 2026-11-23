"""
Test that session read/unread state is tracked server-side
(message_store.mark_read()/last_read_at), not in browser localStorage.

A brand-new browser profile/context hitting the same backend sees a session
as read if it was read from a different browser/device. The proof
here is a completely fresh Playwright browser *context* (its own cookie
jar and localStorage, sharing nothing with the context that marked a
session read) that still sees the session as read, because the state
lives in the backend DB and is served via GET /api/sessions.
"""

from __future__ import annotations

import time


def test_fresh_browser_context_sees_read_state_set_by_another_context(
    logged_in_page, live_server, ui_test_password
):
    tab1 = logged_in_page

    tab1.click("#new-session-btn")
    tab1.fill("#compose-text", "hello from context A")
    tab1.click("#compose-form button[type=submit]")
    tab1.wait_for_selector(".session-item.active", timeout=5000)
    time.sleep(0.2)
    session_id = tab1.eval_on_selector(".session-item.active", "el => el.dataset.conversationId")

    # Opening the session in tab1 marks it read there via PUT /api/sessions/{id}/read.
    tab1.wait_for_timeout(300)

    # A brand-new browser context: separate cookie jar, separate
    # localStorage, no relationship at all to tab1's context.
    fresh_context = tab1.context.browser.new_context()
    fresh_page = fresh_context.new_page()
    fresh_page.goto(f"{live_server}/login")
    fresh_page.fill("#password", ui_test_password)
    fresh_page.click("button[type=submit]")
    fresh_page.wait_for_url(f"{live_server}/")
    fresh_page.wait_for_selector(".session-list .session-item", timeout=5000)

    selector = f".session-item[data-conversation-id='{session_id}']"
    fresh_page.wait_for_selector(selector, timeout=5000)

    # If read-state were localStorage-based, this fresh context would
    # have no record of it being read and would show an unread dot.
    assert fresh_page.query_selector(f"{selector} .session-unread-dot") is None

    fresh_context.close()
