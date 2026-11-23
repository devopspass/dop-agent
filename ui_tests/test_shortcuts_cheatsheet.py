"""
Playwright tests for the '?' keyboard-shortcut cheat-sheet
overlay. See webui/index.html's #shortcuts-overlay and webui/app.js's
openShortcutsOverlay()/closeShortcutsOverlay() plus the '?'/Escape
branches in the document-level keydown listener.
"""

from __future__ import annotations

import time


def test_question_mark_opens_the_shortcuts_cheatsheet(logged_in_page, live_server):
    page = logged_in_page

    assert page.eval_on_selector(
        "#shortcuts-overlay", "el => getComputedStyle(el).display"
    ) == "none"

    page.click("body")
    page.keyboard.press("?")
    time.sleep(0.1)

    assert page.eval_on_selector(
        "#shortcuts-overlay", "el => getComputedStyle(el).display"
    ) != "none"
    # Sanity: a couple of the documented shortcuts actually appear in the
    # panel text.
    text = page.inner_text("#shortcuts-overlay")
    assert "Next session" in text
    assert "Focus search" in text


def test_escape_closes_the_shortcuts_cheatsheet(logged_in_page, live_server):
    page = logged_in_page

    page.click("body")
    page.keyboard.press("?")
    page.wait_for_selector("#shortcuts-overlay", state="visible", timeout=2000)

    page.keyboard.press("Escape")
    time.sleep(0.1)
    assert page.eval_on_selector(
        "#shortcuts-overlay", "el => getComputedStyle(el).display"
    ) == "none"


def test_close_button_and_clicking_outside_the_panel_close_the_cheatsheet(logged_in_page, live_server):
    page = logged_in_page

    page.click("body")
    page.keyboard.press("?")
    page.wait_for_selector("#shortcuts-overlay", state="visible", timeout=2000)
    page.click("#shortcuts-close-btn")
    time.sleep(0.1)
    assert page.eval_on_selector(
        "#shortcuts-overlay", "el => getComputedStyle(el).display"
    ) == "none"

    page.keyboard.press("?")
    page.wait_for_selector("#shortcuts-overlay", state="visible", timeout=2000)
    # Click the overlay backdrop itself (outside the panel) to close it.
    page.click("#shortcuts-overlay", position={"x": 5, "y": 5})
    time.sleep(0.1)
    assert page.eval_on_selector(
        "#shortcuts-overlay", "el => getComputedStyle(el).display"
    ) == "none"


def test_question_mark_is_ignored_while_typing_in_compose_box(logged_in_page, live_server):
    page = logged_in_page

    page.click("#new-session-btn")
    page.fill("#compose-text", "seed message")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(".session-item.active", timeout=5000)
    time.sleep(0.2)

    page.click("#compose-text")
    page.type("#compose-text", "what is this? just a question")
    assert page.input_value("#compose-text") == "what is this? just a question"
    assert page.eval_on_selector(
        "#shortcuts-overlay", "el => getComputedStyle(el).display"
    ) == "none"
