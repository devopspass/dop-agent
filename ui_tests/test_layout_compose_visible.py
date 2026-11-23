"""
Playwright test for the compose box being fully visible/usable
even with the header health strip present.

`body` is `height:100vh` with the health-strip and `.layout` as siblings, so
`.thread-view` (nested inside `.layout` > `.main`) must not set its own 100vh
height; it has `min-height: 0` and shrinks to the flex-allotted space below
the health strip (see webui/style.css), keeping the compose form (textarea +
Send button) inside the viewport.
"""

from __future__ import annotations


def test_compose_send_button_fully_within_viewport(logged_in_page, live_server):
    page = logged_in_page
    page.click("#new-session-btn")
    page.fill("#compose-text", "hello there")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector("#messages >> text=echo: hello there", timeout=5000)

    # The health strip must be present, otherwise the test would pass trivially.
    assert page.is_visible("#health-strip")

    send_box = page.eval_on_selector(
        "#compose-form button[type=submit]",
        "el => { const r = el.getBoundingClientRect(); "
        "return {top: r.top, bottom: r.bottom, height: r.height}; }",
    )
    viewport = page.viewport_size
    assert send_box["height"] > 0, "Send button must have a non-zero rendered height"
    assert send_box["top"] >= 0, "Send button must not be pushed above the viewport"
    assert send_box["bottom"] <= viewport["height"], (
        f"Send button bottom ({send_box['bottom']}) must be within the viewport "
        f"height ({viewport['height']}) - regression for the compose box being "
        "hidden below the fold after the health strip was added"
    )
    assert page.is_visible("#compose-form button[type=submit]")


def test_thread_view_does_not_overflow_available_height(logged_in_page, live_server):
    page = logged_in_page
    page.click("#new-session-btn")
    page.fill("#compose-text", "hello there")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector("#messages >> text=echo: hello there", timeout=5000)

    health_strip_height = page.eval_on_selector(
        "#health-strip", "el => el.getBoundingClientRect().height"
    )
    thread_view_rect = page.eval_on_selector(
        "#thread-view",
        "el => { const r = el.getBoundingClientRect(); return {top: r.top, bottom: r.bottom}; }",
    )
    viewport = page.viewport_size
    # .thread-view must fit within the space left after the health strip,
    # not claim a full extra 100vh on top of it.
    assert thread_view_rect["top"] >= health_strip_height - 1
    assert thread_view_rect["bottom"] <= viewport["height"] + 1
