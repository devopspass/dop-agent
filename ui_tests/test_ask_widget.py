"""
Playwright tests for the interactive Question/Decision "ask" widget (webui/ask.js).

Feature: an assistant message with a fenced ```ask block is rendered as clickable
chips inside the message (one per Q#/D# item) instead of a code block. A chip
opens a popover next to it (Card / Answered / All views) with predefined options
and a note field; answers are written into the compose draft as an
'Answers on "<title>" [<id>]' block. Convention: dop-agent/skills/ask-format/SKILL.md.
The fake echo backend replies "echo: <text>", so sending the fence yields an
assistant bubble containing it.
"""

from __future__ import annotations

MSG = (
    "questions:\n```ask title=\"Review\" id=r1\n"
    "D1 single: Where should it live?\n"
    "- a: Keep in base | self-contained\n"
    "- b*: New module | naming rule\n"
    "Q1 multi: Which envs?\n"
    "- tst: tst\n"
    "- stg: stg\n"
    "? which others?\n"
    "Q2 text: Reviewer?\n"
    "```\n"
)


def _send(page, text):
    page.click("#new-session-btn")
    page.fill("#compose-text", text)
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(".ask-chip", timeout=8000)


def test_ask_fence_becomes_chips_and_picks_fill_compose_draft(logged_in_page):
    page = logged_in_page
    _send(page, MSG)
    assert page.locator(".ask-block pre").count() == 0
    assert page.locator(".ask-chip").count() == 3
    page.click(".ask-chip[data-ask-item=D1]")
    assert page.locator(".ask-pop").count() == 1
    page.click(".ask-pop .ask-opt:nth-child(3)")  # option b (children: title row, a, b)
    draft = page.input_value("#compose-text")
    assert 'Answers on "Review" [r1]' in draft
    assert "- [D1] Where should it live? → b. New module" in draft
    assert "✓" in page.locator(".ask-chip[data-ask-item=D1]").inner_text()


def test_multi_select_note_and_skip_semantics(logged_in_page):
    """Multi items collect several keys; a note alone counts as an answer;
    skipped items are omitted from the draft."""
    page = logged_in_page
    _send(page, MSG)
    page.click(".ask-chip[data-ask-item=Q1]")
    page.click(".ask-pop .ask-opt:nth-child(2)")
    page.click(".ask-pop .ask-opt:nth-child(3)")
    page.fill(".ask-pop textarea.ask-note", "plus mig-tst")
    draft = page.input_value("#compose-text")
    assert "- [Q1] Which envs? → tst. tst; stg. stg — plus mig-tst" in draft
    page.click(".ask-pop button:has-text('skip')")  # on Q2
    assert "Q2" not in page.input_value("#compose-text")
    assert "Q2" in page.locator(".ask-pop").inner_text()


def test_hotkeys_pick_option_and_escape_closes_popover(logged_in_page):
    page = logged_in_page
    _send(page, MSG)
    page.click(".ask-chip[data-ask-item=D1]")
    page.keyboard.press("1")
    assert "- [D1] Where should it live? → a. Keep in base" in page.input_value("#compose-text")
    page.keyboard.press("Escape")
    assert page.locator(".ask-pop").count() == 0


def test_popover_survives_rerender_and_draft_text_is_preserved(logged_in_page):
    """renderMessages() rebuilds every bubble; the open popover must be
    re-anchored to the new chip, answers kept, and the user's own draft text
    must never be touched."""
    page = logged_in_page
    _send(page, MSG)
    page.fill("#compose-text", "my own text")
    page.evaluate("document.activeElement.blur()")
    page.click(".ask-chip[data-ask-item=D1]")
    page.keyboard.press("2")
    page.click(".ask-chip[data-ask-item=Q2]")
    page.evaluate("renderMessages(lastTurns, lastRunning)")
    page.wait_for_selector(".ask-pop")
    assert page.locator(".ask-pop").count() == 1
    assert "Q2" in page.locator(".ask-pop .ask-ct").inner_text()
    draft = page.input_value("#compose-text")
    assert draft.startswith("my own text")
    assert "→ b. New module" in draft
    page.click(".ask-pop button:has-text('All')")
    assert page.locator(".ask-row").count() == 3


def test_two_ask_blocks_in_one_message_have_separate_draft_blocks(logged_in_page):
    page = logged_in_page
    _send(page, "two:\n```ask title=\"A\" id=a\nQ1 single: one?\n- y: yes\n```\n```ask title=\"B\" id=b\nQ1 single: two?\n- n: no\n```\n")
    assert page.locator(".ask-block").count() == 2
    page.locator(".ask-chip").nth(0).click()
    page.keyboard.press("1")
    page.keyboard.press("Escape")  # the popover would otherwise cover the next chip
    page.locator(".ask-chip").nth(1).click()
    page.keyboard.press("1")
    draft = page.input_value("#compose-text")
    assert 'Answers on "A" [a]' in draft and 'Answers on "B" [b]' in draft
    assert "- [Q1] one? → y. yes" in draft and "- [Q1] two? → n. no" in draft


def test_invalid_ask_fence_falls_back_to_plain_code_block(logged_in_page):
    page = logged_in_page
    page.click("#new-session-btn")
    page.fill("#compose-text", "```ask\nQ1 single: a\n- x: one\nQ1 single: dup\n```")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(".msg-row.role-assistant", timeout=8000)
    assert page.locator(".ask-chip").count() == 0
    assert page.locator("pre code").count() >= 1


def test_parse_rules_and_merge_block_unit(logged_in_page):
    """Parser contract: keys allow - and _, '*' marks the suggested option, an
    item without options becomes text, duplicate item ids invalidate the block;
    mergeBlock replaces an existing block by trailing [id] even if the title was retyped."""
    page = logged_in_page
    r = page.evaluate("""() => {
      const d = AskWidget.parse('```ask id=x\\nD1 multi: t\\n- mig-tst*: m | n\\n- a_b: z\\n? hint\\nQ1: free\\n```');
      const br = AskWidget.parse('```ask\\nD1 [multi]: t\\n- a: x\\n```');
      const bad = AskWidget.parse('```ask\\nQ1: a\\nQ1: b\\n```');
      const m = AskWidget.mergeBlock('hi\\n\\nAnswers on "Old" [x]\\n- [D1] t → a\\n', AskWidget.headerLine({title:'New', id:'x'}), 'Answers on "New" [x]\\n- [D1] t → b');
      return {d, br, bad, m};
    }""")
    it = r["d"][0]["items"]
    assert it[0]["options"][0] == {"key": "mig-tst", "rec": True, "label": "m", "note": "n"}
    assert it[0]["mode"] == "multi" and it[0]["hint"] == "hint"
    assert it[1]["mode"] == "text"
    assert r["br"][0]["items"][0]["mode"] == "multi"
    assert r["bad"] == []
    assert r["m"].count("Answers on") == 1 and "→ b" in r["m"] and r["m"].startswith("hi")


def test_popover_closes_itself_after_last_item_is_answered(logged_in_page):
    """After answering the final open item the popover closes (it would otherwise
    cover the next chips); chips stay green."""
    page = logged_in_page
    _send(page, "x\n```ask title=\"T\" id=t\nQ1 single: one?\n- y: yes\nQ2 single: two?\n- n: no\n```\n")
    page.click(".ask-chip[data-ask-item=Q1]")
    page.keyboard.press("1")
    page.wait_for_selector(".ask-pop .ask-ct b:has-text('two?')")
    page.keyboard.press("1")
    page.wait_for_function("document.querySelectorAll('.ask-pop').length === 0", timeout=3000)
    assert "✓" in page.locator(".ask-chip[data-ask-item=Q2]").inner_text()


def test_use_suggested_fills_recommended_options_into_draft(logged_in_page):
    """'use suggested for the rest' answers every unanswered item that has a
    '*' recommended option and writes it into the compose draft."""
    page = logged_in_page
    _send(page, MSG)
    page.click(".ask-chip[data-ask-item=D1]")
    page.click(".ask-pop .ask-seg button:has-text('All')")
    page.click(".ask-pop button:has-text('use suggested')")
    assert "- [D1] Where should it live? → b. New module" in page.input_value("#compose-text")


def test_tab_focuses_note_and_note_text_lands_in_draft(logged_in_page):
    """Tab moves focus from hotkey mode into the note field; typed note is
    appended to the item's draft line."""
    page = logged_in_page
    _send(page, MSG)
    page.click(".ask-chip[data-ask-item=D1]")
    page.keyboard.press("1")
    page.keyboard.press("Tab")
    page.keyboard.type("because")
    assert "— because" in page.input_value("#compose-text")


def test_popover_closes_on_session_switch_and_chips_stay_answered(logged_in_page):
    """Opening a new session must close the body-level popover (it would
    otherwise float over an unrelated conversation)."""
    page = logged_in_page
    _send(page, MSG)
    page.click(".ask-chip[data-ask-item=D1]")
    page.keyboard.press("1")
    page.click("#new-session-btn")
    page.wait_for_function("document.querySelectorAll('.ask-pop').length === 0", timeout=3000)


def test_multiline_note_typed_with_shift_enter_never_duplicates_lines_in_draft(logged_in_page):
    """Typing a multi-line note (Shift+Enter) in the ask popover must not add stray
    lines to the compose draft on each key press: continuation lines are indented
    and replaced in place, leaving one header, one answer entry, each note line once."""
    page = logged_in_page
    _send(page, MSG)
    page.click(".ask-chip[data-ask-item=Q2]")
    page.click(".ask-pop textarea.ask-note")
    page.keyboard.type("line one")
    page.keyboard.press("Shift+Enter")
    page.keyboard.type("line two")
    page.keyboard.press("Shift+Enter")
    page.keyboard.type("line three")
    draft = page.input_value("#compose-text")
    assert draft.count('Answers on "Review" [r1]') == 1
    assert draft.count("- [Q2] Reviewer? → line one\n  line two\n  line three") == 1
    assert draft.count("line one") == 1 and draft.count("line two") == 1 and draft.count("line three") == 1
    assert "\n\n\n" not in draft
