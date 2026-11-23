"""
Usage widget (token/AIU consumption in the Web UI header).

Feature: the Web UI shows Copilot token consumption. The health
strip has "today / 7d / 30d AIU" and quota chips with hover popovers
(daily chart, token-type donut, top sessions as title links), and the thread
header's right block (#thread-meta) shows the open session's AIU, a 🧠 context
chip and a ♨ cache countdown. Data comes from GET /api/usage (stubbed here via
page.route so the test is deterministic and never reads ~/.copilot).

Guarantees:
  * header chips render totals from the API and the quota chip is neutral
    below 80% used (no colour class) and amber from 80%;
  * hovering the usage chip shows the top-sessions table whose title is a
    link to #open=<conversation_id>;
  * the thread-meta block still starts with the existing "N turn(s)" text and
    gets 🧠 / ♨ chips whose icons are grayscale;
  * USAGE_WIDGET=off ({"enabled": false}) hides the chips entirely.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

CONV = "00000000-0000-0000-0000-000000000001"


def _payload(used=25000, conv_id=CONV):
    expires = (datetime.now(timezone.utc) + timedelta(minutes=4)).isoformat().replace("+00:00", "Z")
    return {
        "enabled": True, "indexing": False, "sessions_count": 364,
        "totals": {"today": 1572, "d7": 33579, "d30": 200067},
        "ranges": {
            "1": {"unit": "hour", "token_split": [{"type": x, "tokens": 1, "aiu": 1, "pct": 70 if x == "output" else 10} for x in ("input", "cache_read", "cache_write", "output")], "series": [{"day": f"h{i}", "aiu": i} for i in range(24)], "top": [{"session_id": "s1", "aiu": 5, "title": "Today only session", "conversation_id": conv_id, "kind": "human"}]},
            "7": {"unit": "day", "series": [{"day": f"d{i}", "aiu": i} for i in range(7)], "top": [{"session_id": "s1", "aiu": 50, "title": "Week session", "conversation_id": conv_id, "kind": "human"}]},
            "30": {"unit": "day", "series": [{"day": f"d{i}", "aiu": i} for i in range(30)], "top": [{"session_id": "s1", "aiu": 16618, "title": "New UI features", "conversation_id": conv_id, "kind": "human"}]},
        },
        "burn": {"avg7": 4800, "avg30": 6700, "projection30": 144000},
        "token_split": [{"type": t, "tokens": 1, "aiu": 1, "pct": 25} for t in ("input", "cache_read", "cache_write", "output")],
        "quota": {"entitlement": 50000, "used": used, "unlimited": False, "remaining_pct": 100 - used / 500, "used_pct": used / 500, "overage": 0, "reset_at": "2026-10-30T09:58:00Z"},
        "session": {"aiu": 3400, "context": {"currentTokens": 58684, "tokenLimit": 200000, "systemTokens": 8186, "toolDefinitionsTokens": 26030, "conversationTokens": 24465, "source": "live"},
                    "cache": {"expires_at": expires, "ttl_seconds": 300}},
    }


def _stub(page, payload):
    page.route("**/api/usage*", lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(payload)))


def _seed_session(page):
    page.click("#new-session-btn")
    page.fill("#compose-text", "hello usage")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(".session-item.active", timeout=5000)
    time.sleep(0.3)


def test_header_chips_show_totals_and_quota_is_neutral_below_80_percent(logged_in_page):
    page = logged_in_page
    _stub(page, _payload(used=25000))
    page.reload()
    page.wait_for_selector("#health-usage b", timeout=5000)
    text = page.inner_text("#health-usage")
    assert "1.6k" in text and "33.6k" in text and "200k" in text
    assert "25.0k" in page.inner_text("#health-quota") or "25k" in page.inner_text("#health-quota")
    assert page.locator("#health-quota b.uw-quota-hi, #health-quota b.uw-quota-crit").count() == 0


def test_quota_chip_turns_soft_amber_from_80_percent_used(logged_in_page):
    page = logged_in_page
    _stub(page, _payload(used=42000))
    page.reload()
    page.wait_for_selector("#health-quota b.uw-quota-hi", timeout=5000)


def test_top_sessions_popover_lists_title_as_open_link(logged_in_page):
    page = logged_in_page
    _stub(page, _payload())
    page.reload()
    page.wait_for_selector("#health-usage b", timeout=5000)
    page.hover("#health-usage")
    page.click("#health-usage .uw-range-btn[data-range='30']")
    link = page.locator("#health-usage .uw-pop a", has_text="New UI features")
    link.wait_for(state="visible", timeout=3000)
    assert link.get_attribute("href") == f"#open={CONV}"


def test_thread_meta_gets_context_and_cache_chips_with_grayscale_icons(logged_in_page):
    page = logged_in_page
    _seed_session(page)
    conv = page.evaluate("currentConversationId")
    _stub(page, _payload(conv_id=conv))
    page.evaluate("window.usageWidget.refresh()")
    page.wait_for_selector("#thread-meta .uw-cache", timeout=5000)
    text = page.inner_text("#thread-meta")
    assert "1 turn" in text and "58.7k/200k" in text
    assert page.eval_on_selector("#thread-meta .uw-ico", "el => getComputedStyle(el).filter") == "grayscale(1)"
    page.hover("#thread-meta .uw-hov")
    assert page.locator("#thread-meta .uw-pop h4", has_text="Selected session").first.is_visible()


def test_disabled_flag_hides_all_usage_chips(logged_in_page):
    page = logged_in_page
    _stub(page, {"enabled": False})
    page.reload()
    page.wait_for_timeout(800)
    assert not page.locator("#health-usage").is_visible()
    assert not page.locator("#health-quota").is_visible()


def test_range_switch_defaults_to_1d_and_switches_top_sessions_and_chart(logged_in_page):
    """Hover report of the today/7d/30d chip has 1d/7d/30d tabs, 1d selected by default:
    1d shows 24 hourly bars + today's top sessions, 7d/30d switch both to those ranges."""
    page = logged_in_page
    _stub(page, _payload())
    page.reload()
    page.wait_for_selector("#health-usage b", timeout=5000)
    page.hover("#health-usage")
    assert page.locator("#health-usage .uw-range-btn.uw-active").inner_text() == "1d"
    assert page.locator("#health-usage .uw-chart i").count() == 24
    assert page.locator("#health-usage .uw-pop a", has_text="Today only session").is_visible()
    page.click("#health-usage .uw-range-btn[data-range='7']")
    assert page.locator("#health-usage .uw-chart i").count() == 7
    assert page.locator("#health-usage .uw-pop a", has_text="Week session").is_visible()
    page.click("#health-usage .uw-range-btn[data-range='30']")
    assert page.locator("#health-usage .uw-chart i").count() == 30


def test_usage_popovers_suppress_the_inherited_native_title_tooltips(logged_in_page):
    """Hovering the cache_read/input/... hints inside the usage popover shows only the custom hint, not
    the browser's native "Bot health status" tooltip (inherited from #health-strip's title). An empty
    title on the chip elements (health strip chips and thread-header
    chips) stops the inheritance."""
    page = logged_in_page
    _stub(page, _payload())
    page.reload()
    page.wait_for_selector("#health-usage b", timeout=5000)
    assert page.get_attribute("#health-usage", "title") == ""
    assert page.get_attribute("#health-quota", "title") == ""
    _seed_session(page)
    conv = page.evaluate("currentConversationId")
    _stub(page, _payload(conv_id=conv))
    page.evaluate("window.usageWidget.refresh()")
    page.wait_for_selector("#thread-meta .uw-hov", timeout=5000)
    assert page.get_attribute("#thread-meta .uw-hov", "title") == ""


def test_token_donut_follows_the_selected_range(logged_in_page):
    """The token-type donut (input/cache_read/cache_write/output) follows the selected 1d/7d/30d range: each
    range carries its own split; 1d is output-heavy in
    this stub, 7d falls back to the global split."""
    page = logged_in_page
    _stub(page, _payload())
    page.reload()
    page.wait_for_selector("#health-usage b", timeout=5000)
    page.hover("#health-usage")
    assert "output" in page.inner_text("#health-usage .uw-leg") and "70%" in page.inner_text("#health-usage .uw-leg")
    page.click("#health-usage .uw-range-btn[data-range='7']")
    assert "70%" not in page.inner_text("#health-usage .uw-leg")


def test_quota_chip_shows_reset_countdown_and_popover_shows_local_reset_time_and_age(logged_in_page):
    """The quota chip reads 'resets in Nd', the
    popover shows the exact local reset time, 'updated N min ago' and a 'refresh now' link that re-fetches with
    refresh=1."""
    page = logged_in_page
    payload = _payload(used=25000)
    payload["quota"]["reset_at"] = (datetime.now(timezone.utc) + timedelta(days=10, hours=5)).isoformat().replace("+00:00", "Z")
    payload["quota"]["fetched_at"] = time.time() - 12 * 60
    payload["quota"]["ttl_seconds"] = 3600
    urls = []
    page.route("**/api/usage*", lambda route: (urls.append(route.request.url), route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))))
    page.reload()
    page.wait_for_selector("#health-quota b", timeout=5000)
    assert "resets in 10d" in page.inner_text("#health-quota b")
    html = page.inner_html("#health-quota .uw-pop")
    assert "Resets:" in html and "updated 12 min ago" in html
    page.evaluate("document.querySelector('.uw-quota-refresh').click()")
    page.wait_for_function("() => true")
    time.sleep(0.5)
    assert any("refresh=1" in u for u in urls)
