"""
Playwright tests for the resizable/collapsible sidebar. See webui/index.html's #sidebar/#sidebar-resize-handle/
#sidebar-collapse-btn and webui/app.js's applySidebarWidth()/
setSidebarCollapsed()/the mousedown-mousemove-mouseup drag handlers.
"""

from __future__ import annotations

import time


def _sidebar_width(page):
    return page.eval_on_selector("#sidebar", "el => el.getBoundingClientRect().width")


def test_dragging_the_resize_handle_changes_and_persists_sidebar_width(logged_in_page, live_server):
    page = logged_in_page

    initial_width = _sidebar_width(page)
    assert initial_width > 0

    handle = page.locator("#sidebar-resize-handle")
    box = handle.bounding_box()
    start_x = box["x"] + box["width"] / 2
    start_y = box["y"] + box["height"] / 2

    page.mouse.move(start_x, start_y)
    page.mouse.down()
    page.mouse.move(start_x + 120, start_y, steps=5)
    page.mouse.up()
    time.sleep(0.1)

    resized_width = _sidebar_width(page)
    assert resized_width > initial_width + 50

    # Persisted across reload (allow the width transition to settle).
    page.reload()
    page.wait_for_selector("#sidebar", timeout=5000)
    time.sleep(0.5)
    reloaded_width = _sidebar_width(page)
    assert abs(reloaded_width - resized_width) < 5


def test_resize_is_clamped_to_min_and_max_width(logged_in_page, live_server):
    page = logged_in_page

    handle = page.locator("#sidebar-resize-handle")
    box = handle.bounding_box()
    start_x = box["x"] + box["width"] / 2
    start_y = box["y"] + box["height"] / 2

    # Drag far past the max (600px) - should clamp, not grow unbounded.
    page.mouse.move(start_x, start_y)
    page.mouse.down()
    page.mouse.move(2000, start_y, steps=5)
    page.mouse.up()
    time.sleep(0.1)
    assert _sidebar_width(page) <= 601

    # Drag far past the min (180px) via the handle's new position - should
    # clamp, not shrink to zero/negative.
    new_box = page.locator("#sidebar-resize-handle").bounding_box()
    page.mouse.move(new_box["x"], new_box["y"])
    page.mouse.down()
    page.mouse.move(0, new_box["y"], steps=5)
    page.mouse.up()
    time.sleep(0.1)
    assert _sidebar_width(page) >= 179


def test_collapse_button_hides_sidebar_and_persists_across_reload(logged_in_page, live_server):
    page = logged_in_page

    assert "collapsed" not in (page.get_attribute("#sidebar", "class") or "")
    page.click("#sidebar-collapse-btn")
    time.sleep(0.3)
    assert "collapsed" in page.get_attribute("#sidebar", "class")
    assert _sidebar_width(page) == 0

    page.reload()
    page.wait_for_selector("#sidebar", timeout=5000)
    time.sleep(0.2)
    assert "collapsed" in page.get_attribute("#sidebar", "class")

    # Expand again clears the persisted collapsed state.
    page.click("#sidebar-collapse-btn")
    time.sleep(0.3)
    assert "collapsed" not in (page.get_attribute("#sidebar", "class") or "")
    assert _sidebar_width(page) > 0
