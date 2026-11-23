"""
Feature: deep links into the settings overlay.

`#settings=<tab>` opens Settings on that tab (integrations, browser, agent,
transcription, adapters, mcp, watchers, unfurl, advanced) and
`#settings[=<tab>]&q=<text>` prefills the settings search box. Works on fresh
load and on in-page hash changes (like the #open=/#msg= session links).
"""

from __future__ import annotations


def _active_tab(page):
    return page.eval_on_selector("#settings-tabs .sx-tab.active", "el => el.dataset.stab")


def test_settings_hash_opens_overlay_on_requested_tab(logged_in_page, live_server):
    page = logged_in_page
    page.goto(f"{live_server}/#settings=watchers")
    page.wait_for_selector("#settings-overlay", state="visible")
    page.wait_for_function("document.querySelector('#settings-tabs .sx-tab.active')?.dataset.stab === 'watchers'")
    assert _active_tab(page) == "watchers"


def test_settings_hash_with_query_prefills_search_box(logged_in_page, live_server):
    page = logged_in_page
    page.goto(f"{live_server}/#settings&q=timeout")
    page.wait_for_selector("#settings-overlay", state="visible")
    assert page.input_value("#settings-search") == "timeout"


def test_settings_hash_change_in_open_page_switches_tab(logged_in_page, live_server):
    page = logged_in_page
    page.evaluate("location.hash = '#settings=mcp'")
    page.wait_for_function("document.querySelector('#settings-tabs .sx-tab.active')?.dataset.stab === 'mcp'")


def test_settings_link_in_message_gets_unfurl_card_with_two_rows(logged_in_page, live_server):
    """Settings deep links in bubbles get a hover card like session links:
    row1 = icon + "Settings" + tab, row2 = what the link opens (tab/search)."""
    from ui_tests.test_own_session_link_unfurl import _seed_session_with_message

    page = logged_in_page
    url = f"{live_server}/#settings=watchers&q=poll"
    _seed_session_with_message(page, f"see {url} please")
    page.locator(f".bubble.assistant a[href='{url}']").last.hover()
    card = page.locator(".unfurl-card.unfurl-own_settings").last
    card.wait_for(state="visible", timeout=2000)
    assert "watchers" in card.locator(".unfurl-row1").inner_text()
    assert "poll" in card.locator(".unfurl-row2").inner_text()
