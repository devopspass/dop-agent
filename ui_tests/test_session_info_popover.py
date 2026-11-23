"""
Playwright tests for the session info popover (ℹ️ button in
the thread header). See webui/index.html's #session-info-btn /
#session-info-popover and webui/app.js's openSessionInfoPopover() /
renderSessionInfoPopover() / the popover's copy-button click handler.

The popover shows two things for the currently open session:
  - Copilot session_id (per-conversation, already tracked by the backend
    and returned in the sidebar's sessions[] list - no extra API call).
  - The bot's working directory (a single fixed COPILOT_WORKDIR constant
    for the whole bot, not per-session - see bot_config.py - surfaced via
    GET /api/health's "workdir" field).
"""

from __future__ import annotations

import time


def _seed_session(page):
    page.click("#new-session-btn")
    page.fill("#compose-text", "seed message for session info popover test")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(".session-item.active", timeout=5000)
    time.sleep(0.2)


def test_info_button_toggles_the_popover(logged_in_page, live_server):
    page = logged_in_page
    _seed_session(page)

    assert page.eval_on_selector(
        "#session-info-popover", "el => getComputedStyle(el).display"
    ) == "none"

    page.click("#session-info-btn")
    assert page.eval_on_selector(
        "#session-info-popover", "el => getComputedStyle(el).display"
    ) != "none"

    # The popover must render near its trigger button, not merely have
    # display != 'none' somewhere off-screen: Playwright's "visible" only
    # checks a non-empty bounding box and not display:none/visibility:hidden,
    # not on-screen proximity.
    btn_box = page.eval_on_selector(
        "#session-info-btn", "el => el.getBoundingClientRect()"
    )
    pop_box = page.eval_on_selector(
        "#session-info-popover", "el => el.getBoundingClientRect()"
    )
    assert abs(pop_box["top"] - btn_box["bottom"]) < 200, (
        f"popover rendered far from its button (button bottom={btn_box['bottom']}, "
        f"popover top={pop_box['top']}) - likely anchored to the wrong ancestor"
    )

    page.click("#session-info-btn")
    assert page.eval_on_selector(
        "#session-info-popover", "el => getComputedStyle(el).display"
    ) == "none"


def test_popover_shows_placeholder_when_no_session_id_assigned_yet(logged_in_page, live_server):
    """The Web UI test harness sends messages directly through the HTTP
    API (message_store), not through the real Slack-bot turn-taking code
    that assigns a Copilot session_id (see copilot_session.py) - so a
    freshly seeded conversation has no session_id yet. The popover should
    show a friendly placeholder rather than blank/undefined text."""
    page = logged_in_page
    _seed_session(page)

    page.click("#session-info-btn")
    page.wait_for_selector("#session-info-popover", state="visible", timeout=2000)

    assert page.inner_text("#session-info-id").strip() == "(not yet started)"
    workdir_text = page.inner_text("#session-info-workdir")
    assert workdir_text.strip() != ""
    assert workdir_text.strip() != "(unknown)"


def test_popover_shows_real_session_id_once_assigned(logged_in_page, live_server):
    import message_store

    page = logged_in_page
    _seed_session(page)
    conversation_id = page.evaluate("currentConversationId")
    message_store.set_copilot_session_id(conversation_id, "test-session-id-12345")

    # Force a sidebar refresh so the newly-set session_id is picked up
    # into the client-side sessions[] array before re-opening the popover.
    page.evaluate("refreshSessionList()")
    time.sleep(0.2)

    page.click("#session-info-btn")
    page.wait_for_selector("#session-info-popover", state="visible", timeout=2000)
    assert page.inner_text("#session-info-id").strip() == "test-session-id-12345"


def test_clicking_outside_the_popover_closes_it(logged_in_page, live_server):
    page = logged_in_page
    _seed_session(page)

    page.click("#session-info-btn")
    page.wait_for_selector("#session-info-popover", state="visible", timeout=2000)

    page.click("#thread-title")
    time.sleep(0.1)
    assert page.eval_on_selector(
        "#session-info-popover", "el => getComputedStyle(el).display"
    ) == "none"


def test_copy_button_copies_session_id_to_clipboard(logged_in_page, live_server):
    page = logged_in_page
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    _seed_session(page)

    page.click("#session-info-btn")
    page.wait_for_selector("#session-info-popover", state="visible", timeout=2000)

    expected = page.inner_text("#session-info-id")
    page.click("#session-info-popover .session-info-copy-btn[data-copy-target='session-info-id']")
    time.sleep(0.1)

    clipboard_text = page.evaluate("navigator.clipboard.readText()")
    assert clipboard_text == expected
