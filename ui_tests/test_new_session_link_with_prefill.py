"""
Playwright tests for the "#new" session link, with an optional
context-prefill draft: "#new[&draft=<url-encoded text>]" - the same idea as
"#open=<conversationId>&draft=<text>" (see test_session_link_with_prefill.py)
but for starting a brand-new session (no conversation_id exists yet)
instead of opening an existing one - equivalent to clicking the "New
Session" button.

See webui/app.js's parseSessionHash()/startNewSession()/
newSessionLinkUrlFor() and web_server.py's _web_ui_new_session_url().

Key semantics under test (mirroring the #open= draft-link tests):
  - Plain "#new" (no &draft=) puts the app into the same "new session"
    state as clicking the button - empty compose box.
  - "#new&draft=<text>" fills the (new session's) compose box with that
    text.
  - If there's already unsent text in the new-session compose slot, the
    link's text is APPENDED below it (blank-line separated), never
    replacing it.
  - Re-applying the exact same "#new&draft=<text>" link a second time does
    not duplicate the appended block.
  - The compose box ends up focused.
"""

from __future__ import annotations

import time
from urllib.parse import quote


def _seed_session_with_messages(page, texts):
    page.click("#new-session-btn")
    for text in texts:
        page.fill("#compose-text", text)
        page.click("#compose-form button[type=submit]")
        page.wait_for_selector(".session-item.active", timeout=5000)
        time.sleep(0.2)
    return page.eval_on_selector(".session-item.active", "el => el.dataset.conversationId")


def test_plain_new_link_puts_app_in_new_session_state(logged_in_page, live_server):
    page = logged_in_page
    # Start from a real, existing session so we can confirm "#new" actually
    # switches away from it (not a no-op / already-empty-state coincidence).
    _seed_session_with_messages(page, ["hello there"])

    page.goto(f"{live_server}/#new")
    page.wait_for_selector("#thread-view", state="visible", timeout=5000)

    assert page.evaluate("currentConversationId") is None
    assert page.input_value("#compose-text") == ""
    assert page.text_content("#thread-title") == "(new session)"


def test_new_link_with_draft_fills_the_new_session_compose_box(logged_in_page, live_server):
    page = logged_in_page
    _seed_session_with_messages(page, ["hello there"])

    context_text = "Starting fresh with some context"
    page.goto(f"{live_server}/#new&draft={quote(context_text, safe='')}")
    page.wait_for_selector("#thread-view", state="visible", timeout=5000)

    assert page.evaluate("currentConversationId") is None
    assert page.input_value("#compose-text") == context_text


def test_new_link_appends_below_an_existing_unsent_new_session_draft(logged_in_page, live_server):
    page = logged_in_page
    page.click("#new-session-btn")
    page.fill("#compose-text", "an already-typed unsent draft")
    time.sleep(0.1)  # let the input listener persist it to the drafts cache

    context_text = "extra context from a link"
    page.goto(f"{live_server}/#new&draft={quote(context_text, safe='')}")
    page.wait_for_selector("#thread-view", state="visible", timeout=5000)

    value = page.input_value("#compose-text")
    assert value == f"an already-typed unsent draft\n\n{context_text}"


def test_reapplying_the_same_new_link_does_not_duplicate_text(logged_in_page, live_server):
    page = logged_in_page
    page.click("#new-session-btn")
    page.fill("#compose-text", "")

    context_text = "context that should only appear once"
    url = f"{live_server}/#new&draft={quote(context_text, safe='')}"
    page.goto(url)
    page.wait_for_selector("#thread-view", state="visible", timeout=5000)
    assert page.input_value("#compose-text") == context_text

    # Navigate away and back to the identical hash - simulates clicking the
    # exact same link a second time (or reloading on it).
    page.goto(f"{live_server}/#new")
    page.wait_for_selector("#thread-view", state="visible", timeout=5000)
    page.goto(url)
    page.wait_for_selector("#thread-view", state="visible", timeout=5000)

    value = page.input_value("#compose-text")
    assert value == context_text
    assert value.count(context_text) == 1


def test_new_link_focuses_the_compose_box(logged_in_page, live_server):
    page = logged_in_page
    page.click("#new-session-btn")

    context_text = "focus me"
    page.goto(f"{live_server}/#new&draft={quote(context_text, safe='')}")
    page.wait_for_selector("#thread-view", state="visible", timeout=5000)

    assert page.evaluate("document.activeElement && document.activeElement.id") == "compose-text"


def test_existing_session_draft_is_preserved_when_switching_to_new_via_link(logged_in_page, live_server):
    """Clicking/visiting a "#new" link while a real session has an unsent
    draft must not lose that draft - the same guarantee the New Session
    button provides."""
    page = logged_in_page
    conversation_id = _seed_session_with_messages(page, ["hello there"])
    page.fill("#compose-text", "do not lose this")
    time.sleep(0.1)

    page.goto(f"{live_server}/#new")
    page.wait_for_selector("#thread-view", state="visible", timeout=5000)
    assert page.evaluate("currentConversationId") is None

    page.goto(f"{live_server}/#open={conversation_id}")
    page.wait_for_selector(".session-item.active", timeout=5000)
    assert page.input_value("#compose-text") == "do not lose this"
