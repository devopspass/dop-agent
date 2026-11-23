"""
Playwright test for the header health strip's "Live" item,
which reflects the browser's live EventSource connection(s) to the bot
(/api/stream/sessions + /api/stream/sessions/{id}) - see setSseConnected()/
connectSessionListStream()/connectConversationStream() in webui/app.js.
"""

from __future__ import annotations


def test_health_strip_shows_live_once_sse_connects(logged_in_page, live_server):
    page = logged_in_page
    # The session-list stream connects immediately on page load (see
    # init() in app.js), independent of any session being open.
    page.wait_for_function(
        "() => document.getElementById('health-live').classList.contains('health-ok')",
        timeout=5000,
    )
    assert "Live" in page.inner_text("#health-live")
    assert "connected" in page.get_attribute("#health-live", "title").lower()


def test_health_strip_still_live_after_opening_a_session(logged_in_page, live_server):
    page = logged_in_page
    page.wait_for_function(
        "() => document.getElementById('health-live').classList.contains('health-ok')",
        timeout=5000,
    )
    page.click("#new-session-btn")
    page.fill("#compose-text", "hello there")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector("#messages >> text=echo: hello there", timeout=5000)

    # Opening a conversation adds a second EventSource (the per-conversation
    # stream); the combined "Live" indicator must still read connected.
    assert "health-ok" in page.eval_on_selector("#health-live", "el => el.className")
