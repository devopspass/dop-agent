"""
Playwright test for the watcher-noise views (Today / Smart /
Messages only) and the "While you were away" digest.

Feature: in a session with many watchers, registrations, fire notices and
the agent's one-line replies to them can drown the meaningful messages. The
thread header has a Today / Smart / Messages-only toggle:
  * Today          - every stored message;
  * Smart (default)- routine watcher events folded into chips / a collapsed
                     "N routine events" group; reports and failures stay
                     normal bubbles; clicking a chip shows the FULL original
                     notice (and the agent's reply) like Today does;
  * Messages only  - just the conversation (user, agent, errors).
The seeded transcript below is: user ask -> watcher registered -> two routine
fires answered "ACK" -> a pipeline-green fire answered with a real report ->
agent message. Smart must hide the ACK replies but keep the report.
"""

from __future__ import annotations

import message_store

PIPE_OK = ":white_check_mark: *GitLab MR watch fired — its pipeline* — MR !5\nPipeline #77: `success`\nhttps://gl/x/-/pipelines/77"
NOTE = ":twisted_rightwards_arrows: *GitLab MR watch fired* — MR !5\n\nNew note(s):\n• danger: routine comment"
REPORT = "Pipeline is green for [!5](https://gl/mr/5).\n\n| MR | State |\n|---|---|\n| !5 | ready |\n\nNext: please review."


def _seed():
    cid = message_store.get_or_create_conversation(None)
    rec = message_store.record_message
    rec(cid, "user", "watch my MR and tell me when done")
    rec(cid, "system", ":new: *New watcher registered* — MR !5")
    rec(cid, "system", NOTE)
    rec(cid, "assistant", "ACK - bot note")
    rec(cid, "system", NOTE)
    rec(cid, "assistant", "ACK")
    rec(cid, "system", PIPE_OK)
    rec(cid, "assistant", REPORT)
    return cid


def _bubbles(page):
    return page.locator("#messages .bubble").all_inner_texts()


def test_smart_view_folds_routine_events_keeps_report_and_chip_expands(logged_in_page, live_server):
    page = logged_in_page
    cid = _seed()
    page.goto(f"{live_server}/#open={cid}")
    page.reload()
    page.wait_for_selector("#view-seg")

    # Today: everything is a bubble (user + 4 system + 3 assistant... all 8)
    page.click("#view-seg button[data-mode=raw]")
    assert page.locator("#messages .bubble").count() == 8

    # Smart (default for fresh browsers; click explicitly): ACK replies and routine
    # notices are folded; the pipeline fire + its report stay visible.
    page.click("#view-seg button[data-mode=smart]")
    texts = " ".join(_bubbles(page))
    assert "Pipeline is green" in texts
    assert "ACK" not in texts
    assert page.locator("#messages .watch-group").count() == 1  # registration + 2 routine fires

    # Clicking a chip reveals the full original notice, like Today view does.
    page.locator("#messages .watch-group > summary").click()
    chip = page.locator("#messages .watch-group .watch-chip-click").first
    chip.click()
    page.wait_for_selector("#messages .watch-preview .bubble.system")
    assert "danger" in page.locator("#messages .watch-preview").first.inner_text() or "New watcher" in page.locator("#messages .watch-preview").first.inner_text()

    # Messages only: no system bubbles / chips at all, report still present.
    page.click("#view-seg button[data-mode=msgs]")
    assert page.locator("#messages .bubble.system").count() == 0
    assert page.locator("#messages .watch-chip").count() == 0
    assert "Pipeline is green" in " ".join(_bubbles(page))
