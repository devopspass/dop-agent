"""
Playwright test for the "turn finished" browser Notification,
fired when a running turn transitions to finished while the tab is not
focused/visible (see ensureNotificationPermission()/notifyTurnFinished()
in webui/app.js).

Headless Chromium doesn't have a real focus/permission-prompt UI to drive,
so this stubs `window.Notification` (to capture calls instead of showing
real OS notifications) and `document.hasFocus`/`document.hidden` (to
simulate the tab being backgrounded) via an init script injected before
the page's own scripts run.
"""

from __future__ import annotations

_STUB_NOTIFICATION_SCRIPT = """
window.__notifications = [];
window.__notificationPermission = 'granted';
window.__tabFocused = true;
Object.defineProperty(document, 'hidden', { get: () => !window.__tabFocused } );
document.hasFocus = () => window.__tabFocused;
class FakeNotification {
  constructor(title, options) {
    this.title = title;
    this.options = options;
    window.__notifications.push({ title, options });
  }
  static get permission() { return window.__notificationPermission; }
  static requestPermission() { return Promise.resolve(window.__notificationPermission); }
  close() {}
}
window.Notification = FakeNotification;
"""


def test_notification_fires_when_turn_finishes_while_tab_unfocused(logged_in_page, live_server):
    page = logged_in_page
    page.context.add_init_script(_STUB_NOTIFICATION_SCRIPT)
    page.reload()
    page.wait_for_selector(".session-list", timeout=5000)

    page.click("#new-session-btn")
    page.fill("#compose-text", "SLOW_TURN please think for a while")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector("#turn-elapsed[style*='inline']", timeout=5000)

    # Simulate the tab losing focus/visibility while the turn is running.
    page.evaluate("window.__tabFocused = false")

    page.click("#stop-turn-btn")
    page.wait_for_selector("#turn-elapsed", state="hidden", timeout=5000)

    notifications = page.evaluate("window.__notifications")
    assert len(notifications) == 1
    assert notifications[0]["title"] == "Turn finished"


def test_no_notification_when_turn_finishes_while_tab_focused(logged_in_page, live_server):
    page = logged_in_page
    page.context.add_init_script(_STUB_NOTIFICATION_SCRIPT)
    page.reload()
    page.wait_for_selector(".session-list", timeout=5000)

    page.click("#new-session-btn")
    page.fill("#compose-text", "SLOW_TURN please think for a while")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector("#turn-elapsed[style*='inline']", timeout=5000)

    # Tab stays "focused" (window.__tabFocused left at its default true).
    page.click("#stop-turn-btn")
    page.wait_for_selector("#turn-elapsed", state="hidden", timeout=5000)

    notifications = page.evaluate("window.__notifications")
    assert notifications == []
