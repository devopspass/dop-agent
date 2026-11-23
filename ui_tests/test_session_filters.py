"""
Playwright test for the "filter/quick-toggle for
pinned/running/unread/archived sessions" feature: a row of filter chips
(#session-filters .filter-chip, data-filter=all/pinned/running/unread/
archived) above the sidebar list narrows it down to just that category,
instead of requiring the user to scan the whole (potentially long) list by
eye to answer "which sessions are still running right now?" or "what do I
still need to read?".
"""

from __future__ import annotations


def _create_session(page, text):
    page.click("#new-session-btn")
    page.fill("#compose-text", text)
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(f"#messages >> text=echo: {text}", timeout=5000)


def test_pinned_filter_shows_only_pinned_sessions(logged_in_page, live_server):
    page = logged_in_page
    _create_session(page, "will be pinned filter test")
    pinned_item = page.locator(".session-item.active")
    pinned_id = pinned_item.get_attribute("data-conversation-id")
    pinned_item.locator(".session-pin-btn").click()
    page.wait_for_selector(".session-item.pinned", timeout=3000)

    _create_session(page, "stays unpinned filter test")
    unpinned_id = page.locator(".session-item.active").get_attribute("data-conversation-id")

    page.click(".filter-chip[data-filter='pinned']")
    page.wait_for_selector(f".session-item[data-conversation-id='{pinned_id}']", timeout=3000)

    assert page.locator(f".session-item[data-conversation-id='{pinned_id}']").count() == 1
    assert page.locator(f".session-item[data-conversation-id='{unpinned_id}']").count() == 0


def test_running_filter_shows_only_sessions_with_a_turn_in_progress(logged_in_page, live_server):
    page = logged_in_page
    _create_session(page, "finished already filter test")
    finished_id = page.locator(".session-item.active").get_attribute("data-conversation-id")

    page.click("#new-session-btn")
    page.fill("#compose-text", "SLOW_TURN please think for a while")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector("#stop-turn-btn[style*='block']", timeout=5000)
    running_id = page.locator(".session-item.active").get_attribute("data-conversation-id")
    # The Stop button reacts to the per-transcript SSE stream, but the
    # sidebar's `running` flag is only recomputed on the next full
    # /api/sessions fetch (the session-list SSE push isn't triggered just
    # by a turn starting) - force one so the sidebar reflects it before we
    # filter by it.
    page.evaluate("refreshSessionList()")
    page.wait_for_selector(f".session-item[data-conversation-id='{running_id}'] .session-running-spinner", timeout=3000)

    page.click(".filter-chip[data-filter='running']")
    page.wait_for_selector(f".session-item[data-conversation-id='{running_id}']", timeout=3000)

    assert page.locator(f".session-item[data-conversation-id='{running_id}']").count() == 1
    assert page.locator(f".session-item[data-conversation-id='{finished_id}']").count() == 0

    page.click("#stop-turn-btn")


def test_all_filter_restores_previously_hidden_sessions(logged_in_page, live_server):
    page = logged_in_page
    _create_session(page, "session one for all-filter test")
    id_one = page.locator(".session-item.active").get_attribute("data-conversation-id")
    _create_session(page, "session two for all-filter test")
    id_two = page.locator(".session-item.active").get_attribute("data-conversation-id")

    page.click(".filter-chip[data-filter='pinned']")
    page.wait_for_selector(f".session-item[data-conversation-id='{id_one}']", state="detached", timeout=3000)
    assert page.locator(f".session-item[data-conversation-id='{id_two}']").count() == 0

    page.click(".filter-chip[data-filter='all']")

    page.wait_for_selector(f".session-item[data-conversation-id='{id_one}']", timeout=3000)
    assert page.locator(f".session-item[data-conversation-id='{id_one}']").count() == 1
    assert page.locator(f".session-item[data-conversation-id='{id_two}']").count() == 1


def test_empty_filter_result_shows_a_message_instead_of_a_blank_list(logged_in_page, live_server):
    page = logged_in_page
    _create_session(page, "no filters applied to this one")

    # Unpin every currently-pinned session left over from other tests in
    # this run (they share one live_server/DB), so the "pinned" filter
    # can genuinely be empty for this assertion.
    page.click(".filter-chip[data-filter='pinned']")
    for btn in page.locator(".session-pin-btn").all():
        btn.click()
        page.wait_for_timeout(50)

    page.wait_for_selector(".session-filter-empty", timeout=3000)
    assert "pinned" in page.inner_text(".session-filter-empty")

