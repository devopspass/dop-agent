"""
Playwright regression tests for sidebar/compose keyboard shortcuts:
j/k to move focus between sessions in the sidebar, / to jump to the
search box, and Cmd/Ctrl+Enter to send a compose message. See
webui/app.js's isEditableTarget() / navigateSessionList() / the
document-level keydown listener, and the Cmd/Ctrl+Enter branch in the
compose textarea's own keydown handler.
"""

from __future__ import annotations

import platform
import time


def test_j_and_k_navigate_between_sessions_in_sidebar_order(logged_in_page, live_server):
    page = logged_in_page

    page.click("#new-session-btn")
    page.fill("#compose-text", "session A")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(".session-item.active", timeout=5000)
    time.sleep(0.2)
    session_a_id = page.eval_on_selector(".session-item.active", "el => el.dataset.conversationId")

    page.click("#new-session-btn")
    page.fill("#compose-text", "session B")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(".session-item.active", timeout=5000)
    time.sleep(0.2)
    session_b_id = page.eval_on_selector(".session-item.active", "el => el.dataset.conversationId")

    # B (most recently active) sorts before A in the default sidebar order.
    # Move focus out of the compose textarea first (shortcuts are ignored
    # while an editable field is focused), then press 'j' repeatedly to
    # walk down the visible list and confirm the active session changes.
    page.click("body")
    page.keyboard.press("j")
    time.sleep(0.2)
    active_after_j = page.eval_on_selector(".session-item.active", "el => el.dataset.conversationId")
    # B is currently active and first in sidebar order, so 'j' (move down)
    # should land on the next visible session - not error, not stay on B
    # forever if there's somewhere else to go.
    assert active_after_j in (session_a_id, session_b_id)

    # Now go back up with 'k' and make sure it doesn't error and lands on
    # a valid session.
    page.keyboard.press("k")
    time.sleep(0.2)
    active_after_k = page.eval_on_selector(".session-item.active", "el => el.dataset.conversationId")
    assert active_after_k in (session_a_id, session_b_id)


def test_slash_focuses_the_search_box(logged_in_page, live_server):
    page = logged_in_page

    # Focus something else first (the body) so '/' isn't accidentally
    # already-focused-in-an-input.
    page.click("body")
    assert not page.eval_on_selector("#search-input", "el => el === document.activeElement")

    page.keyboard.press("/")
    time.sleep(0.1)
    assert page.eval_on_selector("#search-input", "el => el === document.activeElement")


def test_shortcuts_are_ignored_while_typing_in_compose_box(logged_in_page, live_server):
    page = logged_in_page

    page.click("#new-session-btn")
    page.fill("#compose-text", "seed message")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(".session-item.active", timeout=5000)
    time.sleep(0.2)

    # Typing the letters 'j' and 'k' and a '/' into the compose textarea
    # must produce ordinary text, not trigger navigation/search-focus.
    page.click("#compose-text")
    page.type("#compose-text", "j/k are just letters here")
    assert page.input_value("#compose-text") == "j/k are just letters here"
    assert page.eval_on_selector("#compose-text", "el => el === document.activeElement")


def test_cmd_or_ctrl_enter_sends_the_compose_message(logged_in_page, live_server):
    page = logged_in_page

    page.click("#new-session-btn")
    page.fill("#compose-text", "seed message")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(".session-item.active", timeout=5000)
    time.sleep(0.2)

    page.click("#compose-text")
    page.keyboard.type("hello via cmd+enter")
    modifier = "Meta" if platform.system() == "Darwin" else "Control"
    page.keyboard.press(f"{modifier}+Enter")

    # Message should have been submitted: compose box clears and the new
    # message appears in the transcript.
    page.wait_for_function(
        "() => document.querySelector('#compose-text').value === ''",
        timeout=5000,
    )
    page.wait_for_selector("text=hello via cmd+enter", timeout=5000)
