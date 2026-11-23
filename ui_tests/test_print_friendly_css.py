"""
Playwright tests for the "print-friendly transcript CSS"
feature. See webui/style.css's `@media print` block and the 🖨️ header
button (#print-btn) in app.js that calls window.print().

Playwright can emulate the "print" media type (page.emulate_media) so we
can assert the print stylesheet actually hides interactive chrome and
switches to a plain layout, without needing a real printer/PDF renderer.
"""

from __future__ import annotations

import time


def _seed_session_with_messages(page, texts):
    page.click("#new-session-btn")
    for text in texts:
        page.fill("#compose-text", text)
        page.click("#compose-form button[type=submit]")
        page.wait_for_selector(".session-item.active", timeout=5000)
        time.sleep(0.2)


def test_print_button_is_visible_in_thread_header(logged_in_page, live_server):
    page = logged_in_page
    _seed_session_with_messages(page, ["hello there"])

    # Print is one of the actions inside the thread-header's "⋯"
    # overflow menu - open it before checking the button is reachable.
    page.click("#thread-overflow-btn")
    assert page.is_visible("#print-btn")


def test_print_media_hides_sidebar_and_compose_form(logged_in_page, live_server):
    page = logged_in_page
    _seed_session_with_messages(page, ["hello there"])

    page.emulate_media(media="print")

    assert not page.is_visible("#sidebar")
    assert not page.is_visible("#compose-form")
    assert not page.is_visible("#health-strip")
    assert not page.is_visible(".thread-header")

    # Restore screen media so subsequent assertions/tests behave normally.
    page.emulate_media(media="screen")


def test_print_media_hides_per_message_hover_buttons_and_avatars(logged_in_page, live_server):
    page = logged_in_page
    _seed_session_with_messages(page, ["hello there"])

    page.emulate_media(media="print")

    assert not page.is_visible(".msg-hover-time")
    assert not page.is_visible(".avatar")
    # The message bubble itself must still be visible - only the chrome
    # around it is hidden.
    assert page.is_visible(".msg-row .bubble")

    page.emulate_media(media="screen")


def test_print_button_sets_document_title_during_print_and_restores_after(logged_in_page, live_server):
    page = logged_in_page
    _seed_session_with_messages(page, ["hello there"])
    original_title = page.title()

    page.evaluate("window.dispatchEvent(new Event('beforeprint'))")
    printing_title = page.title()
    assert printing_title != original_title
    assert "DOP Agent transcript" in printing_title

    page.evaluate("window.dispatchEvent(new Event('afterprint'))")
    assert page.title() == original_title
