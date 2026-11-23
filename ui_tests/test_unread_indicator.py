"""
Playwright test for the per-session unread indicator (a small
dot next to a session's title in the sidebar when it has new activity you
haven't viewed yet). See webui/app.js's isSessionUnread / markSessionRead
and message_store.py's get_last_read/mark_read (server-side last_read_at).
"""

from __future__ import annotations

import time


def test_unread_dot_appears_after_new_activity_and_clears_on_open(logged_in_page, live_server):
    page = logged_in_page

    # Session A: create and read it (opening it marks it read).
    page.click("#new-session-btn")
    page.fill("#compose-text", "session A first message")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(".session-item.active", timeout=5000)
    time.sleep(0.2)
    session_a_id = page.eval_on_selector(".session-item.active", "el => el.dataset.conversationId")

    # Session B: create a second session so A is no longer "current".
    page.click("#new-session-btn")
    page.fill("#compose-text", "session B first message")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(".session-item.active", timeout=5000)
    time.sleep(0.2)

    # A is not currently open, and nothing new has happened in it since we
    # left - it should have no unread dot yet, since renderSessionList()
    # marks the just-viewed session read on every refresh.
    a_selector = f".session-item[data-conversation-id='{session_a_id}']"
    assert page.query_selector(f"{a_selector} .session-unread-dot") is None

    # Send a follow-up into A while B is open - the fake handler answers
    # immediately, bumping A's updated_at without the UI ever opening it.
    import requests

    cookie_jar = {c["name"]: c["value"] for c in page.context.cookies()}
    requests.post(
        f"{live_server}/api/send",
        json={"conversation_id": session_a_id, "text": "a second message into session A"},
        cookies=cookie_jar,
        timeout=10,
    )
    time.sleep(0.5)

    page.wait_for_selector(f"{a_selector} .session-unread-dot", timeout=5000)

    # Opening A should clear its unread dot.
    page.click(a_selector)
    page.wait_for_selector(".session-item.active", timeout=5000)
    time.sleep(0.2)
    assert page.query_selector(f"{a_selector} .session-unread-dot") is None
