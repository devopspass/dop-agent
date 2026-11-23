"""
Playwright tests for the thread-header turn-count/idle-time
readout. See webui/index.html's #thread-meta span and webui/app.js's
updateThreadMeta() (called from updateThreadHeader() and a 30s ticker).
"""

from __future__ import annotations

import time


def _seed_session(page):
    page.click("#new-session-btn")
    page.fill("#compose-text", "seed message for thread meta test")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(".session-item.active", timeout=5000)
    time.sleep(0.2)


def test_thread_meta_shows_turn_count_after_seeding_a_message(logged_in_page, live_server):
    page = logged_in_page
    _seed_session(page)

    text = page.inner_text("#thread-meta")
    assert "turn" in text
    # One user message was sent (the echo-fake backend also appends an
    # assistant reply, but turn_count only counts role='user' messages -
    # see message_store.list_conversations()), so this should read "1 turn".
    assert "1 turn" in text


def test_thread_meta_shows_idle_time_with_last_activity_tooltip(logged_in_page, live_server):
    page = logged_in_page
    _seed_session(page)

    text = page.inner_text("#thread-meta")
    assert "idle" in text

    title = page.eval_on_selector("#thread-meta", "el => el.title")
    assert "Last activity" in title


def test_thread_meta_is_empty_when_no_session_is_open(logged_in_page, live_server):
    page = logged_in_page
    _seed_session(page)
    assert page.inner_text("#thread-meta").strip() != ""

    # Clicking "new session" clears currentConversationId back to null
    # without submitting anything (still shows the compose view, but with
    # no active session) - #thread-meta should go blank along with it.
    page.click("#new-session-btn")
    time.sleep(0.1)
    assert page.inner_text("#thread-meta").strip() == ""
