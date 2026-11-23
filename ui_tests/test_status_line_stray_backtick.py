"""
Playwright test that the Web UI's live "steps" status
history (each tool-call/status line meant to render on its own row - see
copilot_session.py's status_updater()/_build_combined()) never visually
merges several unrelated lines into one row.

Status lines wrap things like a shell command in single
backticks (e.g. ``:wrench: `pretty`: desc (`cmd`)``` from
CopilotConversation._summarize_tool_arguments()/_event_to_status()). If a
tool-call argument itself contains a stray/unpaired backtick, that status
line has an odd total backtick count. marked.js's inline-code lexer would
treat the lone backtick as the *start* of an unterminated code span and keep
scanning past that line's newline for a closing backtick, swallowing
following "step" lines into a single <code> element.

sanitizeDanglingInlineCode() in app.js escapes the backticks of any line with
an odd backtick count (outside a fenced ``` block) before marked.js sees it,
so a bad line can never eat subsequent lines. This test drives that scenario
through the real renderMarkdown() pipeline via the fake echo backend (see
conftest.py).

A *single* line with an unmatched backtick is not enough to trigger the
problem (marked treats one lone backtick with nothing to pair with as
literal text). It needs *two* separate lines that each have an unpaired
backtick: marked's inline-code lexer pairs the first line's stray backtick
with the second one's (potentially many lines later), collapsing every line
in between into a single <code> span. That is the scenario below.
"""

from __future__ import annotations

import time

# Lines 1 and 3 each carry a single, unpaired backtick (odd count on that
# line) simulating tool-call arguments that contain a stray backtick.
# Line 2 is a normal, independent status line in between; unsanitized,
# marked.js would pair line 1's backtick with line 3's and swallow line 2
# into one <code> span.
BUGGY_STATUS_TEXT = (
    "step one: run ` a command\n"
    "step two: completed successfully\n"
    "step three: done with ` trailing"
)


def _seed_session_with_message(page, text):
    page.click("#new-session-btn")
    page.fill("#compose-text", text)
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(".session-item.active", timeout=5000)
    time.sleep(0.3)


def test_stray_backtick_line_does_not_swallow_following_status_lines(logged_in_page, live_server):
    page = logged_in_page
    _seed_session_with_message(page, BUGGY_STATUS_TEXT)

    bubble = page.locator(".bubble.assistant").last
    html = bubble.inner_html()
    code_texts = bubble.locator("code").all_inner_texts()

    # All three lines must still be present and on their own row
    # (marked's `breaks: true` turns single newlines into <br>).
    text = bubble.inner_text()
    assert "step one: run" in text
    assert "step two: completed successfully" in text
    assert "step three: done with" in text

    # No single <code> element may bridge more than one of the three lines.
    for code_text in code_texts:
        matched_lines = sum(
            marker in code_text
            for marker in ("step one", "step two", "step three")
        )
        assert matched_lines <= 1, (
            f"a single <code> element spans multiple status lines: {code_text!r}\nfull html: {html}"
        )
