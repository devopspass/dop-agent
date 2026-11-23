"""
Playwright tests for the auto-growing compose textarea. See
webui/app.js's autoGrowComposeText() and its call sites (input handler,
draft restore on session switch, clear-after-send, new-session reset).

Multi-line text is injected via page.fill() + a dispatched 'input' event
rather than typing real Enter keypresses - the compose textarea's keydown
handler treats plain Enter as "submit the message", so simulating a user
literally pressing Enter to add a newline would just send the form early
instead of growing the textarea. Shift+Enter is what actually inserts a
newline, but fill()+dispatch is simpler/more reliable here since this test
is about the resize behavior, not the newline-vs-submit key handling
(which already has its own coverage elsewhere).
"""

from __future__ import annotations

import time


def _compose_height(page):
    return page.eval_on_selector("#compose-text", "el => el.getBoundingClientRect().height")


def _set_multiline_draft(page, text):
    page.eval_on_selector(
        "#compose-text",
        "(el, value) => { el.value = value; el.dispatchEvent(new Event('input', { bubbles: true })); }",
        text,
    )


MULTILINE_TEXT = "line one\nline two\nline three\nline four\nline five"


def test_textarea_grows_as_multiline_text_is_typed(logged_in_page, live_server):
    page = logged_in_page

    page.click("#new-session-btn")
    initial_height = _compose_height(page)
    assert initial_height > 0

    _set_multiline_draft(page, MULTILINE_TEXT)
    time.sleep(0.1)

    grown_height = _compose_height(page)
    assert grown_height > initial_height


def test_textarea_shrinks_back_after_clearing_grown_text(logged_in_page, live_server):
    page = logged_in_page

    page.click("#new-session-btn")
    initial_height = _compose_height(page)
    _set_multiline_draft(page, MULTILINE_TEXT)
    time.sleep(0.1)
    assert _compose_height(page) > initial_height

    _set_multiline_draft(page, "")
    time.sleep(0.1)
    assert abs(_compose_height(page) - initial_height) < 2


def test_textarea_resets_height_after_sending_a_multiline_message(logged_in_page, live_server):
    page = logged_in_page

    page.click("#new-session-btn")
    initial_height = _compose_height(page)
    _set_multiline_draft(page, MULTILINE_TEXT)
    time.sleep(0.1)
    assert _compose_height(page) > initial_height

    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(".session-item.active", timeout=5000)
    time.sleep(0.2)

    assert abs(_compose_height(page) - initial_height) < 2


def test_textarea_restores_grown_height_when_switching_back_to_a_draft(logged_in_page, live_server):
    page = logged_in_page

    # Start a session and leave a multi-line draft (unsent) in it.
    page.click("#new-session-btn")
    page.fill("#compose-text", "seed message one")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(".session-item.active", timeout=5000)
    time.sleep(0.2)
    first_conversation_id = page.evaluate("currentConversationId")

    _set_multiline_draft(page, "draft line one\ndraft line two\ndraft line three\ndraft line four")
    time.sleep(0.1)
    grown_height = _compose_height(page)

    # Start a second session (its own empty draft), then switch back to the
    # first one - the grown draft text and height should both come back.
    page.click("#new-session-btn")
    page.fill("#compose-text", "seed message two")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(".session-item.active", timeout=5000)
    time.sleep(0.2)

    page.evaluate(f"openSession({first_conversation_id!r})")
    time.sleep(0.2)

    assert "draft line one" in page.input_value("#compose-text")
    assert abs(_compose_height(page) - grown_height) < 2
