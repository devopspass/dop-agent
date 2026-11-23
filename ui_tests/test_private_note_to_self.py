"""
Playwright tests for the "private note-to-self" feature (📝
button in the thread header). See webui/index.html's #note-btn/#note-popover
and webui/app.js's openNotePopover()/loadNoteForCurrentSession()/
saveNoteForCurrentSession().

The note is a free-text scratchpad tied to the conversation row in
bot_store.db (message_store.get_note()/set_note()), autosaved on a short
debounce while typing and never rendered into the transcript or sent to the
agent - it's purely a client<->REST roundtrip against
/api/sessions/{id}/note, exercised at the unit level separately in
tests/test_web_server.py. These UI tests verify the frontend wiring: the
popover toggles, the textarea loads/saves the right thing, and switching
sessions doesn't leak one session's note into another's.
"""

from __future__ import annotations

import time


def _seed_session(page, text="seed message for note-to-self test"):
    page.click("#new-session-btn")
    page.fill("#compose-text", text)
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(".session-item.active", timeout=5000)
    time.sleep(0.2)


def test_note_button_toggles_the_popover(logged_in_page, live_server):
    page = logged_in_page
    _seed_session(page)

    assert page.eval_on_selector("#note-popover", "el => getComputedStyle(el).display") == "none"

    page.click("#note-btn")
    assert page.eval_on_selector("#note-popover", "el => getComputedStyle(el).display") != "none"

    # Must render near its trigger button, not merely have
    # display != 'none' somewhere off-screen (all three header popovers
    # share the same anchoring).
    btn_box = page.eval_on_selector("#note-btn", "el => el.getBoundingClientRect()")
    pop_box = page.eval_on_selector("#note-popover", "el => el.getBoundingClientRect()")
    assert abs(pop_box["top"] - btn_box["bottom"]) < 200, (
        f"popover rendered far from its button (button bottom={btn_box['bottom']}, "
        f"popover top={pop_box['top']}) - likely anchored to the wrong ancestor"
    )

    page.click("#note-btn")
    assert page.eval_on_selector("#note-popover", "el => getComputedStyle(el).display") == "none"


def test_typing_a_note_autosaves_and_persists_via_api(logged_in_page, live_server):
    import message_store

    page = logged_in_page
    _seed_session(page)
    conversation_id = page.evaluate("currentConversationId")

    page.click("#note-btn")
    page.wait_for_selector("#note-popover", state="visible", timeout=2000)
    page.fill("#note-textarea", "waiting on approval before merging")

    # Debounced autosave (800ms) - wait a bit longer, then check the
    # backend actually persisted it (not just that the textarea shows it).
    page.wait_for_function(
        "document.getElementById('note-save-status').textContent === 'Saved'",
        timeout=3000,
    )
    assert message_store.get_note(conversation_id) == "waiting on approval before merging"


def test_note_reloads_when_popover_reopened(logged_in_page, live_server):
    page = logged_in_page
    _seed_session(page)

    page.click("#note-btn")
    page.wait_for_selector("#note-popover", state="visible", timeout=2000)
    page.fill("#note-textarea", "remember this")
    page.wait_for_function(
        "document.getElementById('note-save-status').textContent === 'Saved'",
        timeout=3000,
    )
    page.click("#note-btn")  # close

    page.click("#note-btn")  # reopen
    page.wait_for_selector("#note-popover", state="visible", timeout=2000)
    assert page.input_value("#note-textarea") == "remember this"


def test_note_does_not_leak_between_sessions(logged_in_page, live_server):
    page = logged_in_page
    _seed_session(page, text="first session")
    page.click("#note-btn")
    page.wait_for_selector("#note-popover", state="visible", timeout=2000)
    page.fill("#note-textarea", "note for session one")
    page.wait_for_function(
        "document.getElementById('note-save-status').textContent === 'Saved'",
        timeout=3000,
    )

    _seed_session(page, text="second session")
    page.click("#note-btn")
    page.wait_for_selector("#note-popover", state="visible", timeout=2000)
    page.wait_for_function("document.getElementById('note-textarea').value === ''", timeout=2000)
    assert page.input_value("#note-textarea") == ""


def test_clicking_outside_the_note_popover_closes_it(logged_in_page, live_server):
    page = logged_in_page
    _seed_session(page)

    page.click("#note-btn")
    page.wait_for_selector("#note-popover", state="visible", timeout=2000)

    page.click("#thread-title")
    time.sleep(0.1)
    assert page.eval_on_selector("#note-popover", "el => getComputedStyle(el).display") == "none"


def test_note_is_never_included_in_outgoing_send_payload(logged_in_page, live_server):
    """The private note must never leak into the agent-facing conversation
    - confirm it's not appended to the outgoing /api/send request body when
    a note is set and the user sends a normal chat message."""
    page = logged_in_page
    _seed_session(page)

    page.click("#note-btn")
    page.wait_for_selector("#note-popover", state="visible", timeout=2000)
    page.fill("#note-textarea", "top secret scratchpad content")
    page.wait_for_function(
        "document.getElementById('note-save-status').textContent === 'Saved'",
        timeout=3000,
    )
    page.click("#note-btn")  # close popover before sending

    captured = []
    page.evaluate(
        """
        () => {
          window.__capturedSendBodies = [];
          const origFetch = window.fetch;
          window.fetch = function(url, options) {
            if (typeof url === 'string' && url.includes('/api/send')) {
              window.__capturedSendBodies.push(options && options.body);
            }
            return origFetch.apply(this, arguments);
          };
        }
        """
    )
    page.fill("#compose-text", "a normal chat message")
    page.click("#compose-form button[type=submit]")
    time.sleep(0.3)

    captured = page.evaluate("window.__capturedSendBodies")
    for body in captured:
        assert body is None or "top secret scratchpad content" not in body
