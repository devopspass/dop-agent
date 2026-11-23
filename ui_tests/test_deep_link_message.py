"""
Playwright tests for the "deep-link URL to a specific
message" feature. See webui/app.js's parseDeepLinkHash()/deepLinkUrlFor()/
pendingDeepLinkMessageId and the `.msg-hover-time` per-message-row control
rendered in renderMessages() - clicking the hover-revealed timestamp itself
copies the deep-link URL.

URL scheme: #msg=<conversation_id>:<message_id> - loading the app with
that hash present (instead of falling back to the "reopen last session"
behavior) opens the referenced conversation and scrolls to + briefly
highlights the referenced message row.
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


def test_deep_link_button_appears_on_hover_and_copies_a_url(logged_in_page, live_server):
    page = logged_in_page
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    _seed_session_with_messages(page, ["hello there"])

    row = page.query_selector(".msg-row.role-user")
    assert row is not None
    row.hover()
    time_el = row.query_selector(".msg-hover-time")
    assert time_el is not None
    time_el.click()
    time.sleep(0.1)

    clipboard_text = page.evaluate("navigator.clipboard.readText()")
    assert "#msg=" in clipboard_text
    conversation_id = page.evaluate("currentConversationId")
    assert conversation_id in clipboard_text


def test_visiting_a_deep_link_url_opens_the_right_session_and_scrolls_to_the_message(
    logged_in_page, live_server
):
    page = logged_in_page
    _seed_session_with_messages(page, ["first message", "second message", "third message"])
    conversation_id = page.evaluate("currentConversationId")

    rows = page.query_selector_all(".msg-row.role-user")
    assert len(rows) == 3
    target_row_id = rows[0].get_attribute("id")  # the "first message" row
    assert target_row_id is not None
    target_message_id = target_row_id[len("msg-"):]

    deep_link_url = f"{live_server}/#msg={conversation_id}:{target_message_id}"
    page.goto(deep_link_url)
    page.wait_for_selector(".session-item.active", timeout=5000)

    assert page.eval_on_selector(".session-item.active", "el => el.dataset.conversationId") == conversation_id
    page.wait_for_selector(f"#{target_row_id}.msg-row-deep-link-highlight", timeout=3000)


def test_deep_link_to_unknown_conversation_falls_back_gracefully(logged_in_page, live_server):
    page = logged_in_page
    _seed_session_with_messages(page, ["some message"])

    page.goto(f"{live_server}/#msg=does-not-exist:also-does-not-exist")
    page.wait_for_selector("#new-session-btn", timeout=5000)
    time.sleep(0.3)

    # No JS error (checked by the autouse _fail_on_console_errors fixture)
    # and the app didn't crash into a blank state - the empty state or
    # sidebar is still usable.
    assert page.is_visible("#new-session-btn")
