"""
Playwright test for the "syntax-highlighted diff view"
feature.

highlight.js's bundled "Diff" language (see webui/index.html's highlight.js
CDN include) is loaded, and enhanceCodeBlocks() (see
ui_tests/test_code_block_highlighting.py) runs hljs.highlightElement() on
every <pre><code> block regardless of language - so a fenced ```diff block
gets added/removed line coloring via hljs's "hljs-addition"/"hljs-deletion"
token classes, with no diff-specific app.js/CSS code.
"""

from __future__ import annotations

import time

DIFF_MESSAGE = (
    "please review:\n```diff\n"
    "-old_line = 1\n"
    "+new_line = 2\n"
    "```"
)


def test_fenced_diff_block_is_syntax_highlighted(logged_in_page, live_server):
    page = logged_in_page
    page.click("#new-session-btn")
    page.fill("#compose-text", DIFF_MESSAGE)
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(".session-item.active", timeout=5000)
    time.sleep(0.3)

    code_el = page.locator(".bubble.assistant pre code").last
    assert "hljs" in (code_el.get_attribute("class") or "")
    # highlight.js's diff grammar marks added/removed lines with dedicated
    # token classes - confirms the *diff* grammar specifically ran (not
    # just "some" language falling back to plaintext highlighting).
    assert code_el.locator(".hljs-addition").count() > 0
    assert code_el.locator(".hljs-deletion").count() > 0

    # Same copy-button infrastructure as any other code block - the raw
    # (unhighlighted) diff text should still be what gets copied.
    copy_btn = page.locator(".bubble.assistant .code-copy-btn").last
    assert copy_btn.count() > 0
