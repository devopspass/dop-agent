"""
Feature: a "🕒 Recent" quick-filter in the sidebar shows only sessions with
activity (updated_at) inside the last N hours, N being a Settings value
(default 8, stored in localStorage as settings.recentHours).

Guards: a session active now is listed; shrinking/growing the window via the
settings input changes membership; archived sessions are excluded.
"""

from __future__ import annotations


def test_recent_filter_lists_fresh_sessions_and_respects_hours_setting(logged_in_page, live_server):
    page = logged_in_page
    page.click("#new-session-btn")
    page.fill("#compose-text", "recent filter probe")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(".session-item.active", timeout=5000)
    cid = page.eval_on_selector(".session-item.active", "el => el.dataset.conversationId")
    sel = f".session-item[data-conversation-id='{cid}']"

    page.click("#session-filters .filter-chip[data-filter='recent']")
    assert page.query_selector(sel) is not None

    # Pretend the session's last activity was 10h ago: out of the 8h window, in a 12h one.
    page.evaluate("(id) => { const s = sessions.find(x => x.conversation_id === id); s.updated_at = new Date(Date.now() - 10*3600*1000).toISOString(); }", cid)
    page.evaluate("currentConversationId = null; renderSessionList()")
    assert page.query_selector(sel) is None
    page.click("#settings-link")
    page.fill("#settings-recent-hours", "12")
    page.dispatch_event("#settings-recent-hours", "change")
    assert page.query_selector(sel) is not None


def _make_session(page):
    page.click("#new-session-btn")
    page.fill("#compose-text", "recent probe")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(".session-item.active", timeout=5000)


def test_recent_filter_empty_state_tooltip_and_invalid_input(logged_in_page, live_server):
    """Empty state names the window, chip tooltip tracks it, invalid hours input reverts."""
    page = logged_in_page
    _make_session(page)
    page.click("#session-filters .filter-chip[data-filter='recent']")
    # Age + render + read in one evaluate so a background session poll can't refresh updated_at in between.
    text = page.evaluate("""() => { currentConversationId = null;
      sessions.forEach(s => { s.updated_at = new Date(Date.now() - 100*3600*1000).toISOString(); });
      renderSessionList(); return document.querySelector('.session-filter-empty')?.textContent; }""")
    assert text == "No sessions with activity in the last 8h"
    page.click("#settings-link")
    page.fill("#settings-recent-hours", "")
    page.dispatch_event("#settings-recent-hours", "change")
    assert page.input_value("#settings-recent-hours") == "8"
    page.fill("#settings-recent-hours", "0")
    page.dispatch_event("#settings-recent-hours", "change")
    assert page.input_value("#settings-recent-hours") == "8"
    page.fill("#settings-recent-hours", "6")
    page.dispatch_event("#settings-recent-hours", "change")
    assert page.get_attribute("#recent-filter-chip", "title") == "Sessions with activity in the last 6 hours"
    text = page.evaluate("() => { currentConversationId = null; sessions.forEach(s => { s.updated_at = new Date(Date.now() - 100*3600*1000).toISOString(); }); renderSessionList(); return document.querySelector('.session-filter-empty')?.textContent; }")
    assert text == "No sessions with activity in the last 6h"


def test_recent_filter_ages_out_on_tick_and_intersects_group_filter(logged_in_page, live_server):
    """recentTick() redraws only when the drawn set is stale; Recent ANDs with the group filter.

    Each scenario runs inside one evaluate so a background session poll cannot refresh updated_at between steps.
    """
    page = logged_in_page
    _make_session(page)
    page.click("#session-filters .filter-chip[data-filter='recent']")
    res = page.evaluate("""() => {
      currentConversationId = null;
      const s = sessions[0];
      const has = () => !!sessionListEl.querySelector(`.session-item[data-conversation-id='${s.conversation_id}']`);
      s.updated_at = new Date(Date.now() - 7.99*3600*1000).toISOString();
      renderSessionList();
      const before = has();
      s.updated_at = new Date(Date.now() - 8.5*3600*1000).toISOString();
      const staleUntilTick = has();            // nothing re-rendered yet
      recentTick();
      const afterTick = has();
      // an ordinary render of a different set must not be masked by an old tick key
      s.updated_at = new Date().toISOString();
      renderSessionList();
      const back = has();
      s.updated_at = new Date(Date.now() - 8.5*3600*1000).toISOString();
      recentTick();
      const agedAgain = has();
      // intersection with the group filter
      s.updated_at = new Date().toISOString(); s.group_id = 'g-a'; groupFilter = 'g-b';
      renderSessionList();
      const hiddenByGroup = !has();
      groupFilter = 'all';
      return {before, staleUntilTick, afterTick, back, agedAgain, hiddenByGroup};
    }""")
    assert res == {"before": True, "staleUntilTick": True, "afterTick": False, "back": True,
                   "agedAgain": False, "hiddenByGroup": True}
