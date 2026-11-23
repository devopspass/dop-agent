"""
Playwright test for cross-tab consistency of the unread-session state.
Read/unread state is tracked server-side (see message_store.mark_read())
and pushed to every open tab via the /api/stream/sessions SSE feed, so
marking a session read in one tab clears its unread dot in every other
open tab, without a page reload and without localStorage.
"""

from __future__ import annotations

import time

import requests


def test_marking_session_read_in_one_tab_clears_unread_in_another(logged_in_page, live_server):
    tab1 = logged_in_page

    # Session A: create then immediately leave (create B) so A is left
    # "read but not currently open".
    tab1.click("#new-session-btn")
    tab1.fill("#compose-text", "session A first message")
    tab1.click("#compose-form button[type=submit]")
    tab1.wait_for_selector(".session-item.active", timeout=5000)
    time.sleep(0.2)
    session_a_id = tab1.eval_on_selector(".session-item.active", "el => el.dataset.conversationId")

    tab1.click("#new-session-btn")
    tab1.fill("#compose-text", "session B first message")
    tab1.click("#compose-form button[type=submit]")
    tab1.wait_for_selector(".session-item.active", timeout=5000)
    time.sleep(0.2)

    # A second tab in the same browser context/backend - both tabs get
    # their own SSE session-list subscription, which is how a mark-read in
    # one tab reaches the other (no localStorage involved).
    tab2 = tab1.context.new_page()
    tab2.goto(f"{live_server}/")
    tab2.wait_for_selector(".session-list .session-item", timeout=5000)

    a_selector = f".session-item[data-conversation-id='{session_a_id}']"

    # Bump session A's activity from outside the UI - both tabs' own SSE
    # session-list streams will independently push this update.
    cookie_jar = {c["name"]: c["value"] for c in tab1.context.cookies()}
    requests.post(
        f"{live_server}/api/send",
        json={"conversation_id": session_a_id, "text": "a second message into session A"},
        cookies=cookie_jar,
        timeout=10,
    )

    tab1.wait_for_selector(f"{a_selector} .session-unread-dot", timeout=5000)
    tab2.wait_for_selector(f"{a_selector} .session-unread-dot", timeout=5000)

    # Mark A read in tab1 only, by opening it there.
    tab1.click(a_selector)
    tab1.wait_for_selector(".session-item.active", timeout=5000)
    time.sleep(0.2)
    assert tab1.query_selector(f"{a_selector} .session-unread-dot") is None

    # tab2 never opened A - the unread dot there must clear purely from the
    # server-side last_read_at update flowing back through tab2's own SSE
    # session-list subscription (PUT /api/sessions/{id}/read -> DB write ->
    # _notify_change() -> broadcast to all subscribers, tab2 included).
    tab2.wait_for_selector(f"{a_selector}:not(:has(.session-unread-dot))", timeout=5000)
    assert tab2.query_selector(f"{a_selector} .session-unread-dot") is None

    tab2.close()

