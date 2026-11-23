"""
Playwright tests for Web UI voice-transcription feature parity
with Slack:

1. Transcript action buttons (Auto-send/Preview/Send to agent) - rendered
   by renderMessages() from a message's `actions` field (message_store.py's
   `actions` column) and wired to POST
   /api/sessions/{id}/messages/{id}/transcript-action via
   triggerTranscriptAction() in app.js. They let the UI distinguish a voice
   message's "ready to send" and "already forwarded" states and toggle
   auto-send/preview mode like Slack's buttons.

2. The mic button itself (#mic-btn) exists in the compose form and is
   reachable/clickable - full MediaRecorder capture can't be exercised in
   this headless Playwright harness without a real/fake microphone device,
   so this only proves the button renders and is wired up, not the actual
   recording round-trip (covered by the Python unit tests in
   tests/test_handlers.py and tests/test_web_server.py instead).
"""

from __future__ import annotations

import time

import message_store


def _new_session(page):
    page.click("#new-session-btn")
    page.fill("#compose-text", "start the session")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(".session-item.active", timeout=5000)
    time.sleep(0.2)
    return page.evaluate("currentConversationId")


def test_transcript_action_button_renders_from_actions_field(logged_in_page, live_server):
    """A message with a non-empty `actions` array must render a real
    clickable button inside its bubble - not just plain text - proving the
    Web UI has genuine per-message button parity with Slack's block-kit
    buttons instead of a single global auto-send setting."""
    page = logged_in_page
    conversation_id = _new_session(page)

    message_store.record_message(
        conversation_id, "status", ":microphone: ready to send",
        actions=[{"id": "send_transcript_to_copilot", "label": ":arrow_right: Send to agent"}],
    )

    page.evaluate(f"openSession('{conversation_id}')")
    page.wait_for_selector(".msg-transcript-action-btn", timeout=5000)
    btn = page.query_selector(".msg-transcript-action-btn")
    assert btn is not None
    assert "Send to agent" in btn.text_content()


def test_clicking_transcript_action_button_posts_to_backend(logged_in_page, live_server):
    """Clicking the rendered button must call the
    /transcript-action endpoint (triggerTranscriptAction() in app.js), which
    delegates to handlers.web_transcript_action(). The test asserts only the
    button disabling itself immediately on click (the one UI-only side effect
    that doesn't depend on handlers.py's Whisper/Copilot-turn plumbing
    running in this harness)."""
    page = logged_in_page
    conversation_id = _new_session(page)
    message_store.record_message(
        conversation_id, "status", ":headphones: _Transcribing..._",
        actions=[{"id": "auto_send_transcript", "label": ":zap: Auto-send when ready"}],
    )

    page.evaluate(f"openSession('{conversation_id}')")
    page.wait_for_selector(".msg-transcript-action-btn", timeout=5000)
    btn = page.query_selector(".msg-transcript-action-btn")
    btn.click()
    page.wait_for_function(
        "() => document.querySelector('.msg-transcript-action-btn')?.disabled === true",
        timeout=3000,
    )


def test_message_without_actions_shows_no_transcript_buttons(logged_in_page, live_server):
    """A normal message (empty/absent `actions`) must never render a
    transcript button row - guards against the feature leaking onto
    unrelated messages."""
    page = logged_in_page
    conversation_id = _new_session(page)
    message_store.record_message(conversation_id, "assistant", "just a normal reply")

    page.evaluate(f"openSession('{conversation_id}')")
    page.wait_for_selector(".msg-row.role-assistant", timeout=5000)
    assert page.query_selector(".msg-transcript-action-btn") is None


def test_mic_button_exists_and_is_clickable_in_compose_form(logged_in_page, live_server):
    """The mic button must be present in the compose form alongside Send -
    full recording can't run headlessly without a real mic, but this proves
    the control is rendered and not hidden/broken."""
    page = logged_in_page
    _new_session(page)

    mic_btn = page.query_selector("#mic-btn")
    assert mic_btn is not None
    assert mic_btn.is_visible()
    assert mic_btn.get_attribute("type") == "button"


def test_mic_status_animated_mode_renders_moving_dots(logged_in_page, live_server):
    """setMicStatus(text, {animated: true}) renders a `.mic-status-dots`
    indicator (3 staggered-bounce spans) alongside the text, so the
    "Recording..."/"Uploading & transcribing..." status pill shows activity.
    Verifies that element renders, that the dots are structured for the
    staggered-animation CSS (3 child spans), and that plain (non-animated)
    calls are unaffected."""
    page = logged_in_page
    _new_session(page)

    page.evaluate("setMicStatus('Recording test status', { animated: true })")
    dots = page.query_selector("#mic-status .mic-status-dots")
    assert dots is not None
    assert page.eval_on_selector("#mic-status .mic-status-dots", "el => el.children.length") == 3
    assert "Recording test status" in page.inner_text("#mic-status")

    # Non-animated calls must keep rendering plain text with no dots markup.
    page.evaluate("setMicStatus('Plain status, no dots')")
    assert page.query_selector("#mic-status .mic-status-dots") is None
    assert page.inner_text("#mic-status").strip() == "Plain status, no dots"
