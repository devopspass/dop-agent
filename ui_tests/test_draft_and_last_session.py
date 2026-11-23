"""
Playwright tests for restoring the last-viewed session and its unsent draft
after a page reload (draft is kept in localStorage; the Web UI reopens the
last-viewed session on load so it is shown again).

See dop-agent/webui/app.js: LAST_SESSION_STORAGE_KEY / saveLastSession /
loadLastSession.
"""

from __future__ import annotations

import time


def _create_session_via_send(page, live_server, text: str) -> str:
    page.click("#new-session-btn")
    page.fill("#compose-text", text)
    page.click("#compose-form button[type=submit]")
    # Wait until the new session becomes selected (URL/state stays the same
    # page, but the sidebar re-renders with an "active" item once the send
    # completes and a conversation_id comes back).
    page.wait_for_selector(".session-item.active", timeout=5000)
    return page.eval_on_selector(".session-item.active", "el => el.dataset.conversationId")


def test_last_selected_session_is_restored_after_reload(logged_in_page, live_server):
    page = logged_in_page
    conversation_id = _create_session_via_send(page, live_server, "hello from playwright")
    # Give the fake handler a beat to record the assistant reply.
    time.sleep(0.2)

    page.reload()
    page.wait_for_selector(".session-item.active", timeout=5000)
    active_id = page.eval_on_selector(".session-item.active", "el => el.dataset.conversationId")
    assert active_id == conversation_id


def test_draft_survives_reload_because_last_session_is_restored(logged_in_page, live_server):
    page = logged_in_page
    conversation_id = _create_session_via_send(page, live_server, "first message")
    time.sleep(0.2)

    draft_text = "an unsent draft that should not disappear"
    page.fill("#compose-text", draft_text)
    # Give localStorage a moment (input listener fires synchronously, but be
    # generous since this crosses a real browser event loop).
    time.sleep(0.1)

    page.reload()
    page.wait_for_selector(".session-item.active", timeout=5000)
    assert page.eval_on_selector(".session-item.active", "el => el.dataset.conversationId") == conversation_id
    assert page.input_value("#compose-text") == draft_text


def test_new_session_choice_is_not_sticky_across_reload(logged_in_page, live_server):
    """Clicking "new session" (no conversation yet) is not remembered across a
    reload: with no last real session, the empty state is shown."""
    page = logged_in_page
    page.click("#new-session-btn")
    page.reload()
    # No prior real session exists, so the empty state is expected.
    assert page.is_visible("#empty-state")
