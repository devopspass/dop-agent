"""
Playwright tests for the "message timestamp shown on row hover"
feature. The full timestamp is a small `.msg-hover-time` button inside the
same row as the bookmark button (`.msg-bookmark-btn`), revealed together with
it on `.msg-row:hover` (no native title="" tooltip) - see
renderMessages() in app.js. The time element doubles as the "copy
deep link" control: clicking it copies the message's deep-link URL and appends
"✅ Copied" after the existing timestamp text (not replacing it), reverting
back to just the timestamp after ~1s.
"""

from __future__ import annotations

import time


def _seed_session_with_message(page, text):
    page.click("#new-session-btn")
    page.fill("#compose-text", text)
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(".session-item.active", timeout=5000)
    time.sleep(0.3)


def test_message_row_has_no_native_title_tooltip(logged_in_page, live_server):
    """The bubble itself must not carry a title="" tooltip for the
    timestamp."""
    page = logged_in_page
    _seed_session_with_message(page, "hello there")

    bubble = page.query_selector(".msg-row.role-user .bubble")
    assert bubble is not None
    assert (bubble.get_attribute("title") or "") == ""


def test_message_hover_time_appears_alongside_bookmark_button(logged_in_page, live_server):
    page = logged_in_page
    _seed_session_with_message(page, "hello there")

    row = page.query_selector(".msg-row.role-user")
    assert row is not None

    # Hidden until the row is hovered.
    time_el = row.query_selector(".msg-hover-time")
    assert time_el is not None
    assert float((time_el.evaluate("el => getComputedStyle(el).opacity"))) == 0

    row.hover()
    time.sleep(0.2)
    assert float((time_el.evaluate("el => getComputedStyle(el).opacity"))) > 0
    # It should render a real, non-empty formatted timestamp string.
    assert time_el.inner_text().strip() != ""
    # Lives in the same row as the other hover-reveal message action.
    assert row.query_selector(".msg-bookmark-btn") is not None


def test_clicking_hover_time_copies_deep_link_and_shows_checkmark(logged_in_page, live_server):
    """The time element itself is the copy-link control - no
    separate 🔗 button exists."""
    page = logged_in_page
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    _seed_session_with_message(page, "hello there")

    row = page.query_selector(".msg-row.role-user")
    assert row is not None
    assert row.query_selector(".msg-deep-link-btn") is None

    time_el = row.query_selector(".msg-hover-time")
    assert time_el is not None
    original_text = time_el.inner_text()
    time_el.click()
    time.sleep(0.1)

    clipboard_text = page.evaluate("navigator.clipboard.readText()")
    assert "#msg=" in clipboard_text
    conversation_id = page.evaluate("currentConversationId")
    assert conversation_id in clipboard_text
    assert "Copied" in time_el.inner_text()
    # The original timestamp must still be visible - only appended to, not
    # replaced.
    assert original_text in time_el.inner_text()

    time.sleep(1.1)
    assert time_el.inner_text() == original_text
