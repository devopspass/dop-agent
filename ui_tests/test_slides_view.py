"""Playwright tests for the slides view (webui/app.js applySlides/splitSections)."""

from __future__ import annotations

import time

LONG = (
    "intro line\n\n## Alpha\n\n" + "alpha text. " * 30 + "\n\n```\n# not a header\n```\n\n"
    "## Beta\n\nbeta text\n\n## Gamma\n\ngamma text\n"
)


def _send(page, text):
    page.click("#new-session-btn")
    page.fill("#compose-text", text)
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(".msg-row.role-assistant", timeout=5000)


def test_long_reply_is_sliced_with_rail(logged_in_page):
    page = logged_in_page
    page.evaluate("localStorage.removeItem('dopAgentSlidesOff')")
    _send(page, LONG)
    page.wait_for_selector(".slides", timeout=5000)
    assert page.locator(".slides-rail-item").count() == 4  # Intro + 3 headers, fence ignored
    assert page.locator(".slides-slide:not([hidden])").count() == 1
    page.click(".slides-rail-item:nth-child(3)")
    assert "beta text" in page.locator(".slides-slide:not([hidden])").inner_text()
    page.click(".slides-btn:has-text('Next')")
    assert "gamma text" in page.locator(".slides-slide:not([hidden])").inner_text()


def test_state_survives_rerender_and_show_all(logged_in_page):
    page = logged_in_page
    _send(page, LONG)
    page.wait_for_selector(".slides")
    page.click(".slides-rail-item:nth-child(3)")
    page.evaluate("renderMessages(lastTurns, lastRunning)")
    assert "beta text" in page.locator(".slides-slide:not([hidden])").inner_text()
    page.click(".slides-all")
    assert page.locator(".slides-slide:not([hidden])").count() == 4


def test_short_reply_not_sliced_and_global_off(logged_in_page):
    page = logged_in_page
    _send(page, "hello\n\n## One\n\nx\n\n## Two\n\ny")
    time.sleep(0.3)
    assert page.locator(".slides").count() == 0
    _send(page, LONG)
    page.wait_for_selector(".slides")
    page.click("#slides-toggle-btn")
    assert page.locator(".slides").count() == 0
    page.click("#slides-toggle-btn")
    assert page.locator(".slides").count() == 1


def test_keyboard_arrows_survive_rerender(logged_in_page):
    page = logged_in_page
    _send(page, LONG)
    page.wait_for_selector(".slides")
    page.focus(".slides")
    page.keyboard.press("ArrowRight")
    page.keyboard.press("ArrowRight")
    assert "Alpha" not in page.locator(".slides-slide:not([hidden])").inner_text()
    page.evaluate("renderMessages(lastTurns, lastRunning)")
    page.wait_for_timeout(100)
    page.keyboard.press("ArrowRight")
    assert "gamma text" in page.locator(".slides-slide:not([hidden])").inner_text()


def test_hover_arrows_and_inline_markup_rail(logged_in_page):
    page = logged_in_page
    _send(page, "intro\n\n## A `code` [link](https://x.y)\n\nfoo\n\n## B\n\nbar\n\n## C\n\nbaz")
    page.wait_for_selector(".slides")
    assert "A code link" in page.locator(".slides-rail-item").first.inner_text() or \
        "A code link" in page.locator(".slides-rail-item").nth(1).inner_text()
    page.evaluate("document.activeElement.blur()")
    page.hover(".slides")
    page.keyboard.press("ArrowRight")
    assert page.locator(".slides-count").inner_text().startswith("2 /")


def test_mobile_rail_becomes_chips(logged_in_page):
    page = logged_in_page
    page.set_viewport_size({"width": 600, "height": 800})
    _send(page, LONG)
    page.wait_for_selector(".slides")
    assert page.eval_on_selector(".slides-rail", "e => getComputedStyle(e).display") == "flex"


def test_deep_link_to_sliced_message_scrolls_to_row(logged_in_page):
    page = logged_in_page
    _send(page, LONG)
    page.wait_for_selector(".slides")
    mid = page.eval_on_selector(".msg-row.role-assistant", "e => e.id.slice(4)")
    page.evaluate(f"pendingDeepLinkMessageId = '{mid}'; renderMessages(lastTurns, lastRunning)")
    page.wait_for_selector(f"#msg-{mid}.msg-row-deep-link-highlight", timeout=3000)
    assert page.locator(".slides").count() == 1


def test_bold_only_lines_count_as_headers(logged_in_page):
    page = logged_in_page
    _send(page, "x\n\n## Title\n\n" + "intro text. " * 30 + "\n\n**First part**\n\nfoo\n\n**Second part**\n\nbar\n")
    page.wait_for_selector(".slides")
    assert page.locator(".slides-rail-item").count() == 4


def test_two_sections_still_get_rail(logged_in_page):
    page = logged_in_page
    _send(page, "x\n\n## One\n\n" + "text. " * 120 + "\n\n## Two\n\nbar")
    page.wait_for_selector(".slides")
    assert page.locator(".slides-rail-item").count() == 3
    assert page.locator(".slides-rail").is_visible()
