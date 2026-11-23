"""
Playwright tests for auto-unfurling links that point back at one
of THIS bot's own Web UI sessions (a #open=<id> or #msg=<id>:<msgId> hash on
the current origin) - e.g. session_create's returned web_url, a "continue in
new session" backlink, or a bookmark pasted into a message.

Two things are verified here:
  1. Such links must NOT open in a new browser tab/window (unlike ordinary
     external links, which still get target="_blank") - clicking one should
     navigate in-app to that session instead.
  2. The link should get a small "own_session" unfurl card on hover (see
     renderUnfurlCard() in app.js), showing the target session's title and
     running/idle status - resolved entirely client-side from the already-
     loaded `sessions` array (no /api/unfurl round trip, unlike the Jira/
     GitLab card types covered by test_jira_gitlab_unfurl.py).

Deliberately origin-scoped (see parseOwnSessionLink() in app.js): a link is
only recognized as "one of ours" if it resolves to the same origin the page
is currently being viewed from, not some separately-configured allowlist of
hostnames - this Web UI can be reached via more than one hostname (localhost,
a reverse proxy, a tailnet name, ...) and there's no reliable way to
enumerate all of them up front.
"""

from __future__ import annotations

import time


def _seed_session_with_message(page, text):
    page.click("#new-session-btn")
    page.fill("#compose-text", text)
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(".session-item.active", timeout=5000)
    time.sleep(0.3)
    return page.evaluate("window.currentConversationId ?? currentConversationId")


def test_own_session_link_does_not_open_in_new_tab(logged_in_page, live_server):
    page = logged_in_page

    # Create a first session (the link target) and grab its conversation id.
    target_id = _seed_session_with_message(page, "hello from session A")

    # Create a second session whose message contains a link back to the
    # first one, mimicking a pasted session_create web_url / backlink.
    own_link_url = f"{live_server}/#open={target_id}"
    _seed_session_with_message(page, f"see {own_link_url} for the other session")

    link = page.locator(f".bubble.assistant a[href='{own_link_url}']").last
    link.wait_for(state="visible", timeout=2000)
    # The key assertion: unlike ordinary external links, this one must NOT
    # have target="_blank" set (see renderMessages()'s anchor-processing
    # loop in app.js skipping parseOwnSessionLink() matches).
    assert link.get_attribute("target") != "_blank"


def test_clicking_own_session_link_navigates_in_app_without_new_tab(logged_in_page, live_server):
    page = logged_in_page

    target_id = _seed_session_with_message(page, "hello from session A")
    own_link_url = f"{live_server}/#open={target_id}"
    _seed_session_with_message(page, f"see {own_link_url} for the other session")

    pages_before = len(page.context.pages)

    link = page.locator(f".bubble.assistant a[href='{own_link_url}']").last
    link.click()

    # Must navigate in the SAME tab/page - no new browser tab/window opened.
    assert len(page.context.pages) == pages_before

    # And it must actually land on the target session (hash-based in-app
    # routing via the delegated click handler / applyDeepLinkFromHash()).
    page.wait_for_function(
        "id => currentConversationId === id",
        arg=target_id,
        timeout=2000,
    )
    assert "hello from session A" in page.locator(".bubble.user").first.inner_text()


def test_own_session_link_gets_an_unfurl_card_on_hover(logged_in_page, live_server):
    page = logged_in_page

    target_id = _seed_session_with_message(page, "hello from session A")
    own_link_url = f"{live_server}/#open={target_id}"
    _seed_session_with_message(page, f"see {own_link_url} for the other session")

    link = page.locator(f".bubble.assistant a[href='{own_link_url}']").last
    link.hover()

    card = page.locator(".unfurl-card.unfurl-own_session").last
    card.wait_for(state="visible", timeout=2000)
    text = card.inner_text()
    # Session titles default to (a plain-text render of) the first message
    # sent, so the target session's title should mention its seed message.
    assert "hello from session A" in text
    # Idle (no turn running) -> the idle status/emoji should show, not the
    # "running" one.
    assert "idle" in text

    # Must follow the same two-row layout as every other unfurl card type
    # (see test_jira_gitlab_unfurl.py): row1 = icon + short key + status
    # (quick glance, no need to read the title), row2 = the actual title
    # text (can be long, wraps independently).
    row1 = card.locator(".unfurl-row1")
    row2 = card.locator(".unfurl-row2")
    assert row2.count() == 1, "own_session card is missing its row2 (title) line"
    assert "hello from session A" in row2.inner_text()
    assert "hello from session A" not in row1.inner_text()
    assert "idle" in row1.inner_text()


def test_own_session_link_card_shows_running_status(logged_in_page, live_server):
    page = logged_in_page

    # SLOW_TURN keeps the fake backend's turn "running" (lock held) until
    # interrupted or a few seconds pass - see conftest.py's _fake_handle_fn.
    target_id = _seed_session_with_message(page, "SLOW_TURN please wait")
    own_link_url = f"{live_server}/#open={target_id}"
    _seed_session_with_message(page, f"see {own_link_url} for the busy session")

    link = page.locator(f".bubble.assistant a[href='{own_link_url}']").last
    link.hover()

    card = page.locator(".unfurl-card.unfurl-own_session").last
    card.wait_for(state="visible", timeout=2000)
    assert "running" in card.inner_text()
