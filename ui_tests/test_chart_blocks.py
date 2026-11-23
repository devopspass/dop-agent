"""Markdown ```chart fences render as Chart.js charts (vendored lib, offline)."""
import uuid

DONUT = "see:\n```chart\ntype: donut\ntitle: AIU spend\ncenter: 36%\ncards: Total=194k, Watcher=71k\ntable: yes\ndata:\n  - Watcher: 71\n  - Interactive: 123\n```\n"
BURN = "```chart\ntype: burndown\nx: D1, D2, D3\nideal: 10 -> 0\ndata:\n  - Remaining: 10, 7, 4\n```\n"
BAD = "```chart\ntype: nope\ndata:\n  - A: 1\n```\n"
JSON_BODY = '```chart\n{"type":"bar","x":"a,b","data":[{"label":"S","values":[1,2]}]}\n```\n'


def _send(page, text):
    marker = f"chart-marker-{uuid.uuid4().hex[:8]}"
    page.click("#new-session-btn")
    page.fill("#compose-text", f"{marker}\n{text}")
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(f".bubble.assistant:has-text('{marker}')", timeout=8000)


def test_donut_renders_canvas_cards_legend_table(logged_in_page):
    page = logged_in_page
    _send(page, DONUT)
    page.wait_for_selector(".bubble.assistant .chart-block canvas", timeout=8000)
    assert page.locator(".bubble.assistant .chart-card").count() == 2
    assert "36.6%" in page.locator(".bubble.assistant .chart-legend").first.inner_text()
    assert page.locator(".bubble.assistant .chart-table tr").count() == 3
    assert page.locator(".bubble.assistant .chart-block pre").first.is_hidden()
    assert page.evaluate("Chart.getChart(document.querySelector('.bubble.assistant .chart-block canvas')).config.type") == "doughnut"


def test_toggle_shows_source_and_back(logged_in_page):
    page = logged_in_page
    _send(page, DONUT)
    page.wait_for_selector(".bubble.assistant .chart-block canvas", timeout=8000)
    btn = page.locator(".bubble.assistant .chart-bar button").first
    btn.click()
    assert page.locator(".bubble.assistant .chart-block pre").first.is_visible()
    btn.click()
    assert page.locator(".bubble.assistant .chart-block canvas").first.is_visible()


def test_burndown_has_ideal_series_and_json_body_works(logged_in_page):
    page = logged_in_page
    _send(page, BURN + JSON_BODY)
    page.wait_for_selector(".bubble.assistant .chart-block canvas >> nth=1", state="attached", timeout=8000)
    labels = page.evaluate("[...document.querySelectorAll('.bubble.assistant .chart-block canvas')].map(c => Chart.getChart(c).data.datasets.map(d => d.label).join('|'))")
    assert "Remaining|Ideal" in labels and "S" in labels


def test_bad_fence_falls_back_with_note(logged_in_page):
    page = logged_in_page
    _send(page, BAD)
    page.wait_for_selector(".bubble.assistant .chart-note:has-text('unknown type')", timeout=8000)
    assert page.locator(".bubble.assistant .chart-block").count() == 0


ALL = "".join(f"```chart\n{b}\n```\n" for b in [
    "type: pie\ndata:\n  - A: 1\n  - B: 2",
    "type: hbar\ndata:\n  - A: 5\n  - B: 9",
    "type: stacked\nx: a, b\ndata:\n  - S1: 1, 2\n  - S2: 3, 4",
    "type: line\nx: a, b, c\ndata:\n  - L: 1, 3, 2",
    "type: spark\ndata:\n  - W: 1, 3, 2, 5",
])


def test_all_other_types_render_and_survive_rerender(logged_in_page):
    page = logged_in_page
    _send(page, ALL)
    page.wait_for_selector(".bubble.assistant .chart-block svg", timeout=8000)
    page.wait_for_function("document.querySelectorAll('.bubble.assistant .chart-block canvas').length >= 4")
    types = page.evaluate("[...document.querySelectorAll('.bubble.assistant .chart-block canvas')].map(c => Chart.getChart(c).config.type)")
    assert types == ["pie", "bar", "bar", "line"]
    page.screenshot(path="/tmp/i4b-charts/shot.png", full_page=True)
