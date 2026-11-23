"""
Playwright test for the "archive/hide session" feature: each
sidebar session item gets an archive button (🗄️/📤) that toggles
message_store's `archived` flag via PUT /api/sessions/{id}/archive.
Archived sessions are hidden from the default sidebar view and only
reappear when the "Show archived sessions" checkbox is checked - this
keeps old/unneeded sessions out of the way without deleting their
history.
"""

from __future__ import annotations


def _create_session(page, text):
    page.click("#new-session-btn")
    page.fill("#compose-text", text)
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(f"#messages >> text=echo: {text}", timeout=5000)


def test_archiving_a_session_hides_it_from_default_sidebar_view(logged_in_page, live_server):
    page = logged_in_page
    _create_session(page, "session to archive")
    item = page.locator(".session-item.active")
    conversation_id = item.get_attribute("data-conversation-id")

    item.locator(".session-archive-btn").click()
    # The currently-open session stays visible while you're looking at it
    # (archiving shouldn't yank it out from under you) - switch to a new
    # session first to confirm it then disappears from the default view.
    _create_session(page, "a different active session")

    page.wait_for_selector(f".session-item[data-conversation-id='{conversation_id}']", state="detached", timeout=3000)
    assert page.locator(f".session-item[data-conversation-id='{conversation_id}']").count() == 0


def test_archived_filter_chip_reveals_archived_sessions_again(logged_in_page, live_server):
    page = logged_in_page
    _create_session(page, "another session to archive")
    item = page.locator(".session-item.active")
    conversation_id = item.get_attribute("data-conversation-id")

    item.locator(".session-archive-btn").click()
    _create_session(page, "a different active session 2")
    page.wait_for_selector(f".session-item[data-conversation-id='{conversation_id}']", state="detached", timeout=3000)

    page.click(".filter-chip[data-filter='archived']")

    revealed = page.locator(f".session-item[data-conversation-id='{conversation_id}']")
    page.wait_for_selector(f".session-item[data-conversation-id='{conversation_id}']", timeout=3000)
    assert revealed.count() == 1
    assert "archived" in (revealed.get_attribute("class") or "")


def test_unarchiving_a_session_restores_it_to_default_view(logged_in_page, live_server):
    page = logged_in_page
    _create_session(page, "session to archive then restore")
    item = page.locator(".session-item.active")
    conversation_id = item.get_attribute("data-conversation-id")

    item.locator(".session-archive-btn").click()
    _create_session(page, "a different active session 3")
    page.wait_for_selector(f".session-item[data-conversation-id='{conversation_id}']", state="detached", timeout=3000)

    page.click(".filter-chip[data-filter='archived']")
    page.wait_for_selector(f".session-item[data-conversation-id='{conversation_id}']", timeout=3000)
    page.locator(f".session-item[data-conversation-id='{conversation_id}'] .session-archive-btn").click()

    page.click(".filter-chip[data-filter='all']")

    page.wait_for_selector(f".session-item[data-conversation-id='{conversation_id}']", timeout=3000)
    assert page.locator(f".session-item[data-conversation-id='{conversation_id}']").count() == 1
