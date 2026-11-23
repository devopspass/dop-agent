"""
Playwright tests for the interactive "ideas" widget (webui/ideas.js).

Feature: an assistant message with a fenced ```ideas block is rendered as a
Card / Selected / All triage widget instead of a code block. Deciding an idea
(ok / nok / skip, optional comment) writes a "Feedback on ..." block into the
compose draft. Convention: dop-agent/skills/ideas-format/SKILL.md. The fake echo
backend replies "echo: <text>", so sending the fence yields an assistant
bubble containing it.
"""

from __future__ import annotations

MSG = (
    "ideas:\n```ideas title=\"Three ideas\" id=t3\n"
    "- [1] First idea :: does `a` | effort=S\n"
    "- [2] Second idea :: does b\n"
    "- [3] Third idea :: does c\n```\n"
)


def _send(page, text):
    page.click("#new-session-btn")
    page.fill("#compose-text", text)
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(".ideas-widget", timeout=8000)


def test_ideas_fence_becomes_widget_and_decisions_fill_compose_box(logged_in_page):
    page = logged_in_page
    _send(page, MSG)
    assert page.locator(".ideas-widget pre").count() == 0
    assert "First idea" in page.locator(".ideas-card-title").inner_text()
    page.fill("textarea.ideas-note", "needs a flag")
    page.click(".ideas-acts button:has-text('ok')")
    page.click(".ideas-acts button:has-text('nok')")
    page.click(".ideas-acts button:has-text('skip')")
    draft = page.input_value("#compose-text")
    assert 'Feedback on "Three ideas" [t3]' in draft
    assert "- [1] OK — First idea: needs a flag" in draft
    assert "- [2] NOK — Second idea" in draft
    assert "Third idea" not in draft  # skip is omitted
    # finishing the last card switches to the Selected table
    assert page.locator(".ideas-row").count() == 1


def test_widget_state_and_hotkeys_survive_rerender_and_keep_other_draft_text(logged_in_page):
    page = logged_in_page
    _send(page, MSG)
    page.fill("#compose-text", "my own text")
    page.evaluate("document.activeElement.blur()")
    page.hover(".ideas-widget")
    page.keyboard.press("1")
    page.evaluate("renderMessages(lastTurns, lastRunning)")
    assert "Second idea" in page.locator(".ideas-card-title").inner_text()
    draft = page.input_value("#compose-text")
    assert draft.startswith("my own text")
    assert "- [1] OK — First idea" in draft
    page.click(".ideas-seg button:has-text('All')")
    assert page.locator(".ideas-row").count() == 3
    page.click(".ideas-row:nth-child(1) .t")
    page.click(".ideas-row-x button:has-text('ok')")  # toggles decision off
    assert "Feedback on" not in page.input_value("#compose-text")
    assert page.input_value("#compose-text").startswith("my own text")


def test_invalid_ideas_fence_falls_back_to_plain_code_block(logged_in_page):
    page = logged_in_page
    page.click("#new-session-btn")
    page.fill("#compose-text", "```ideas title=x\n- [1] dup\n- [1] dup\n```")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(".msg-row.role-assistant", timeout=8000)
    assert page.locator(".ideas-widget").count() == 0
    assert page.locator("pre code").count() >= 1


def test_edited_feedback_header_is_still_replaced_not_duplicated(logged_in_page):
    """If the user retypes the title part of the 'Feedback on "..." [id]' header
    line, the next decision still replaces the block (matched by the trailing
    [id] tag) instead of appending a second one."""
    page = logged_in_page
    _send(page, MSG)
    page.click(".ideas-acts button:has-text('ok')")
    page.fill("#compose-text", page.input_value("#compose-text").replace('"Three ideas"', '"my title"'))
    page.click(".ideas-acts button:has-text('ok')")
    draft = page.input_value("#compose-text")
    assert draft.count("Feedback on") == 1
    assert "- [2] OK — Second idea" in draft


def test_fresh_ideas_message_takes_keyboard_focus_and_escape_returns_to_compose(logged_in_page):
    """When an ideas message arrives, keyboard focus jumps from the
    compose box into the card so 1/2/3 decide immediately (no mouse needed);
    Esc hands focus back to the compose box."""
    page = logged_in_page
    _send(page, MSG)
    page.wait_for_function("document.activeElement && document.activeElement.classList.contains('ideas-widget')")
    page.keyboard.press("1")
    assert "- [1] OK — First idea" in page.input_value("#compose-text")
    page.keyboard.press("Escape")
    assert page.evaluate("document.activeElement.id") == "compose-text"


def test_changing_decisions_repeatedly_never_grows_the_draft(logged_in_page):
    """Re-deciding ideas (ok -> nok -> ok, multi-line comments, switching
    to the table and back) must not make the 'Feedback on ...' text pile up in
    the compose box. The block is always replaced in place:
    exactly one header, one line per idea, regardless of how often or how the
    decisions change."""
    page = logged_in_page
    _send(page, MSG)
    page.click(".ideas-acts button:has-text('ok')")
    page.click(".ideas-seg button:has-text('Card')")
    page.click(".ideas-back, .ideas-acts button:has-text('back')")
    page.click("textarea.ideas-note")
    page.keyboard.type("line one")
    page.keyboard.press("Shift+Enter")
    page.keyboard.type("line two")
    page.keyboard.press("Enter")
    page.keyboard.type("line three")
    page.click(".ideas-seg button:has-text('All')")
    page.click(".ideas-row .t >> nth=0")
    for b in ("nok", "ok", "nok", "ok", "nok", "ok"):
        page.click(f".ideas-row-x button:has-text('{b}')")
        page.fill("input.ideas-note", f"c {b}\nmore")
    draft = page.input_value("#compose-text")
    assert draft.count("Feedback on") == 1, draft
    assert draft.count("\n") < 8, draft
