"""
Playwright test for switching between sessions in the sidebar
updating the transcript pane promptly.

openSession() in webui/app.js (a) synchronously clears #messages when
switching, and (b) fetches the new session's transcript over a plain REST
call right away (via applyConversationData) rather than waiting on the SSE
stream's first event (which has handshake/auth latency); the SSE connection
then takes over for live updates.
"""

from __future__ import annotations


def test_switching_sessions_clears_stale_content_immediately(logged_in_page, live_server):
    page = logged_in_page

    # Session A: distinctive content.
    page.click("#new-session-btn")
    page.fill("#compose-text", "unique marker for session A")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector("#messages >> text=echo: unique marker for session A", timeout=5000)
    page.wait_for_selector(".session-item.active", timeout=5000)
    session_a_item = page.eval_on_selector(".session-item.active", "el => el.dataset.conversationId")

    # Session B: distinctive content, different from A.
    page.click("#new-session-btn")
    page.fill("#compose-text", "unique marker for session B")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector("#messages >> text=echo: unique marker for session B", timeout=5000)

    # Switch back to A via the sidebar - the transcript should reflect A's
    # content and must never show B's leftover content once the click has
    # registered (no stale intermediate state, however briefly).
    page.click(f".session-item[data-conversation-id='{session_a_item}']")
    page.wait_for_selector("#messages >> text=echo: unique marker for session A", timeout=2000)
    assert "unique marker for session B" not in page.inner_text("#messages")


def test_switching_sessions_never_shows_previous_sessions_stale_bubble(logged_in_page, live_server):
    """Right after clicking a different session, #messages must not still
    contain the previous session's marker text, even for a moment - it is
    wiped synchronously on click, not only after the network round trip
    completes."""
    page = logged_in_page

    page.click("#new-session-btn")
    page.fill("#compose-text", "content belonging only to session ONE")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector("#messages >> text=echo: content belonging only to session ONE", timeout=5000)
    session_one = page.eval_on_selector(".session-item.active", "el => el.dataset.conversationId")

    page.click("#new-session-btn")
    page.fill("#compose-text", "content belonging only to session TWO")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector("#messages >> text=echo: content belonging only to session TWO", timeout=5000)

    page.click(f".session-item[data-conversation-id='{session_one}']")
    # Poll immediately (no artificial sleep) - #messages should already be
    # cleared of session TWO's content synchronously on click.
    still_has_stale_content = page.eval_on_selector(
        "#messages", "el => el.textContent.includes('content belonging only to session TWO')"
    )
    assert not still_has_stale_content, "switching sessions must clear stale content synchronously, not lazily"
