"""
Test that Slack's angle-bracket link syntax for
non-http(s) schemes (e.g. phone numbers: ``<tel:+37062558384|+37062558384>``)
renders as a clickable link in the Web UI instead of raw text.

slackLinksToMarkdown() in app.js matches any URI scheme inside Slack's
``<url|label>`` syntax, so a ``tel:`` link doesn't show up verbatim
(including the ``<...|...>`` wrapper) in the rendered message bubble.
"""

from __future__ import annotations

import time

TEL_LINK_MESSAGE = "call me at <tel:+37062558384|+37062558384>"


def _seed_session_with_message(page, text):
    page.click("#new-session-btn")
    page.fill("#compose-text", text)
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(".session-item.active", timeout=5000)
    time.sleep(0.3)


def test_tel_link_renders_as_clickable_link(logged_in_page, live_server):
    page = logged_in_page
    _seed_session_with_message(page, TEL_LINK_MESSAGE)

    link = page.locator(".bubble.assistant a[href='tel:+37062558384']")
    assert link.count() == 1
    assert link.inner_text() == "+37062558384"
    # The raw Slack angle-bracket syntax must not leak into the rendered text.
    assert "<tel:" not in page.locator(".bubble.assistant").last.inner_text()


def test_bare_tel_pipe_syntax_without_angle_brackets_also_renders_as_link(logged_in_page, live_server):
    """Covers angle brackets being stripped upstream
    before reaching the Web UI (mirrors the existing bare-https? fallback
    regex already in slackLinksToMarkdown())."""
    page = logged_in_page
    _seed_session_with_message(page, "call me at tel:+37062558384|+37062558384")

    link = page.locator(".bubble.assistant a[href='tel:+37062558384']")
    assert link.count() == 1
    assert link.inner_text() == "+37062558384"
