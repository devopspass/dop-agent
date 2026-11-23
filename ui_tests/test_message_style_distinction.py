"""
Playwright test locking in the visual distinction between
user, assistant, and system/watch-injected messages in the transcript
(see .bubble.user / .bubble.assistant / .bubble.system in style.css and
the avatar-user / avatar-assistant / avatar-system classes in app.js's
renderMessages()). System messages (watch-fired notices, interrupt
notices, "continue in new session" backlinks, etc.) all use role="system"
server-side and must look visually distinct from normal user/agent
bubbles - dashed border, italic, muted color, smaller font - so they read
as "this was injected by the system", not part of the actual
human<->agent conversation.
"""

from __future__ import annotations


def test_user_assistant_and_system_messages_have_distinct_styles(logged_in_page, live_server):
    page = logged_in_page
    page.click("#new-session-btn")
    page.fill("#compose-text", "hello there")
    page.click("#compose-form button[type=submit]")
    # Scope to the transcript panel: the sidebar can independently show a
    # matching preview from an earlier test's session (live_server is
    # session-scoped and shared across ui_tests), which would otherwise let
    # a bare "text=..." selector resolve before the current turn's bubbles
    # actually render.
    page.wait_for_selector("#messages >> text=echo: hello there", timeout=5000)

    user_bg = page.eval_on_selector(".bubble.user", "el => getComputedStyle(el).backgroundColor")
    assistant_bg = page.eval_on_selector(".bubble.assistant", "el => getComputedStyle(el).backgroundColor")
    user_avatar = page.eval_on_selector(".avatar.avatar-user", "el => el.textContent")
    assistant_avatar = page.eval_on_selector(".avatar.avatar-assistant", "el => el.textContent")

    # Seed a system message the same way watch-fired notices/backlinks do,
    # via the "Continue in new session" action, which server-side records
    # a role="system" message in the *new* conversation. That action lives
    # inside the thread-header's "⋯" overflow menu.
    page.click("#thread-overflow-btn")
    page.click("#continue-session-btn")
    page.wait_for_selector(".msg-row.role-system", timeout=5000)

    system_style = page.eval_on_selector(
        ".bubble.system",
        "el => { const s = getComputedStyle(el); return {borderStyle: s.borderStyle, fontStyle: s.fontStyle}; }",
    )
    system_avatar = page.eval_on_selector(".avatar.avatar-system", "el => el.textContent")

    # The three roles must not share identical background/emphasis styling.
    assert user_bg != assistant_bg
    assert system_style["borderStyle"] == "dashed"
    assert system_style["fontStyle"] == "italic"
    assert len({user_avatar, assistant_avatar, system_avatar}) == 3
