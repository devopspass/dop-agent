"""
Playwright tests for the "warn before closing tab with
unsent draft" feature. See the `beforeunload` listener in app.js
right before the stop-turn-btn click handler.

Real native `beforeunload` confirmation dialogs cannot be driven through
Playwright (browsers intentionally suppress/auto-dismiss them for
automation), so instead we dispatch a synthetic 'beforeunload' Event in
the page and assert whether app.js's listener calls preventDefault() /
sets returnValue, which is the actual code path we own and want to lock
in behavior for.
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


def test_no_warning_when_compose_box_is_empty(logged_in_page, live_server):
    page = logged_in_page
    _seed_session_with_messages(page, ["hello there"])

    page.fill("#compose-text", "")
    result = page.evaluate(
        "() => {"
        "const ev = new Event('beforeunload', {cancelable: true});"
        "window.dispatchEvent(ev);"
        "return {defaultPrevented: ev.defaultPrevented, returnValue: ev.returnValue};"
        "}"
    )
    assert result["defaultPrevented"] is False


def test_warns_when_compose_box_has_unsent_draft(logged_in_page, live_server):
    page = logged_in_page
    _seed_session_with_messages(page, ["hello there"])

    page.fill("#compose-text", "this message was never sent")
    result = page.evaluate(
        "() => {"
        "const ev = new Event('beforeunload', {cancelable: true});"
        "window.dispatchEvent(ev);"
        "return {defaultPrevented: ev.defaultPrevented};"
        "}"
    )
    assert result["defaultPrevented"] is True


def test_no_warning_after_draft_is_sent(logged_in_page, live_server):
    page = logged_in_page
    _seed_session_with_messages(page, ["hello there"])

    page.fill("#compose-text", "draft to be sent")
    page.click("#compose-form button[type=submit]")
    page.wait_for_timeout(300)

    result = page.evaluate(
        "() => {"
        "const ev = new Event('beforeunload', {cancelable: true});"
        "window.dispatchEvent(ev);"
        "return {defaultPrevented: ev.defaultPrevented};"
        "}"
    )
    assert result["defaultPrevented"] is False


def test_no_warning_when_only_whitespace_in_compose_box(logged_in_page, live_server):
    page = logged_in_page
    _seed_session_with_messages(page, ["hello there"])

    page.fill("#compose-text", "   \n   ")
    result = page.evaluate(
        "() => {"
        "const ev = new Event('beforeunload', {cancelable: true});"
        "window.dispatchEvent(ev);"
        "return {defaultPrevented: ev.defaultPrevented};"
        "}"
    )
    assert result["defaultPrevented"] is False
