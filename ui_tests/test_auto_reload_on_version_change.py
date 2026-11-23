"""
Test for auto-reloading an already-open tab once the running bot process
restarts with a new version.

Cache-busting the /static/app.js URL (see web_server.py's index_page(),
?v=<mtime>) only helps new page loads; a tab opened before a bot restart
keeps running the JS it already loaded. refreshHealth() in app.js compares
the git-commit `bot_version` field from each /api/health poll against the
value seen when the tab first loaded; a change triggers `location.reload()`
so an open tab self-heals within one poll interval.

The live_server never restarts mid-test, so this test intercepts
/api/health and returns a different bot_version on the second poll.
"""

from __future__ import annotations

import time


def test_tab_auto_reloads_when_bot_version_changes(logged_in_page, live_server):
    page = logged_in_page

    poll_count = {"n": 0}

    def handler(route):
        poll_count["n"] += 1
        version = "aaaaaaa" if poll_count["n"] == 1 else "bbbbbbb"
        route.fulfill(
            status=200,
            content_type="application/json",
            body=(
                '{"uptime_seconds": 5, "bot_version": "%s", "slack_connected": true, '
                '"mcp_servers": [], "mcp_configured": false, '
                '"session_timeout_seconds": 1800, "workdir": "/tmp"}' % version
            ),
        )

    page.route("**/api/health*", handler)

    # Force a fresh navigation so the route above governs every /api/health
    # call from the very first poll (the initial one sets the "baseline"
    # version the tab considers current).
    page.reload()
    page.wait_for_function(
        "() => document.getElementById('health-version').textContent.length > 1",
        timeout=5000,
    )
    assert poll_count["n"] >= 1

    navigations = []
    page.on("framenavigated", lambda frame: navigations.append(frame.url) if frame == page.main_frame else None)

    # Trigger the second poll (simulating the interval firing after a bot
    # restart changed bot_version) directly, rather than waiting the full
    # 30s HEALTH_POLL_INTERVAL_MS in real time.
    page.evaluate("refreshHealth()")
    page.wait_for_timeout(500)

    assert len(navigations) >= 1, "page should have reloaded once bot_version changed"
