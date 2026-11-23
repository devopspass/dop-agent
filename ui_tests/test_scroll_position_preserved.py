"""
Playwright test for scroll position being preserved during live
updates while the user has scrolled up to read earlier history.

`renderMessages()` in webui/app.js captures whether the user was "near the
bottom" (within SCROLL_STICK_TO_BOTTOM_PX) *before* the DOM is rebuilt, and
only re-pins to the bottom if so; SSE-driven re-renders (a running turn, a
passive watch-fired notice) therefore never yank a user who scrolled up.
Sending a new message from the compose box always jumps to the bottom
(there's no optimistic local echo, so the jump is done explicitly in
sendMessageText() ahead of the next render).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

_BOT_DIR = Path(__file__).resolve().parent.parent
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))

import message_store  # noqa: E402


# Mimics a user scroll: the pinned-to-bottom tracking only reacts to scrolls
# that follow real wheel/touch/key input.
USER_SCROLL = "const m = document.getElementById('messages'); m.dispatchEvent(new WheelEvent('wheel')); m.scrollTop = {}"


def _fill_transcript(page, n=25, start=0):
    """Send enough short messages to make #messages taller than the
    viewport, so there's something to scroll through."""
    for i in range(start, start + n):
        page.fill("#compose-text", f"filler message number {i}")
        page.click("#compose-form button[type=submit]")
        page.wait_for_selector(f"#messages >> text=echo: filler message number {i}", timeout=5000)


def test_scroll_position_preserved_during_passive_live_update(logged_in_page, live_server):
    page = logged_in_page
    page.click("#new-session-btn")
    # A new session has no conversation_id (and no sidebar entry) until the
    # first message is actually sent - only then does the server create it.
    _fill_transcript(page, 1)
    page.wait_for_selector(".session-item.active", timeout=5000)
    conversation_id = page.eval_on_selector(".session-item.active", "el => el.dataset.conversationId")

    _fill_transcript(page, 24, start=1)

    # Scroll up, away from the bottom, to simulate reading earlier history.
    page.evaluate(USER_SCROLL.format("0"))
    time.sleep(0.1)
    scroll_before = page.eval_on_selector("#messages", "el => el.scrollTop")
    assert scroll_before < 50, "test setup: should actually be scrolled near the top"

    # Simulate a passive update arriving while the user is reading history
    # (e.g. a watch-fired notice injected into the thread) - this must
    # trigger a live re-render via SSE without looking at the compose box at
    # all.
    message_store.record_message(conversation_id, "system", "a watch fired while you were scrolled up")
    page.wait_for_selector("#messages >> text=a watch fired while you were scrolled up", timeout=5000)
    time.sleep(0.2)

    scroll_after = page.eval_on_selector("#messages", "el => el.scrollTop")
    assert scroll_after < 50, (
        f"scrollTop jumped from {scroll_before} to {scroll_after} after a passive "
        "live update - the user's reading position should be preserved"
    )


def test_sending_a_message_scrolls_to_bottom_even_if_scrolled_up(logged_in_page, live_server):
    page = logged_in_page
    page.click("#new-session-btn")
    _fill_transcript(page, 25)
    page.wait_for_selector(".session-item.active", timeout=5000)
    page.evaluate(USER_SCROLL.format("0"))
    time.sleep(0.1)
    assert page.eval_on_selector("#messages", "el => el.scrollTop") < 50

    page.fill("#compose-text", "deliberate send while scrolled up")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector("#messages >> text=echo: deliberate send while scrolled up", timeout=5000)
    time.sleep(0.2)

    distance_from_bottom = page.eval_on_selector(
        "#messages", "el => el.scrollHeight - el.scrollTop - el.clientHeight"
    )
    assert distance_from_bottom <= 48, (
        "sending a message should scroll the transcript to the bottom even if "
        f"the user had scrolled up (distance from bottom: {distance_from_bottom})"
    )


def test_mid_thread_scroll_position_survives_live_rerender_of_long_thread(logged_in_page, live_server):
    """With a long thread (tall bubbles, tables) every live re-render
    (status heartbeat, tool-call update, new message) must keep scrollTop: a
    user reading mid-thread (more than 48px from the bottom, not at the very
    top) must not be thrown back to the start. renderMessages() saves
    scrollTop before the wipe and restores it when the user was not stuck to
    the bottom."""
    page = logged_in_page
    page.click("#new-session-btn")
    _fill_transcript(page, 1)
    page.wait_for_selector(".session-item.active", timeout=5000)
    conversation_id = page.eval_on_selector(".session-item.active", "el => el.dataset.conversationId")
    _fill_transcript(page, 24, start=1)

    page.evaluate(USER_SCROLL.format("Math.floor((m.scrollHeight - m.clientHeight) / 2)"))
    time.sleep(0.1)
    before = page.eval_on_selector("#messages", "el => el.scrollTop")
    assert before > 200, "test setup: should be scrolled to the middle of the thread"

    message_store.record_message(conversation_id, "system", "a watch fired mid-thread")
    page.wait_for_selector("#messages >> text=a watch fired mid-thread", timeout=5000)
    time.sleep(0.2)

    after = page.eval_on_selector("#messages", "el => el.scrollTop")
    assert abs(after - before) < 20, f"scrollTop moved from {before} to {after} on a passive live update"
