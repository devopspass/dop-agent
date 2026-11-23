"""
Playwright tests for the "session link with context prefill"
feature: an "#open=<conversationId>&draft=<url-encoded text>" link opens the
referenced session AND merges that text into the compose box - e.g. a
bot-generated "click here to continue with this context" link.

See webui/app.js's parseSessionHash()/applyDraftPrefill()/
sessionLinkUrlFor() and web_server.py's _web_ui_url_for(draft_text=...).

Key semantics under test:
  - Plain "#open=<id>" (no &draft=) leaves the compose box untouched.
  - "&draft=<text>" on an EMPTY compose box just fills it with that text.
  - "&draft=<text>" on a compose box that already has unsent text APPENDS
    the new text below it (blank-line separated) - the existing draft is
    never replaced/lost.
  - Re-applying the exact same link a second time (same session, same
    text) must NOT duplicate the appended block - re-opening/re-clicking an
    unchanged link is idempotent.
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


def test_plain_open_link_without_draft_leaves_compose_box_untouched(logged_in_page, live_server):
    page = logged_in_page
    conversation_id = _seed_session_with_messages(page, ["hello there"])

    page.fill("#compose-text", "some typed draft")
    page.goto(f"{live_server}/#open={conversation_id}")
    page.wait_for_selector(".session-item.active", timeout=5000)

    assert page.input_value("#compose-text") == "some typed draft"


def test_draft_link_fills_an_empty_compose_box(logged_in_page, live_server):
    page = logged_in_page
    conversation_id = _seed_session_with_messages(page, ["hello there"])
    page.fill("#compose-text", "")

    context_text = "Following up on the PRJ-12345 discussion above"
    page.goto(f"{live_server}/#open={conversation_id}&draft={quote(context_text, safe='')}")
    page.wait_for_selector(".session-item.active", timeout=5000)

    assert page.input_value("#compose-text") == context_text


def test_draft_link_appends_below_an_existing_unsent_draft(logged_in_page, live_server):
    page = logged_in_page
    conversation_id = _seed_session_with_messages(page, ["hello there"])
    page.fill("#compose-text", "an already-typed unsent draft")
    time.sleep(0.1)  # let the input listener persist it to the drafts cache

    context_text = "extra context from a link"
    page.goto(f"{live_server}/#open={conversation_id}&draft={quote(context_text, safe='')}")
    page.wait_for_selector(".session-item.active", timeout=5000)

    value = page.input_value("#compose-text")
    assert value == f"an already-typed unsent draft\n\n{context_text}"


def test_reapplying_the_same_draft_link_does_not_duplicate_text(logged_in_page, live_server):
    page = logged_in_page
    conversation_id = _seed_session_with_messages(page, ["hello there"])
    page.fill("#compose-text", "")

    context_text = "context that should only appear once"
    url = f"{live_server}/#open={conversation_id}&draft={quote(context_text, safe='')}"
    page.goto(url)
    page.wait_for_selector(".session-item.active", timeout=5000)
    assert page.input_value("#compose-text") == context_text

    # Navigate away and back to the identical hash - simulates clicking the
    # exact same link a second time (or reloading on it).
    page.goto(f"{live_server}/#open={conversation_id}")
    page.wait_for_selector(".session-item.active", timeout=5000)
    page.goto(url)
    page.wait_for_selector(".session-item.active", timeout=5000)

    value = page.input_value("#compose-text")
    assert value == context_text
    assert value.count(context_text) == 1


def test_draft_link_focuses_the_compose_box(logged_in_page, live_server):
    page = logged_in_page
    conversation_id = _seed_session_with_messages(page, ["hello there"])
    page.fill("#compose-text", "")

    context_text = "focus me"
    page.goto(f"{live_server}/#open={conversation_id}&draft={quote(context_text, safe='')}")
    page.wait_for_selector(".session-item.active", timeout=5000)

    assert page.evaluate("document.activeElement && document.activeElement.id") == "compose-text"
