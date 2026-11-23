"""
Playwright test for the "jump to first unread session" button
(🔵 in the sidebar header). See webui/app.js's jumpToFirstUnread() /
isSessionUnread().
"""

from __future__ import annotations

import time

import requests


def test_jump_unread_button_opens_the_first_unread_session(logged_in_page, live_server):
    page = logged_in_page

    # Session A: create and read it.
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

    a_selector = f".session-item[data-conversation-id='{session_a_id}']"

    # Bump A's activity while B is open, making A unread without opening it.
    cookie_jar = {c["name"]: c["value"] for c in page.context.cookies()}
    requests.post(
        f"{live_server}/api/send",
        json={"conversation_id": session_a_id, "text": "a second message into session A"},
        cookies=cookie_jar,
        timeout=10,
    )
    time.sleep(0.5)
    page.wait_for_selector(f"{a_selector} .session-unread-dot", timeout=5000)

    # Clicking the jump-to-unread button should open A (the unread one),
    # not stay on B.
    page.click("#jump-unread-btn")
    page.wait_for_selector(".session-item.active", timeout=5000)
    active_id = page.eval_on_selector(".session-item.active", "el => el.dataset.conversationId")
    assert active_id == session_a_id

    # Opening it should have cleared the unread dot too.
    assert page.query_selector(f"{a_selector} .session-unread-dot") is None


def test_jump_unread_button_gives_feedback_when_nothing_is_unread(logged_in_page, live_server):
    page = logged_in_page

    # Create a session and leave it open/read.
    page.click("#new-session-btn")
    page.fill("#compose-text", "only session, already read")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(".session-item.active", timeout=5000)
    time.sleep(0.2)

    active_before = page.eval_on_selector(".session-item.active", "el => el.dataset.conversationId")

    # The UI test DB is session-scoped (shared across the whole test run),
    # so other tests' sessions may still be genuinely unread here. Mark
    # every known session as read via markSessionRead() so this test can
    # reliably exercise the "nothing unread" branch regardless of what
    # other tests left behind.
    page.evaluate(
        "sessions.forEach(s => markSessionRead(s.conversation_id, s.updated_at))"
    )
    page.evaluate("renderSessionList()")

    # Clicking jump-to-unread with nothing unread should not navigate away
    # from the currently open session, and should apply the "no target"
    # shake-feedback CSS class briefly.
    page.click("#jump-unread-btn")
    page.wait_for_selector("#jump-unread-btn.jump-unread-none", timeout=2000)
    active_after = page.eval_on_selector(".session-item.active", "el => el.dataset.conversationId")
    assert active_after == active_before
