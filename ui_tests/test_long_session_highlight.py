"""
Playwright test for the long-session highlight (sessions past
LONG_SESSION_TURN_THRESHOLD turns get a visual accent in the sidebar) -
see webui/app.js's isLongSession()/LONG_SESSION_TURN_THRESHOLD and
webui/style.css's .long-session/.session-turn-count-long rules.

Generating 100+ real turns would make this test slow, so it exercises the
pure client-side threshold logic directly (isLongSession is a global
function since app.js is a plain, non-module script) and confirms the
resulting DOM/CSS hook renders as expected for a real session once its
turn_count crosses the threshold.
"""

from __future__ import annotations


def test_is_long_session_threshold_logic(logged_in_page):
    page = logged_in_page
    threshold = page.evaluate("LONG_SESSION_TURN_THRESHOLD")
    assert threshold == 100

    assert page.evaluate("isLongSession({turn_count: 101})") is True
    assert page.evaluate("isLongSession({turn_count: 100})") is False
    assert page.evaluate("isLongSession({turn_count: 1})") is False
    assert page.evaluate("isLongSession({})") is False
    assert page.evaluate("isLongSession(null)") is False


def test_long_session_gets_highlight_class_and_badge(logged_in_page, live_server):
    page = logged_in_page
    page.click("#new-session-btn")
    page.fill("#compose-text", "hello")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(".session-item.active", timeout=5000)

    # Not long yet with just one turn.
    assert "long-session" not in page.get_attribute(".session-item.active", "class")

    # Force this session's cached turn_count over the threshold and
    # re-render, the same way a real long-running thread would look once
    # the backend reports turn_count > 100.
    page.evaluate(
        """() => {
            sessions[0].turn_count = 150;
            renderSessionList();
        }"""
    )
    item_class = page.get_attribute(".session-item.active", "class")
    assert "long-session" in item_class
    assert page.query_selector(".session-item.active .session-turn-count-long") is not None
