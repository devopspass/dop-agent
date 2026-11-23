"""
Playwright tests for the "Settings screen: quiet-hours
toggle for notifications" feature. See the #settings-link/#settings-overlay
markup in index.html and the settings/quiet-hours logic in app.js
(loadSettings/saveSettings/isWithinQuietHours).

Settings are stored in localStorage (like drafts), not the backend, so
these tests interact purely with the DOM + localStorage - no seeded
session/message content is required for most of them.
"""

from __future__ import annotations


def test_settings_button_opens_and_closes_overlay(logged_in_page, live_server):
    page = logged_in_page

    assert page.is_visible("#settings-link")
    assert not page.is_visible("#settings-overlay")

    page.click("#settings-link")
    assert page.is_visible("#settings-overlay")
    assert page.is_visible("#settings-quiet-hours-enabled")

    page.click("#settings-close-btn")
    assert not page.is_visible("#settings-overlay")


def test_settings_overlay_closes_on_escape(logged_in_page, live_server):
    page = logged_in_page

    page.click("#settings-link")
    assert page.is_visible("#settings-overlay")

    page.keyboard.press("Escape")
    assert not page.is_visible("#settings-overlay")


def test_settings_overlay_closes_on_backdrop_click(logged_in_page, live_server):
    page = logged_in_page

    page.click("#settings-link")
    assert page.is_visible("#settings-overlay")

    # Click the overlay backdrop itself (top-left corner, outside the panel).
    page.click("#settings-overlay", position={"x": 5, "y": 5})
    assert not page.is_visible("#settings-overlay")


def test_quiet_hours_toggle_persists_across_reload(logged_in_page, live_server):
    page = logged_in_page

    page.click("#settings-link")
    page.check("#settings-quiet-hours-enabled")
    page.fill("#settings-quiet-hours-start", "23:00")
    page.fill("#settings-quiet-hours-end", "07:00")
    # Blur to fire the 'change' events that persist to localStorage.
    page.keyboard.press("Tab")
    page.click("#settings-close-btn")

    page.reload()
    page.click("#settings-link")
    assert page.is_checked("#settings-quiet-hours-enabled")
    assert page.input_value("#settings-quiet-hours-start") == "23:00"
    assert page.input_value("#settings-quiet-hours-end") == "07:00"


def test_is_within_quiet_hours_wrapping_window(logged_in_page, live_server):
    page = logged_in_page

    # Window wraps midnight: 22:00 -> 08:00. Simulate "now" at 23:30 and 12:00.
    result_inside = page.evaluate(
        """() => {
            settings = { quietHoursEnabled: true, quietHoursStart: '22:00', quietHoursEnd: '08:00' };
            const realDate = Date;
            class FakeDate extends realDate {
              constructor() { super(2024, 0, 1, 23, 30); }
            }
            window.Date = FakeDate;
            const inside = isWithinQuietHours();
            window.Date = realDate;
            return inside;
        }"""
    )
    result_outside = page.evaluate(
        """() => {
            settings = { quietHoursEnabled: true, quietHoursStart: '22:00', quietHoursEnd: '08:00' };
            const realDate = Date;
            class FakeDate extends realDate {
              constructor() { super(2024, 0, 1, 12, 0); }
            }
            window.Date = FakeDate;
            const inside = isWithinQuietHours();
            window.Date = realDate;
            return inside;
        }"""
    )

    assert result_inside is True
    assert result_outside is False


def test_is_within_quiet_hours_disabled_returns_false(logged_in_page, live_server):
    page = logged_in_page

    result = page.evaluate(
        """() => {
            settings = { quietHoursEnabled: false, quietHoursStart: '22:00', quietHoursEnd: '08:00' };
            return isWithinQuietHours();
        }"""
    )
    assert result is False
