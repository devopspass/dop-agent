"""
Playwright tests for the quick-reply "actions" widget (webui/actions.js).

Feature: an assistant message with a fenced ```actions block (lines
`- [*] label [| text]`) renders as clickable chips instead of a code block, to
cut the typing of replies like "proceed" / "fix gaps". Clicking inserts the
text into the compose draft (no auto-send), never duplicating a line already
there; `window.ActionsWidget.onClick` / render({onClick}) lets the inbox send
directly instead. Convention: dop-agent/skills/actions-format/SKILL.md.
The fake echo backend replies "echo: <text>", so sending the fence yields an
assistant bubble containing it.
"""

from __future__ import annotations

MSG = (
    "next:\n```actions id=next\n"
    "- * Proceed | proceed\n"
    "- Fix gaps | fix gaps\n"
    "- Skip tests\n"
    "```\n"
)


def _send(page, text):
    page.click("#new-session-btn")
    page.fill("#compose-text", text)
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(".actions-chip", timeout=8000)


def test_actions_fence_becomes_chips_and_primary_is_marked(logged_in_page):
    page = logged_in_page
    _send(page, MSG)
    assert page.locator(".actions-block").count() == 1
    # the user's own bubble keeps the raw fence; only the assistant echo is converted
    assert page.locator(".actions-block pre").count() == 0
    assert page.locator("pre > code.language-actions").count() == 1
    assert page.locator(".actions-chip").count() == 3
    assert page.locator(".actions-chip.primary").inner_text() == "Proceed"


def test_click_inserts_text_into_compose_without_sending_or_duplicating(logged_in_page):
    page = logged_in_page
    _send(page, MSG)
    page.fill("#compose-text", "")
    page.click(".actions-chip >> text=Fix gaps")
    assert page.input_value("#compose-text") == "fix gaps"
    page.click(".actions-chip >> text=Fix gaps")
    page.click(".actions-chip >> text=Skip tests")
    assert page.input_value("#compose-text") == "fix gaps\nSkip tests"
    assert page.locator(".actions-chip.used").count() >= 1


def test_chip_is_keyboard_activatable_and_override_hook_replaces_insert(logged_in_page):
    page = logged_in_page
    _send(page, MSG)
    page.fill("#compose-text", "")
    page.focus(".actions-chip >> text=Proceed")
    page.keyboard.press("Enter")
    assert page.input_value("#compose-text") == "proceed"
    page.evaluate("window.__hits = []; window.ActionsWidget.onClick = (a) => { window.__hits.push(a.text); }; 0")
    page.fill("#compose-text", "")
    page.click(".actions-chip >> text=Fix gaps")
    assert page.evaluate("window.__hits") == ["fix gaps"]
    assert page.input_value("#compose-text") == ""


def test_parse_api_returns_flat_actions_with_default_text(logged_in_page):
    page = logged_in_page
    page.goto(page.url)
    res = page.evaluate("(t) => window.ActionsWidget.parse(t)", MSG)
    assert [(a["label"], a["text"], a["primary"]) for a in res] == [
        ("Proceed", "proceed", True),
        ("Fix gaps", "fix gaps", False),
        ("Skip tests", "Skip tests", False),
    ]
