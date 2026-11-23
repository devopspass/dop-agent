"""
Test that a failed transcript action (network error, 400, 500) leaves the
clicked button usable: triggerTranscriptAction() in app.js disables the
button before awaiting the fetch, and since no SSE update would arrive to
re-render it on failure, it returns a success boolean and re-enables the
button (with a retry hint) when it's false.
"""

from __future__ import annotations

import time


def test_failed_transcript_action_reenables_button_for_retry(
    logged_in_page, live_server, _fail_on_console_errors
):
    page = logged_in_page
    page.click("#new-session-btn")
    page.fill("#compose-text", "start the session")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(".session-item.active", timeout=5000)
    time.sleep(0.2)
    conversation_id = page.evaluate("currentConversationId")

    import message_store
    message_store.record_message(
        conversation_id, "status", ":microphone: ready to send",
        actions=[{"id": "bogus_unknown_action", "label": ":arrow_right: Send to agent"}],
    )
    page.evaluate(f"openSession('{conversation_id}')")
    page.wait_for_selector(".msg-transcript-action-btn", timeout=5000)

    btn = page.query_selector(".msg-transcript-action-btn")
    btn.click()
    # Backend rejects the bogus action_id with a 400 - button must come
    # back enabled instead of staying stuck disabled forever.
    page.wait_for_function(
        "() => document.querySelector('.msg-transcript-action-btn')?.disabled === false",
        timeout=3000,
    )
    # This test deliberately triggers a 400 (bogus action_id) to exercise
    # the failure path - clear the expected console.error entries so the
    # autouse _fail_on_console_errors fixture only fails on *unexpected*
    # errors, not this test's own intentional one.
    _fail_on_console_errors.clear()
