"""Playwright tests for the dark/light/system theme (webui/theme.js + light.css)."""

from __future__ import annotations

import pytest


def _theme(page):
    return page.evaluate("document.documentElement.getAttribute('data-theme')")


def _bg(page):
    return page.evaluate("getComputedStyle(document.body).backgroundColor")


def test_toggle_cycles_dark_light_system_and_persists(logged_in_page):
    page = logged_in_page
    assert _theme(page) == "dark"  # existing users stay on dark
    dark_bg = _bg(page)
    btn = page.locator("#theme-toggle")

    btn.click()
    assert _theme(page) == "light"
    page.wait_for_function(f"getComputedStyle(document.body).backgroundColor !== '{dark_bg}'")  # colour transition
    assert page.evaluate("document.querySelector('meta[name=color-scheme]').content") == "light"
    assert "github.min.css" in page.evaluate("document.querySelector('link[href*=\"highlight.js\"]').href")
    assert "Switch to system" in btn.get_attribute("aria-label")

    page.reload()
    page.wait_for_selector("#theme-toggle")
    assert _theme(page) == "light"

    btn.click()  # -> system
    assert page.evaluate("localStorage.getItem('dop-theme')") == "system"
    btn.click()  # -> dark
    assert _theme(page) == "dark"
    page.wait_for_function(f"getComputedStyle(document.body).backgroundColor === '{dark_bg}'")


@pytest.mark.parametrize("scheme,expected", [("light", "light"), ("dark", "dark")])
def test_system_mode_follows_os_and_reacts_live(logged_in_page, scheme, expected):
    page = logged_in_page
    page.evaluate("localStorage.setItem('dop-theme','system')")
    page.emulate_media(color_scheme=scheme)
    page.reload()
    page.wait_for_selector("#theme-toggle")
    assert _theme(page) == expected
    other = "dark" if expected == "light" else "light"
    page.emulate_media(color_scheme=other)
    page.wait_for_function(f"document.documentElement.getAttribute('data-theme') === '{other}'")


def test_theme_applied_before_paint_and_settings_selector(logged_in_page):
    page = logged_in_page
    page.evaluate("localStorage.setItem('dop-theme','light')")
    page.reload(wait_until="commit")
    # theme.js is a blocking <head> script, so the attribute exists before DOMContentLoaded
    page.wait_for_function("document.documentElement.getAttribute('data-theme') === 'light'", timeout=3000)
    page.wait_for_selector("#theme-toggle")
    page.click("#settings-link")
    sel = page.locator("#settings-theme")
    assert sel.input_value() == "light"
    sel.select_option("dark")
    assert _theme(page) == "dark"
    assert page.evaluate("localStorage.getItem('dop-theme')") == "dark"


def test_theme_syncs_across_tabs(logged_in_page):
    page = logged_in_page
    other = page.context.new_page()
    other.goto(page.url)
    other.wait_for_selector("#theme-toggle")
    page.click("#theme-toggle")
    other.wait_for_function("document.documentElement.getAttribute('data-theme') === 'light'")
    other.close()


def test_chart_and_mermaid_hooks_rerender_on_theme_change(logged_in_page):
    page = logged_in_page
    events = page.evaluate(
        "() => new Promise(r => { const got=[]; window.addEventListener('dop-theme-change', e => got.push(e.detail.theme));"
        " dopTheme.set('light'); dopTheme.set('dark'); r(got); })"
    )
    assert events == ["light", "dark"]
