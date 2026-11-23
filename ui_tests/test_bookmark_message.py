"""
Playwright tests for the "bookmark/star a specific message
within a thread" feature. See webui/app.js's toggleMessageBookmark()/
renderBookmarksList()/openBookmarksPopover(), the ☆/⭐ per-message-row
button rendered in renderMessages(), and the header ⭐ button that opens the
bookmarks popover listing every starred message in the current session.

Uses the same message-id-based row targeting as the deep-link feature: clicking a bookmarks-list entry sets pendingDeepLinkMessageId
and re-renders, scrolling to + highlighting that row.
"""

from __future__ import annotations

import time


def _seed_session_with_messages(page, texts):
    page.click("#new-session-btn")
    for text in texts:
        page.fill("#compose-text", text)
        page.click("#compose-form button[type=submit]")
        page.wait_for_selector(".session-item.active", timeout=5000)
        time.sleep(0.2)


def test_star_button_toggles_bookmark_state(logged_in_page, live_server):
    page = logged_in_page
    _seed_session_with_messages(page, ["hello there"])

    row = page.query_selector(".msg-row.role-user")
    assert row is not None
    star_btn = row.query_selector(".msg-bookmark-btn")
    assert star_btn is not None
    assert star_btn.text_content() == "☆"

    star_btn.click()
    page.wait_for_selector(".msg-bookmark-btn-active", timeout=3000)
    star_btn = page.query_selector(".msg-row.role-user .msg-bookmark-btn")
    assert star_btn.text_content() == "⭐"

    star_btn.click()
    page.wait_for_function(
        "() => !document.querySelector('.msg-bookmark-btn-active')", timeout=3000
    )
    star_btn = page.query_selector(".msg-row.role-user .msg-bookmark-btn")
    assert star_btn.text_content() == "☆"


def test_bookmark_persists_across_reload(logged_in_page, live_server):
    page = logged_in_page
    _seed_session_with_messages(page, ["remember this one"])

    row = page.query_selector(".msg-row.role-user")
    row.query_selector(".msg-bookmark-btn").click()
    page.wait_for_selector(".msg-bookmark-btn-active", timeout=3000)
    conversation_id = page.evaluate("currentConversationId")

    page.reload()
    page.wait_for_selector(".session-item.active", timeout=5000)
    page.evaluate(f"openSession('{conversation_id}')")
    page.wait_for_selector(".msg-bookmark-btn-active", timeout=3000)


def test_bookmarks_popover_lists_starred_messages_and_jumps_to_them(logged_in_page, live_server):
    page = logged_in_page
    _seed_session_with_messages(page, ["first message", "second message", "third message"])

    rows = page.query_selector_all(".msg-row.role-user")
    assert len(rows) == 3
    target_row_id = rows[0].get_attribute("id")  # "first message"
    rows[0].query_selector(".msg-bookmark-btn").click()
    page.wait_for_selector(".msg-bookmark-btn-active", timeout=3000)

    page.click("#bookmarks-btn")
    page.wait_for_selector("#bookmarks-popover .bookmarks-item", timeout=3000)
    items = page.query_selector_all("#bookmarks-popover .bookmarks-item")
    assert len(items) == 1
    assert "first message" in items[0].text_content()

    # Must render near its trigger button, not merely be non-hidden somewhere off-screen.
    btn_box = page.eval_on_selector("#bookmarks-btn", "el => el.getBoundingClientRect()")
    pop_box = page.eval_on_selector("#bookmarks-popover", "el => el.getBoundingClientRect()")
    assert abs(pop_box["top"] - btn_box["bottom"]) < 200, (
        f"popover rendered far from its button (button bottom={btn_box['bottom']}, "
        f"popover top={pop_box['top']}) - likely anchored to the wrong ancestor"
    )

    items[0].click()
    page.wait_for_selector(f"#{target_row_id}.msg-row-deep-link-highlight", timeout=3000)
    # Clicking an entry closes the popover.
    assert page.is_hidden("#bookmarks-popover")


def test_bookmarks_popover_shows_empty_state_with_no_bookmarks(logged_in_page, live_server):
    page = logged_in_page
    _seed_session_with_messages(page, ["nothing starred here"])

    page.click("#bookmarks-btn")
    page.wait_for_selector("#bookmarks-popover .bookmarks-empty", timeout=3000)


def test_bookmark_does_not_leak_across_sessions(logged_in_page, live_server):
    page = logged_in_page
    _seed_session_with_messages(page, ["session one message"])
    row = page.query_selector(".msg-row.role-user")
    row.query_selector(".msg-bookmark-btn").click()
    page.wait_for_selector(".msg-bookmark-btn-active", timeout=3000)

    _seed_session_with_messages(page, ["session two message"])
    # Freshly opened session's own message must not appear pre-starred.
    star_btn = page.query_selector(".msg-row.role-user .msg-bookmark-btn")
    assert star_btn.text_content() == "☆"
    page.click("#bookmarks-btn")
    page.wait_for_selector("#bookmarks-popover .bookmarks-empty", timeout=3000)
