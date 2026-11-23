"""
Playwright test for the one-click "Retry last message" button
that appears after a failed/timed-out turn (see renderMessages()'s
ERROR_RESPONSE_PREFIXES check and the retryLastMessage() handler in
webui/app.js).
"""

from __future__ import annotations


def test_retry_button_appears_after_failed_turn_and_resends_text(logged_in_page, live_server):
    page = logged_in_page

    page.click("#new-session-btn")
    page.fill("#compose-text", "FAIL_TURN please break")
    page.click("#compose-form button[type=submit]")

    # conftest's _fake_handle_fn responds instantly to "FAIL_TURN" with the
    # same wording handlers.py uses for a real timeout - the retry button
    # should show up once that assistant reply is rendered.
    page.wait_for_selector(".retry-last-message-btn", timeout=5000)

    # Clicking it should resend the exact preceding user message text as a
    # brand-new /api/send call - which, since it still contains FAIL_TURN,
    # produces another failure and the retry button re-appears (proving the
    # resend actually happened, not just a no-op click).
    page.click(".retry-last-message-btn")
    page.wait_for_function(
        "() => document.querySelectorAll('.bubble.user').length === 2",
        timeout=5000,
    )
    user_bubbles = page.locator(".bubble.user")
    assert user_bubbles.nth(0).inner_text() == user_bubbles.nth(1).inner_text()
    page.wait_for_selector(".retry-last-message-btn", timeout=5000)


def test_retry_button_absent_after_successful_turn(logged_in_page, live_server):
    page = logged_in_page

    page.click("#new-session-btn")
    page.fill("#compose-text", "hello there")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector("text=echo: hello there", timeout=5000)

    assert page.locator(".retry-last-message-btn").count() == 0
