"""
Playwright test for the "Continue in new session" (web_server.py's
POST /api/sessions/{id}/continue + webui/app.js's continue-session-btn):
clicking it creates a brand-new session seeded with a system message
linking back to the one you came from, and switches you straight into it.
"""

from __future__ import annotations


def test_continue_in_new_session_seeds_backlink_and_switches(logged_in_page):
    page = logged_in_page

    page.click("#new-session-btn")
    page.fill("#compose-text", "original session message")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(".session-item.active", timeout=5000)
    old_id = page.eval_on_selector(".session-item.active", "el => el.dataset.conversationId")
    old_title = page.inner_text("#thread-title")

    # "Continue in new session" lives inside the thread-header's "⋯"
    # overflow menu - open it before the action button becomes clickable.
    page.click("#thread-overflow-btn")
    page.click("#continue-session-btn")

    # A new session becomes active, different from the original one.
    page.wait_for_function(
        "(oldId) => document.querySelector('.session-item.active')?.dataset.conversationId !== oldId",
        arg=old_id,
        timeout=5000,
    )
    new_id = page.eval_on_selector(".session-item.active", "el => el.dataset.conversationId")
    assert new_id != old_id

    # The new session's first (and only) message is the system backlink.
    page.wait_for_selector(".msg-row.role-system", timeout=5000)
    backlink_text = page.inner_text(".msg-row.role-system .bubble")
    assert "Continued from" in backlink_text
    assert old_title in backlink_text

    # Clicking the in-app backlink jumps back to the original session.
    page.click(".msg-row.role-system .bubble a")
    page.wait_for_function(
        "(oldId) => document.querySelector('.session-item.active')?.dataset.conversationId === oldId",
        arg=old_id,
        timeout=5000,
    )
