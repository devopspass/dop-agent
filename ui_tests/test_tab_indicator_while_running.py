"""
Feature: browser-tab activity indicator.

While any non-archived session is running, the favicon is redrawn as the
logo with a rotating arc and the tab title gets a "⏳" prefix; when nothing
runs, the original favicon and title are restored.
"""

from __future__ import annotations


def test_favicon_animates_and_title_prefixed_only_while_running(logged_in_page, live_server):
    page = logged_in_page
    base_title = page.title()
    base_href = page.eval_on_selector("link[rel='icon']", "el => el.getAttribute('href')")

    page.evaluate("window.TabIndicator.set(true)")
    page.wait_for_function("document.querySelector(\"link[rel='icon']\").href.startsWith('data:image/png')")
    first = page.eval_on_selector("link[rel='icon']", "el => el.href")
    page.wait_for_function(
        "h => document.querySelector(\"link[rel='icon']\").href !== h", arg=first
    )
    assert page.title() == "⏳ " + base_title

    page.evaluate("window.TabIndicator.set(false)")
    assert page.title() == base_title
    assert page.eval_on_selector("link[rel='icon']", "el => el.getAttribute('href')") == base_href
