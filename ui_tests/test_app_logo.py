"""
Playwright test for the app logo/favicon (devopspass GitHub
avatar, saved at webui/icons/logo.png). Verifies the favicon <link>, the
sidebar logo <img>, and the login-page logo all point at the same static
asset and that it's actually served (not a 404).
"""

from __future__ import annotations


def test_favicon_link_points_at_logo(logged_in_page, live_server):
    page = logged_in_page
    href = page.eval_on_selector("link[rel='icon']", "el => el.getAttribute('href')")
    assert href == "/static/icons/logo.png"


def test_sidebar_logo_is_present_and_loads(logged_in_page, live_server):
    page = logged_in_page
    logo = page.query_selector(".sidebar-logo")
    assert logo is not None
    assert logo.get_attribute("src") == "/static/icons/logo.png"
    natural_width = logo.evaluate("el => el.naturalWidth")
    assert natural_width > 0


def test_login_page_shows_logo(page, live_server):
    page.goto(f"{live_server}/login")
    logo = page.query_selector(".login-logo")
    assert logo is not None
    assert logo.get_attribute("src") == "/static/icons/logo.png"
