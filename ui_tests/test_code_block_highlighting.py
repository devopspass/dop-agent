"""
Playwright tests for the syntax-highlighted code blocks with
a copy button. See webui/app.js's enhanceCodeBlocks() (called from
renderMessages() after each bubble's markdown HTML is set) and
webui/index.html's highlight.js <script>/<link> includes.

The fake echo backend (see ui_tests/conftest.py's _fake_handle_fn) replies
with "echo: {text}" - sending a message containing a fenced code block
means the assistant reply also contains that fenced block, which marked.js
renders as a real <pre><code> element for these tests to check against.
"""

from __future__ import annotations

import time

CODE_MESSAGE = "please review:\n```python\nprint('hello world')\n```"


def _seed_session_with_code_block(page):
    page.click("#new-session-btn")
    page.fill("#compose-text", CODE_MESSAGE)
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(".session-item.active", timeout=5000)
    time.sleep(0.3)


def test_fenced_code_block_is_syntax_highlighted(logged_in_page, live_server):
    page = logged_in_page
    _seed_session_with_code_block(page)

    code_el = page.locator(".bubble.assistant pre code").last
    # highlight.js adds an "hljs" class and wraps tokens in <span> elements
    # once it has actually processed a block - a plain unhighlighted
    # <code> block would have neither.
    assert "hljs" in (code_el.get_attribute("class") or "")
    assert code_el.locator("span").count() > 0


def test_code_block_has_a_working_copy_button(logged_in_page, live_server):
    page = logged_in_page
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    _seed_session_with_code_block(page)

    copy_btn = page.locator(".bubble.assistant .code-copy-btn").last
    assert copy_btn.count() > 0

    copy_btn.click(force=True)
    time.sleep(0.1)
    clipboard_text = page.evaluate("navigator.clipboard.readText()")
    assert "print('hello world')" in clipboard_text


def test_code_copy_button_is_not_duplicated_on_repeated_renders(logged_in_page, live_server):
    """renderMessages() rebuilds #messages' innerHTML on every refresh (SSE
    heartbeat, new message, etc.); enhanceCodeBlocks() must not accumulate
    extra copy buttons across those re-renders."""
    page = logged_in_page
    _seed_session_with_code_block(page)

    # Trigger a couple of extra renders like the periodic status refresh
    # would, by re-fetching the transcript via the same code path the app
    # itself uses.
    conversation_id = page.evaluate("currentConversationId")
    for _ in range(2):
        page.evaluate(
            "(id) => fetch(`/api/sessions/${id}`).then(r => r.json()).then(applyConversationData)",
            conversation_id,
        )
        time.sleep(0.2)

    assert page.locator(".bubble.assistant .code-copy-btn").count() == 1

