"""
Test that a markdown link whose URL contains internal parentheses still
renders correctly when the assistant model drops the link's own closing
")" - seen with real Wiz resource/posture-issue URLs, which embed a
literal parenthesized hash fragment like
"#~(entity~(~'<uuid>*2cVIRTUAL_MACHINE))", most often inside a markdown
table cell where the model miscounts parens against the "|"
column delimiter.

marked's own link tokenizer isn't a strict paren-depth counter, so on an
unbalanced "[Wiz](https://.../(a(b))" (missing the link's own closing ")")
it falls back to its GFM autolinker, leaving literal "[Wiz](" text in the
bubble followed by a link whose visible text AND href are both the raw,
truncated URL.

repairUnbalancedMarkdownLinks() in webui/app.js paren-depth-counts the URL
after "](", and if depth never returns to 0 before a "|" (table cell
boundary), a newline, or the end of the string, appends the missing ")"
characters right there - reconstructing the well-formed link marked
renders correctly, without swallowing any following link past the same
line/cell.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

_BOT_DIR = Path(__file__).resolve().parent.parent
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))

import message_store  # noqa: E402

REAL_BROKEN_WIZ_TABLE = (
    "| instance | Findings | Crit/High | Packages to update | Link to report | SFM's |\n"
    "|---|---|---|---|---|---|\n"
    "| a-dev-ec2-example-fc14-7-vm-pam-jumpbox-1-server | 6 | 6 | BusyBox, OpenSSL | "
    "[Wiz](https://app.wiz.io/resources#~(entity~(~'9c13baed-859e-59cc-92f4-a21a853017f4*2cVIRTUAL_MACHINE)) | "
    "[SFM-2472](https://jira.example.net/browse/SFM-2472), "
    "[SFM-3954](https://jira.example.net/browse/SFM-3954) |\n"
)

MALFORMED_LINK_AT_END_OF_MESSAGE = (
    "See the report: [Wiz](https://app.wiz.io/resources#~(entity~(~'abc*2cVIRTUAL_MACHINE))"
)


def _seed_session_with_assistant_message(page, text):
    """Seed a fresh session then directly inject an assistant message via
    message_store, bypassing the "echo: " prefix the fake turn-handler adds
    to compose-box sends (which would otherwise corrupt the table's leading
    "|" and break GFM table detection)."""
    page.click("#new-session-btn")
    page.fill("#compose-text", "seed")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(".session-item.active", timeout=5000)
    conversation_id = page.eval_on_selector(".session-item.active", "el => el.dataset.conversationId")
    message_store.record_message(conversation_id, "assistant", text)
    page.wait_for_selector(".bubble.assistant a", timeout=5000)
    time.sleep(0.2)
    return conversation_id


def test_wiz_style_link_with_unbalanced_parens_in_table_renders_as_clean_link(logged_in_page, live_server):
    page = logged_in_page
    _seed_session_with_assistant_message(page, REAL_BROKEN_WIZ_TABLE)

    bubble = page.locator(".bubble.assistant").last
    # No leaked raw markdown/broken-autolink text.
    assert "[Wiz](" not in bubble.inner_text()

    wiz_link = bubble.locator("a", has_text="Wiz")
    assert wiz_link.count() == 1
    assert wiz_link.get_attribute("href") == (
        "https://app.wiz.io/resources#~(entity~(~'9c13baed-859e-59cc-92f4-a21a853017f4*2cVIRTUAL_MACHINE))"
    )

    # The table must still be a real, intact table with the following SFM
    # links unaffected (i.e. the repair must not swallow content past the
    # malformed link's own cell boundary).
    assert bubble.locator("table").count() == 1
    sfm_links = bubble.locator("a", has_text="SFM-")
    assert sfm_links.count() == 2


def test_malformed_link_at_very_end_of_message_still_renders(logged_in_page, live_server):
    """No trailing '|' or newline after the malformed link - the repair must
    still close it at end-of-string instead of leaving it broken."""
    page = logged_in_page
    _seed_session_with_assistant_message(page, MALFORMED_LINK_AT_END_OF_MESSAGE)

    bubble = page.locator(".bubble.assistant").last
    assert "[Wiz](" not in bubble.inner_text()
    wiz_link = bubble.locator("a", has_text="Wiz")
    assert wiz_link.count() == 1
    assert wiz_link.get_attribute("href") == (
        "https://app.wiz.io/resources#~(entity~(~'abc*2cVIRTUAL_MACHINE))"
    )


def test_well_formed_link_with_balanced_parens_in_url_is_unaffected(logged_in_page, live_server):
    """A legitimately balanced link with parens in the
    URL must keep rendering correctly (repair must be a no-op here)."""
    page = logged_in_page
    _seed_session_with_assistant_message(page, "[docs](http://example.com/(section)/intro)")

    bubble = page.locator(".bubble.assistant").last
    link = bubble.locator("a", has_text="docs")
    assert link.count() == 1
    assert link.get_attribute("href") == "http://example.com/(section)/intro"
