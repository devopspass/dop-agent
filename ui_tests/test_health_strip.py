"""
Playwright test for the header health/status strip (GET
/api/health backed by web_server.py's slack_connected/mcp_configured/
uptime_seconds/bot_version fields).
"""

from __future__ import annotations


def test_health_strip_shows_connected_slack(logged_in_page, live_server):
    page = logged_in_page
    page.wait_for_function(
        "() => document.getElementById('health-uptime').textContent.includes('Uptime:')",
        timeout=5000,
    )

    # conftest's _FakeSlackClient.auth_test() returns {"ok": True}, so the
    # strip should show a healthy/connected Slack indicator.
    assert "health-ok" in page.eval_on_selector("#health-slack", "el => el.className")
    assert "connected" in page.get_attribute("#health-slack", "title").lower()

    version_text = page.inner_text("#health-version")
    assert version_text.startswith("v") and len(version_text) > 1
