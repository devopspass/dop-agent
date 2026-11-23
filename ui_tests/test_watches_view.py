"""
Playwright tests for the Watches view in the Web UI (see
web_server.py's GET /api/watches + /api/watches/{id}/... endpoints and
webui/app.js's watchesBtn/watches-overlay wiring, triggered from the
health-strip's "Watchers" item, id #health-watches).

NOTE: every watch is keyed by `conversation_id` (watchers.add_watch's
second positional arg), not by raw Slack channel/thread_ts, so these tests
use message_store.get_or_create_conversation(...) to mint a real
conversation_id first, as production code does. The panel also has inline
pause/resume/remove/recheck actions (see the test_watches_panel_* tests).
"""

from __future__ import annotations


def test_watches_panel_lists_registered_watch(logged_in_page, live_server):
    import message_store
    import watchers

    conv = message_store.get_or_create_conversation(None)
    watchers.add_watch(
        "pipeline",
        conv,
        key="group/project@main",
        label="Test pipeline watch",
        announce=False,
    )

    page = logged_in_page
    page.click("#health-watches")
    page.wait_for_selector(".watch-item", timeout=5000)

    assert "Test pipeline watch" in page.inner_text(".watch-item")
    assert "active" in page.inner_text(".watch-status-badge").lower()

    page.click("#watches-close-btn")
    assert not page.is_visible("#watches-overlay")


def test_watches_panel_include_inactive_toggle(logged_in_page, live_server):
    import message_store
    import watchers

    conv = message_store.get_or_create_conversation(None)
    watch_id = watchers.add_watch(
        "time", conv, key="in:5m", label="Done reminder", announce=False,
    )
    watchers.set_status(watch_id, "done")

    page = logged_in_page
    page.click("#health-watches")
    page.wait_for_selector(".watches-list", timeout=5000)

    # Done watches are excluded by default.
    assert "Done reminder" not in page.inner_text(".watches-list")

    page.check("#watches-include-inactive")
    page.wait_for_function(
        "() => document.querySelector('.watches-list').innerText.includes('Done reminder')",
        timeout=5000,
    )

    page.click("#watches-close-btn")


def test_thread_watches_button_scopes_to_current_session_thread(logged_in_page, live_server):
    """The 👁 button in a session's thread header opens the same
    Watches panel but pre-filtered to that session's own conversation_id
    (see GET /api/watches?conversation_id=... and webui/app.js's
    threadWatchesBtn), so you can check what's pending for the session you
    currently have open without hunting through every watch across every
    session."""
    import message_store
    import watchers

    # Two sessions, each with its own watch registered against that
    # specific conversation_id.
    conv_a = message_store.get_or_create_conversation("C_TEST:111.111")
    message_store.record_message(conv_a, "user", "hello from thread A")
    watchers.add_watch("mr", conv_a, key="group/project#1", label="Thread A watch", announce=False)

    conv_b = message_store.get_or_create_conversation("C_TEST:222.222")
    message_store.record_message(conv_b, "user", "hello from thread B")
    watchers.add_watch("pipeline", conv_b, key="group/project#2", label="Thread B watch", announce=False)

    page = logged_in_page
    page.reload()
    page.click(f".session-item[data-conversation-id='{conv_a}']")
    page.wait_for_selector(".session-item.active", timeout=5000)

    page.click("#thread-watches-btn")
    page.wait_for_selector(".watch-item", timeout=5000)

    assert "Thread A watch" in page.inner_text(".watches-list")
    assert "Thread B watch" not in page.inner_text(".watches-list")
    assert "this session" in page.inner_text("#watches-panel-title").lower()

    # The "Show all sessions" toggle should reveal the other session's watch too.
    page.check("#watches-show-all-threads")
    page.wait_for_function(
        "() => document.querySelector('.watches-list').innerText.includes('Thread B watch')",
        timeout=5000,
    )
    assert "Thread A watch" in page.inner_text(".watches-list")

    page.click("#watches-close-btn")


def test_thread_watches_chip_shows_status_dot_scoped_to_current_thread(logged_in_page, live_server):
    """The 👁 Watches chip in the thread header has a status dot
    (#thread-watches-dot),
    scoped to *that session's own conversation_id* only (GET
    /api/watches?conversation_id=...), independent from the global
    'Watchers' health-strip item which reflects every session's watches
    combined. The dot is hidden entirely for a session with zero
    registered watches (not shown as a neutral/gray color), appears green
    once a watch exists and none are broken, and flips red the moment any
    of *this session's* watches - and only this session's - gets marked
    broken."""
    import message_store
    import watchers

    conv_a = message_store.get_or_create_conversation("C_TEST:333.111")
    message_store.record_message(conv_a, "user", "hello from thread A")

    conv_b = message_store.get_or_create_conversation("C_TEST:333.222")
    message_store.record_message(conv_b, "user", "hello from thread B")
    watch_b = watchers.add_watch(
        "pipeline", conv_b, key="group/project#5555", label="Thread B watch", announce=False,
    )
    watchers.mark_broken(watch_b, "confirmed 404 - test")

    page = logged_in_page
    page.reload()

    # Thread A has no watches at all -> dot stays hidden, not neutral-gray.
    page.click(f".session-item[data-conversation-id='{conv_a}']")
    page.wait_for_selector(".session-item.active", timeout=5000)
    page.evaluate("refreshThreadWatchesHealth()")
    page.wait_for_function(
        "() => !document.querySelector('#thread-watches-dot').classList.contains('chip-dot-visible')",
        timeout=5000,
    )

    # Thread B has a broken watch -> dot visible and red, and switching
    # threads picks this up immediately (not just on the next 30s tick).
    page.click(f".session-item[data-conversation-id='{conv_b}']")
    page.wait_for_selector(".session-item.active", timeout=5000)
    page.wait_for_function(
        "() => document.querySelector('#thread-watches-dot').classList.contains('chip-dot-bad')",
        timeout=5000,
    )
    assert "broken" in page.get_attribute("#thread-watches-btn", "title").lower()

    # Switching back to thread A immediately clears the dot again - it must
    # not "stick" from the previously-viewed thread.
    page.click(f".session-item[data-conversation-id='{conv_a}']")
    page.wait_for_selector(".session-item.active", timeout=5000)
    page.wait_for_function(
        "() => !document.querySelector('#thread-watches-dot').classList.contains('chip-dot-visible')",
        timeout=5000,
    )

    # Clean up: live_server's watches DB is session-scoped and shared with
    # every other test in this file - leaving this broken watch behind
    # would make the global health-strip test (which asserts a healthy
    # starting state) flaky depending on test order.
    watchers.remove_watch(watch_b)


def test_thread_watches_button_shows_empty_message_for_session_without_watches(logged_in_page, live_server):
    """A session with no registered watches yet (Web-UI-only or otherwise -
    every session has a real conversation_id regardless of whether it has
    an underlying Slack thread) should see a plain "no watches" empty
    state for its own scope, not a misleading global list under a
    "this session" heading."""
    page = logged_in_page

    page.click("#new-session-btn")
    page.fill("#compose-text", "web ui only session")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(".session-item.active", timeout=5000)

    page.click("#thread-watches-btn")
    page.wait_for_selector(".watches-empty", timeout=5000)

    assert "no watches registered for this session" in page.inner_text(".watches-empty").lower()

    page.click("#watches-close-btn")


def test_health_strip_watchers_item_shows_status_dot_and_opens_global_panel(logged_in_page, live_server):
    """The 'Watchers' health-strip item (right after MCP) is a real
    .health-item with a .health-dot, like Slack/MCP/Live, colored via GET /api/watches: green (health-ok) when no
    registered watch has status == 'broken', red (health-bad) the moment
    any watch has flipped to 'broken' (see watchers.mark_broken()) - so a
    silently-broken watch is visible at a glance without opening the panel.
    Clicking it opens the global (all-sessions) watches overlay."""
    import message_store
    import watchers

    page = logged_in_page

    # No broken watches exist yet in this shared test DB -> healthy (green)
    # dot. (Doesn't assert an exact watch count: live_server is session-
    # scoped, so other tests in this file/run may have already registered
    # unrelated active watches - only their 'broken'-ness matters here.)
    page.wait_for_function(
        "() => document.querySelector('#health-watches').classList.contains('health-ok')",
        timeout=5000,
    )
    assert "broken" not in page.get_attribute("#health-watches", "title").lower()

    # Register a watch and immediately mark it broken - the dot should flip
    # to health-bad on the next poll tick without a page reload.
    conv = message_store.get_or_create_conversation(None)
    watch_id = watchers.add_watch(
        "pipeline", conv, key="group/project#4321", label="Broken test watch", announce=False,
    )
    watchers.mark_broken(watch_id, "confirmed 404 - test")
    page.evaluate("refreshWatchesHealth()")

    page.wait_for_function(
        "() => document.querySelector('#health-watches').classList.contains('health-bad')",
        timeout=10000,
    )
    assert "broken" in page.get_attribute("#health-watches", "title").lower()

    # Clicking still opens the global (unscoped) watches overlay.
    page.click("#health-watches")
    page.wait_for_selector(".watch-item", timeout=5000)
    assert "Broken test watch" in page.inner_text(".watches-list")
    assert "this session" not in page.inner_text("#watches-panel-title").lower()

    page.click("#watches-close-btn")


def test_watches_panel_row_actions_pause_and_remove_a_watch(logged_in_page, live_server):
    """Every Watches panel row has inline pause/resume/remove/recheck action
    buttons backed by the POST/DELETE /api/watches/{id}/... endpoints, so a
    broken or stale watch can be paused or removed directly from the Web UI.
    This exercises pause (status flips, row gets the
    watch-status-paused class) and remove (row disappears after the
    confirm() prompt, which Playwright auto-accepts via the dialog
    handler)."""
    import message_store
    import watchers

    conv = message_store.get_or_create_conversation(None)
    watch_id = watchers.add_watch(
        "ticket", conv, key="PRJ-99999", label="Row action test watch", announce=False,
    )

    page = logged_in_page
    page.on("dialog", lambda dialog: dialog.accept())
    page.click("#health-watches")
    page.wait_for_selector(f".watch-item[data-watch-id='{watch_id}']", timeout=5000)

    row = page.locator(f".watch-item[data-watch-id='{watch_id}']")
    row.locator(".watch-action-btn[data-action='pause']").click()
    page.wait_for_selector(f".watch-item[data-watch-id='{watch_id}'].watch-status-paused", timeout=5000)
    assert watchers.get_watch(watch_id)["status"] == "paused"

    row.locator(".watch-action-btn[data-action='remove']").click()
    page.wait_for_function(
        f"() => !document.querySelector(\".watch-item[data-watch-id='{watch_id}']\")",
        timeout=5000,
    )
    assert watchers.get_watch(watch_id) is None

    page.click("#watches-close-btn")


def test_watches_panel_search_filters_by_label(logged_in_page, live_server):
    """New search box (#watches-search, approved UX item #16) filters the
    already-loaded watch list client-side by label/key substring, so
    finding one specific watch in a long list doesn't require scrolling
    through every type group."""
    import message_store
    import watchers

    conv = message_store.get_or_create_conversation(None)
    watchers.add_watch("ticket", conv, key="PRJ-11111", label="Findable ticket watch", announce=False)
    watchers.add_watch("ticket", conv, key="PRJ-22222", label="Other ticket watch", announce=False)

    page = logged_in_page
    page.click("#health-watches")
    page.wait_for_selector(".watch-item", timeout=5000)

    page.fill("#watches-search", "Findable")
    page.wait_for_function(
        "() => !document.querySelector('.watches-list').innerText.includes('Other ticket watch')",
        timeout=5000,
    )
    assert "Findable ticket watch" in page.inner_text(".watches-list")

    page.click("#watches-close-btn")


def test_long_time_watch_label_can_be_expanded_to_read_the_full_prompt(logged_in_page, live_server):
    """A `time` watch's `label` IS the literal prompt text fired into the
    thread when it triggers (unlike every other watch type, where `label`
    is just a short human nickname - see watchers.py's _poll_time_watch).
    A long/multi-sentence prompt is not limited to the row's native
    title="..." tooltip (which doesn't wrap reliably and is unusable on touch
    devices). Any row with a label over 60 chars (not just `time`
    watches) gets a '⤢' expand toggle next to the label that reveals
    the full, word-wrapped text inline via the .watch-full-text block
    - no tooltip hovering required."""
    import message_store
    import watchers

    long_prompt = (
        "Check if the nightly STG batch job finished successfully and post "
        "a summary with any failed steps, retry counts, and the duration "
        "compared to yesterday's run before the morning standup."
    )
    conv = message_store.get_or_create_conversation(None)
    watch_id = watchers.add_watch(
        "time", conv, key="in:1h", label=long_prompt, announce=False,
    )

    page = logged_in_page
    page.click("#health-watches")
    page.wait_for_selector(f".watch-item[data-watch-id='{watch_id}']", timeout=5000)

    row = page.locator(f".watch-item[data-watch-id='{watch_id}']")
    expand_btn = row.locator(".watch-label-expand-btn")
    full_text = row.locator(f"[data-full-text-for='{watch_id}']")

    assert expand_btn.count() == 1
    assert not full_text.is_visible()

    expand_btn.click()
    assert full_text.is_visible()
    assert long_prompt in full_text.inner_text()

    expand_btn.click()
    assert not full_text.is_visible()

    page.click("#watches-close-btn")


def test_watch_row_shows_real_added_timestamp_not_nan(logged_in_page, live_server):
    """Watch rows show a sane "added ... ago · last checked ... ago" string, never NaN.
    watchers.py's _now() (used for every watch's created_at/updated_at) emits
    `datetime.now(timezone.utc).isoformat()`, i.e. an explicit
    numeric offset suffix like `2026-10-07T10:26:36.961571+00:00`, never a
    bare `Z`; app.js's timeAgo()/fullTimestamp() (via the shared parseIso()
    helper) must parse that correctly instead of appending "Z" blindly.
    This exercises a watch created through the real add_watch() path (so its
    created_at is the real backend-generated timestamp, not a hand-crafted
    stub) and asserts the panel renders a sane "added ... ago" string."""
    import message_store
    import watchers

    conv = message_store.get_or_create_conversation(None)
    watch_id = watchers.add_watch(
        "ticket", conv, key="PRJ-33333", label="Timestamp regression watch", announce=False,
    )

    page = logged_in_page
    page.click("#health-watches")
    page.wait_for_selector(f".watch-item[data-watch-id='{watch_id}']", timeout=5000)

    row_text = page.inner_text(f".watch-item[data-watch-id='{watch_id}'] .watch-meta-secondary")
    assert "NaN" not in row_text
    assert "added just now" in row_text or "added 0m ago" in row_text

    page.click("#watches-close-btn")


def test_watches_background_refresh_keeps_scroll_and_rows_link_without_preview_calls(logged_in_page, live_server):
    """A silent background refresh (loadWatches(true), what the 30s health
    poll calls) must not reset the panel's scroll position, even when the
    list changed and re-rendered; and mr/ticket rows must build their links
    purely from the watch key, never calling /api/watches/{id}/preview."""
    import message_store
    import watchers

    conv = message_store.get_or_create_conversation(None)
    for i in range(30):
        watchers.add_watch("mr", conv, key=f"grp/proj#{1000 + i}", label=f"MR watch {i}", announce=False)
    watchers.add_watch("ticket", conv, key="PRJ-4242", label="Ticket watch", announce=False)

    page = logged_in_page
    preview_calls = []
    page.on("request", lambda r: preview_calls.append(r.url) if "/preview" in r.url else None)

    page.click("#health-watches")
    page.wait_for_selector(".watch-item", timeout=5000)

    hrefs = page.eval_on_selector_all(".watch-preview-link", "els => els.map(e => e.getAttribute('href'))")
    assert "https://gitlab.example.com/grp/proj/-/merge_requests/1000" in hrefs
    assert "https://jira.example.net/browse/PRJ-4242" in hrefs

    scroll_js = """() => {
      const l = document.getElementById('watches-list');
      const p = l.closest('.watches-panel');
      return [l, p].find(e => e.scrollHeight > e.clientHeight) || l;
    }"""
    page.evaluate(f"""() => {{
      const el = ({scroll_js})();
      el.scrollTop = 400;
      window.__scrollEl = el;
    }}""")
    before = page.evaluate("() => window.__scrollEl.scrollTop")
    assert before > 0

    watchers.add_watch("mr", conv, key="grp/proj#9999", label="New MR watch", announce=False)
    page.evaluate("() => loadWatches(true)")
    page.wait_for_function(
        "() => document.getElementById('watches-list').innerText.includes('New MR watch')",
        timeout=5000,
    )
    assert abs(page.evaluate("() => window.__scrollEl.scrollTop") - before) <= 2
    assert not preview_calls, preview_calls
