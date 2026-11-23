"""
Playwright test for the "search sessions & messages" feature.
By default the search box (#search-input) only matches session titles -
client-side, no backend round trip. Checking "Also search messages"
(#search-include-messages) additionally queries GET /api/search for
message-content matches, merged below the title matches.
"""

from __future__ import annotations


def test_search_finds_message_in_a_different_session_and_opens_it(logged_in_page, live_server):
    page = logged_in_page

    # Session A: contains the term we'll search for.
    page.click("#new-session-btn")
    page.fill("#compose-text", "please bump example-app to 0.19.3")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector("#messages >> text=echo: please bump example-app to 0.19.3", timeout=5000)

    # Session B: does not contain the term - becomes the "currently open"
    # session before we search, so we can confirm search actually switches
    # sessions rather than just finding the currently-open one.
    page.click("#new-session-btn")
    page.fill("#compose-text", "totally unrelated message")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector("#messages >> text=echo: totally unrelated message", timeout=5000)

    # The term only appears in message content, not the session title, so
    # the default title-only search needs the opt-in checkbox ticked.
    page.check("#search-include-messages")
    page.fill("#search-input", "example-app")
    page.wait_for_selector(".search-result", timeout=3000)
    assert "example-app" in page.inner_text("#search-results")

    page.click(".search-result")

    # Clicking a result should open the session containing the match and
    # clear the search box/dropdown.
    page.wait_for_selector("#messages >> text=echo: please bump example-app to 0.19.3", timeout=5000)
    assert page.input_value("#search-input") == ""
    assert not page.is_visible("#search-results")


def test_search_shows_no_matches_message_for_unmatched_query(logged_in_page, live_server):
    page = logged_in_page
    page.click("#new-session-btn")
    page.fill("#compose-text", "hello there")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector("#messages >> text=echo: hello there", timeout=5000)

    page.fill("#search-input", "zzz_definitely_not_present_anywhere_zzz")
    page.wait_for_selector("#search-results >> text=No matches", timeout=3000)


def test_clearing_search_input_hides_results(logged_in_page, live_server):
    page = logged_in_page
    page.click("#new-session-btn")
    page.fill("#compose-text", "a searchable marker term")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector("#messages >> text=echo: a searchable marker term", timeout=5000)

    page.check("#search-include-messages")
    page.fill("#search-input", "searchable marker")
    page.wait_for_selector(".search-result", timeout=3000)

    page.fill("#search-input", "")
    page.wait_for_selector("#search-results", state="hidden", timeout=3000)


def test_default_search_matches_titles_only_not_message_content(logged_in_page, live_server):
    """The checkbox starts unchecked - a term that only appears in a
    message's body (never in the session title, which auto-derives from
    the *first* message only) should show no matches until "Also search
    messages" is ticked."""
    page = logged_in_page
    page.click("#new-session-btn")
    # First message becomes the session's auto-title - keep the marker
    # term out of it. Second message carries the marker in its body only.
    page.fill("#compose-text", "hello there")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector("#messages >> text=echo: hello there", timeout=5000)
    page.fill("#compose-text", "the secret payload word is xyzzy_marker_term")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector("#messages >> text=echo: the secret payload word is xyzzy_marker_term", timeout=5000)

    assert not page.is_checked("#search-include-messages")
    page.fill("#search-input", "xyzzy_marker_term")
    page.wait_for_selector("#search-results >> text=No matches", timeout=3000)

    # Ticking the checkbox re-runs the search and finds the message.
    page.check("#search-include-messages")
    page.wait_for_selector(".search-result", timeout=3000)
    assert "xyzzy_marker_term" in page.inner_text("#search-results")


def test_default_search_matches_session_title(logged_in_page, live_server):
    """A term that appears in a session's title should be found by the
    default title-only search, with no checkbox needed."""
    page = logged_in_page
    page.click("#new-session-btn")
    page.fill("#compose-text", "hello")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector("#messages >> text=echo: hello", timeout=5000)

    # Set a custom title via the same API the double-click-to-rename UI
    # calls (webui/app.js's startRenameSession()), then refresh the
    # sidebar so the new title is loaded into the in-memory `sessions`
    # list that the default title-only search filters over.
    conversation_id = page.evaluate("() => currentConversationId")
    page.evaluate(
        """async (id) => {
            await fetch(`/api/sessions/${encodeURIComponent(id)}/title`, {
                method: 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({title: 'unique-title-marker-42'}),
            });
        }""",
        conversation_id,
    )
    page.reload()
    page.wait_for_selector(".session-item.active .session-title:has-text('unique-title-marker-42')", timeout=3000)

    page.fill("#search-input", "unique-title-marker-42")
    page.wait_for_selector(".search-result", timeout=3000)
    assert "unique-title-marker-42" in page.inner_text("#search-results")
