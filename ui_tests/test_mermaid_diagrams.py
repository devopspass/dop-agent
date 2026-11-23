"""
Playwright tests for mermaid diagram rendering (webui/mermaid.js).

Feature: an assistant message with a fenced ```mermaid block is rendered as an
SVG diagram instead of a code block, with a Diagram/Source toggle and a copy
button. Bad syntax or an unreachable CDN must leave the plain code block in
place (with a note) rather than breaking the message. Slack is unaffected.
The mermaid ESM bundle is stubbed via page.route so tests stay offline: the
stub's render() returns a marker <svg> and throws for sources containing BAD.
The fake echo backend replies "echo: <text>", so sending the fence yields an
assistant bubble containing it.
"""

from __future__ import annotations

import uuid

STUB = """
window.mermaid = {
  initialize() {},
  async render(id, src) {
    if (src.includes('BAD')) throw new Error('Parse error on line 2\\n  details');
    const esc = src.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
    return { svg: '<svg data-stub="1" data-src="' + esc + '" viewBox="0 0 400 200"><g class="node" id="mmd-1-flowchart-A-0"><rect width="40" height="20"/></g><g class="node" id="mmd-1-flowchart-B-1"><rect width="40" height="20"/></g></svg>' };
  },
};
"""
GOOD = "see:\n```mermaid\nflowchart LR\n  A --> B\n```\n"
BAD = "see:\n```mermaid\nBAD diagram\n```\n"


def _stub_cdn(page):
    page.route("**/vendor/mermaid*.js", lambda r: r.fulfill(status=200, content_type="text/javascript", body=STUB))


def _send(page, text):
    # Unique marker so we wait for THIS test's reply, not an older session's
    # bubble that is still on screen while the new session opens.
    marker = f"mmd-marker-{uuid.uuid4().hex[:8]}"
    page.click("#new-session-btn")
    page.fill("#compose-text", f"{marker}\n{text}")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(f".bubble.assistant:has-text('{marker}')", timeout=8000)


def test_mermaid_fence_renders_as_svg_diagram(logged_in_page):
    page = logged_in_page
    _stub_cdn(page)
    _send(page, GOOD)
    page.wait_for_selector(".bubble.assistant .mermaid-viewport svg[data-stub]", timeout=8000)
    assert page.locator(".bubble.assistant .mermaid-block pre").is_hidden()


def test_toggle_switches_between_diagram_and_source_and_survives_rerender(logged_in_page):
    page = logged_in_page
    _stub_cdn(page)
    _send(page, GOOD)
    page.wait_for_selector(".bubble.assistant .mermaid-viewport svg[data-stub]", timeout=8000)
    page.click(".mermaid-toggle")
    assert page.locator(".bubble.assistant .mermaid-block pre").is_visible()
    assert page.locator(".bubble.assistant .mermaid-viewport").is_hidden()
    assert "flowchart LR" in page.locator(".bubble.assistant .mermaid-block pre").inner_text()
    page.click(".mermaid-toggle")
    assert page.locator(".bubble.assistant .mermaid-viewport svg").is_visible()


def test_bad_mermaid_syntax_falls_back_to_code_block_with_note(logged_in_page):
    page = logged_in_page
    _stub_cdn(page)
    _send(page, BAD)
    page.wait_for_selector(".bubble.assistant .mermaid-note", timeout=8000)
    assert page.locator(".bubble.assistant .mermaid-block").count() == 0
    assert "BAD diagram" in page.locator(".bubble.assistant pre code.language-mermaid").inner_text()


def test_unreachable_cdn_falls_back_to_code_block_with_note(logged_in_page):
    page = logged_in_page
    page.route("**/vendor/mermaid*.js", lambda r: r.fulfill(status=200, content_type="text/javascript", body=""))
    _send(page, GOOD)
    page.wait_for_selector(".bubble.assistant .mermaid-note", timeout=8000)
    assert "Could not load" in page.locator(".bubble.assistant .mermaid-note").inner_text()
    assert page.locator(".bubble.assistant pre code.language-mermaid").count() == 1


def test_mermaid_default_theme_vars_are_dark_slack_palette(logged_in_page):
    """Guards the dark Slack-like look: the bundled theme must use the Web UI
    palette (bg #1a1d21, accent #1164a3) so diagrams don't render on white."""
    page = logged_in_page
    css = page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--slack-main-bg').trim()")
    assert css == "#1a1d21"
    src = page.evaluate("fetch('/static/mermaid.js').then(r => r.text())")
    assert "#1a1d21" in src and "#1164a3" in src and "securityLevel: 'strict'" in src


def _tf(page):
    return page.evaluate("document.querySelector('.bubble.assistant .mermaid-stage').style.transform")


def _scale(page):
    import re
    return float(re.search(r"scale\(([\d.]+)\)", _tf(page)).group(1))


def _ready(page, text=GOOD):
    _stub_cdn(page)
    _send(page, text)
    page.wait_for_selector(".bubble.assistant .mermaid-viewport svg[data-stub]", timeout=8000)


def test_zoom_buttons_and_fit_and_natural_size_change_the_scale(logged_in_page):
    """The zoom toolbar zooms in/out and switches between Fit (bubble width) and 100% natural size."""
    page = logged_in_page
    _ready(page)
    assert _scale(page) == 1.0
    page.click(".bubble.assistant .mermaid-zoom-in")
    page.click(".bubble.assistant .mermaid-zoom-in")
    zoomed = _scale(page)
    assert zoomed > 1.0
    page.click(".bubble.assistant .mermaid-fit")
    assert _scale(page) < zoomed
    page.click(".bubble.assistant .mermaid-100")
    assert _scale(page) == 1.0


def test_ctrl_wheel_zooms_but_plain_wheel_does_not(logged_in_page):
    page = logged_in_page
    _ready(page)
    box = page.locator(".bubble.assistant .mermaid-viewport").bounding_box()
    page.mouse.move(box["x"] + 50, box["y"] + 50)
    page.mouse.wheel(0, -100)
    assert _scale(page) == 1.0
    # A plain wheel scrolls the transcript (the view is pinned to the bottom),
    # so re-measure before zooming with the mouse over the diagram again.
    page.wait_for_timeout(200)
    box = page.locator(".bubble.assistant .mermaid-viewport").bounding_box()
    page.mouse.move(box["x"] + 50, box["y"] + 50)
    page.keyboard.down("Control")
    page.mouse.wheel(0, -100)
    page.keyboard.up("Control")
    assert _scale(page) > 1.0


def test_drag_pans_the_diagram(logged_in_page):
    page = logged_in_page
    _ready(page)
    page.wait_for_timeout(800)  # let post-reply re-renders settle so the DOM isn't rebuilt mid-drag
    before = _tf(page)
    box = page.locator(".bubble.assistant .mermaid-viewport").bounding_box()
    page.mouse.move(box["x"] + 100, box["y"] + 60)
    page.mouse.down()
    page.mouse.move(box["x"] + 160, box["y"] + 90, steps=4)
    page.mouse.up()
    assert _tf(page) != before


def test_keyboard_plus_zooms_and_zero_fits(logged_in_page):
    page = logged_in_page
    _ready(page)
    page.focus(".bubble.assistant .mermaid-viewport")
    page.keyboard.press("+")
    assert _scale(page) > 1.0
    page.keyboard.press("1")
    assert _scale(page) == 1.0


def test_fullscreen_overlay_opens_and_escape_closes_it(logged_in_page):
    page = logged_in_page
    _ready(page)
    page.click(".bubble.assistant .mermaid-full")
    page.wait_for_selector(".mermaid-overlay svg[data-stub]")
    page.keyboard.press("Escape")
    page.wait_for_selector(".mermaid-overlay", state="detached")


def test_click_directive_becomes_a_safe_link_and_is_not_sent_to_mermaid(logged_in_page):
    """Mermaid stays in strict mode (click is disabled there), so `click NODE "url"`
    lines are stripped and re-applied as <a> wrappers - only http(s)/# URLs."""
    page = logged_in_page
    text = ('```mermaid\nflowchart LR\n A --> B\n click A "https://example.com/x" _blank\n'
            ' click B "javascript:alert(1)"\n```\n')
    _ready(page, text)
    assert page.locator(".bubble.assistant .mermaid-viewport a[href='https://example.com/x'][target=_blank]").count() == 1
    assert page.locator(".bubble.assistant .mermaid-viewport a[href^='javascript']").count() == 0
    sent = page.get_attribute(".bubble.assistant .mermaid-viewport svg", "data-src")
    assert "click" not in sent


def test_big_diagram_waits_for_render_button(logged_in_page):
    page = logged_in_page
    _stub_cdn(page)
    body = "flowchart LR\n" + "\n".join(f" N{i} --> N{i+1}" for i in range(600))
    _send(page, f"```mermaid\n{body}\n```\n")
    page.wait_for_selector(".bubble.assistant .mermaid-render-btn", timeout=8000)
    assert page.locator(".bubble.assistant .mermaid-block").count() == 0
    page.click(".bubble.assistant .mermaid-render-btn")
    page.wait_for_selector(".bubble.assistant .mermaid-viewport svg[data-stub]")


def test_syntax_error_note_includes_the_first_error_line(logged_in_page):
    page = logged_in_page
    _stub_cdn(page)
    _send(page, BAD)
    page.wait_for_selector(".bubble.assistant .mermaid-note", timeout=8000)
    assert "Parse error on line 2" in page.locator(".bubble.assistant .mermaid-note").inner_text()
