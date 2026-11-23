"""
Playwright test that markdown tables never render "endlessly" wide in chat
bubbles (webui/style.css):
  1. `.bubble:has(table)` is bounded by `max-width: min(94%, 1600px)`
     (intentionally wider than the plain-text cap `min(94%, 1200px)`, since
     tables benefit from more room), so it can't grow arbitrarily wide on
     ultrawide monitors.
  2. Table cells wrap (`white-space: normal`, `overflow-wrap: break-word` /
     `word-break: break-word`) and `<table>` has `max-width: 100%`, so a
     long-prose column or a single long unbroken token (e.g. a bare file
     path) cannot push the table's intrinsic width past the bubble, where
     `overflow-x: auto` would make it silently scrollable with no visible
     affordance.

  `table-layout: fixed` is deliberately not used: without explicit
  per-column widths it distributes widths roughly equally regardless of
  content, which looks bad for a narrow "#" column next to a wide prose
  column. `table-layout: auto` sizes columns by content while respecting
  `max-width: 100%`.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

_BOT_DIR = Path(__file__).resolve().parent.parent
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))

import message_store  # noqa: E402

LONG_PROSE_TABLE = (
    "| # | What I found |\n"
    "| --- | --- |\n"
    "| 1 | " + ("This is a very long prose sentence describing a finding in detail. " * 6) + " |\n"
    "| 2 | Second row with a long unbroken token: "
    "terraform-aws-userdata/files/some/very/long/nested/path/userdata.sh |\n"
)


def _seed_conversation_with_table(page):
    page.click("#new-session-btn")
    page.fill("#compose-text", "seed")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(".session-item.active", timeout=5000)
    conversation_id = page.eval_on_selector(".session-item.active", "el => el.dataset.conversationId")
    message_store.record_message(conversation_id, "assistant", LONG_PROSE_TABLE)
    page.wait_for_selector(".bubble.assistant table", timeout=5000)
    time.sleep(0.2)
    return conversation_id


def test_table_with_long_prose_cell_wraps_and_fits_bubble_width(logged_in_page, live_server):
    page = logged_in_page
    page.set_viewport_size({"width": 1440, "height": 900})
    _seed_conversation_with_table(page)

    result = page.evaluate(
        """
        () => {
          const table = document.querySelector('.bubble.assistant table');
          const bubble = table.closest('.bubble');
          const td = table.querySelector('td');
          const cs = getComputedStyle(td);
          return {
            td_white_space: cs.whiteSpace,
            td_overflow_wrap: cs.overflowWrap,
            table_width: table.getBoundingClientRect().width,
            bubble_scroll_width: bubble.scrollWidth,
            bubble_client_width: bubble.clientWidth,
          };
        }
        """
    )

    assert result["td_white_space"] == "normal", "table cells must wrap instead of forcing nowrap"
    assert result["td_overflow_wrap"] == "break-word"
    # No hidden horizontal overflow: the table must actually fit within the
    # bubble's own visible width, not just be scrollable past it.
    assert result["bubble_scroll_width"] <= result["bubble_client_width"] + 1, (
        "table content overflows the bubble horizontally "
        f"(scrollWidth={result['bubble_scroll_width']} > "
        f"clientWidth={result['bubble_client_width']})"
    )


def test_bubble_with_table_has_bounded_max_width(logged_in_page, live_server):
    """Bubbles containing tables must still be capped, not allowed to grow
    unboundedly wide on large viewports."""
    page = logged_in_page
    page.set_viewport_size({"width": 2200, "height": 900})
    _seed_conversation_with_table(page)

    bubble_max_width = page.eval_on_selector(
        ".bubble.assistant", "el => getComputedStyle(el).maxWidth"
    )
    assert "1600px" in bubble_max_width or "none" not in bubble_max_width
