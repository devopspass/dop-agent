"""
Playwright test for the sidebar "errored" flag on a session
whose last turn failed/timed out (see message_store.list_conversations()'s
"last_role" field, web_server._session_payload()'s "errored" computation,
and the session-error-flag rendering in webui/app.js's renderSessionList()).
"""

from __future__ import annotations


def test_session_list_flags_session_after_failed_turn(logged_in_page, live_server):
    page = logged_in_page
    page.click("#new-session-btn")
    page.fill("#compose-text", "FAIL_TURN please break")
    page.click("#compose-form button[type=submit]")

    page.wait_for_selector(".retry-last-message-btn", timeout=5000)
    page.wait_for_selector(".session-item.active.errored-session", timeout=5000)
    assert page.locator(".session-item.active .session-error-flag").count() == 1


def test_session_list_does_not_flag_a_successful_session(logged_in_page, live_server):
    page = logged_in_page
    page.click("#new-session-btn")
    page.fill("#compose-text", "hello there")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector("text=echo: hello there", timeout=5000)

    # live_server is session-scoped and shared across ui_tests, so check
    # only the just-created (active) session's own class list rather than
    # asserting no errored session exists anywhere in the sidebar.
    active_class = page.get_attribute(".session-item.active", "class")
    assert "errored-session" not in active_class
