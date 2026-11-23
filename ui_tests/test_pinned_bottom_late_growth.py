"""A view pinned to the bottom must stay there when rows grow
after render (link previews, diagrams, ...) and while the live status
bubble is patched in place; a view scrolled up must not move."""

from __future__ import annotations

import threading
import time

import message_store

GROW = """() => { const m = document.getElementById('messages');
 new MutationObserver(() => { const rows = m.querySelectorAll('.msg-row'); const r = rows[rows.length - 2];
   if (r && !r.dataset.g) { r.dataset.g = '1'; const d = document.createElement('div'); d.style.height = '400px'; r.appendChild(d); } }
 ).observe(m, { childList: true }); }"""


def _start_running_session(page, n=25):
    page.click("#new-session-btn")
    for i in range(n):
        page.fill("#compose-text", f"filler {i}")
        page.click("#compose-form button[type=submit]")
        page.wait_for_selector(f"#messages >> text=echo: filler {i}", timeout=5000)
    cid = page.eval_on_selector(".session-item.active", "el => el.dataset.conversationId")
    page.fill("#compose-text", "SLOW_TURN go")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector("#stop-turn-btn[style*='block']", timeout=5000)
    return cid


def _feed(cid, seconds):
    end = time.time() + seconds
    i = 0
    while time.time() < end:
        i += 1
        message_store.record_message(cid, "status", f"_:stopwatch: {i * 3}s elapsed_", message_id="status-feed")
        if i % 3 == 0:
            message_store.record_message(cid, "assistant", f"progress {i}")
        time.sleep(0.4)


def test_stays_pinned_through_late_row_growth(logged_in_page, live_server):
    page = logged_in_page
    cid = _start_running_session(page)
    page.evaluate(GROW)
    t = threading.Thread(target=_feed, args=(cid, 4))
    t.start()
    t.join()
    time.sleep(0.5)
    distance = page.eval_on_selector("#messages", "m => m.scrollHeight - m.scrollTop - m.clientHeight")
    assert distance <= 48, f"view stranded {distance}px above the bottom"


def test_scrolled_up_view_does_not_move(logged_in_page, live_server):
    page = logged_in_page
    cid = _start_running_session(page)
    page.evaluate(GROW)
    page.hover("#messages")
    page.mouse.wheel(0, -100000)
    time.sleep(0.5)
    page.mouse.wheel(0, 300)
    time.sleep(0.5)
    before = page.eval_on_selector("#messages", "m => m.scrollTop")
    assert before > 100, "test setup: should be mid-transcript"
    assert page.eval_on_selector("#messages", "m => m.scrollHeight - m.scrollTop - m.clientHeight") > 200
    t = threading.Thread(target=_feed, args=(cid, 4))
    t.start()
    t.join()
    time.sleep(0.3)
    assert abs(page.eval_on_selector("#messages", "m => m.scrollTop") - before) <= 2
