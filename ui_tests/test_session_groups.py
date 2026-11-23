"""
Playwright coverage for the session-groups UI (webui/groups.js).

Feature: many sessions belong to the same workstream, so the sidebar gets
group chips (filter), an optional collapsible grouped view with a status dot,
drag/context-menu assignment, an in-session banner with prev/next sibling
navigation, and a per-group overview page. A session is in at most one group;
the master is always listed first inside its group.
"""

from __future__ import annotations


def _create_session(page, text):
    page.click("#new-session-btn")
    page.fill("#compose-text", text)
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(f"#messages >> text=echo: {text}", timeout=5000)
    return page.locator(".session-item.active").get_attribute("data-conversation-id")


def _api(page, method, path, body=None):
    return page.evaluate(
        """async ([m, p, b]) => { const r = await fetch(p, {method: m, headers: {'Content-Type': 'application/json'}, body: b ? JSON.stringify(b) : undefined}); return r.json(); }""",
        [method, path, body],
    )


def _setup_group(page):
    a = _create_session(page, "alpha task")
    b = _create_session(page, "beta task")
    c = _create_session(page, "gamma loner")
    g = _api(page, "POST", "/api/groups", {"title": "Workstream X", "emoji": "🚀"})
    _api(page, "PUT", f"/api/sessions/{a}/group", {"group_id": g["id"]})
    _api(page, "PUT", f"/api/sessions/{b}/group", {"group_id": g["id"]})
    _api(page, "PUT", f"/api/groups/{g['id']}/master", {"conversation_id": b})
    page.evaluate("refreshSessionList()")
    page.wait_for_selector(f".group-chip[data-group='{g['id']}']", state="attached", timeout=3000)
    return a, b, c, g["id"]


def _open_dropdown(page):
    """The group filter is a dropdown (not an always-visible chip wall); open it."""
    if page.locator(".group-dropdown-menu").is_hidden():
        page.click(".group-dropdown-toggle")


def _ids(page):
    return page.locator(".session-item").evaluate_all("els => els.map(e => e.dataset.conversationId)")


def test_group_chip_filters_sidebar_and_ungrouped_chip_shows_the_rest(logged_in_page, live_server):
    page = logged_in_page
    a, b, c, gid = _setup_group(page)
    _open_dropdown(page)
    page.click(f".group-chip[data-group='{gid}']")
    ids = _ids(page)
    assert set(ids) == {a, b}
    assert ids[0] == b  # master first
    _open_dropdown(page)
    page.click(".group-chip[data-group='none']")
    ids = _ids(page)  # shared DB: other tests' ungrouped sessions are listed too
    assert c in ids and a not in ids and b not in ids


def test_grouped_view_shows_collapsible_header_with_master_first(logged_in_page, live_server):
    page = logged_in_page
    a, b, c, gid = _setup_group(page)
    page.click(".group-chip[data-action='toggle-view']")
    header = page.locator(f".group-header[data-group-id='{gid}']")
    assert header.count() == 1
    order = page.locator(".session-list > *").evaluate_all(
        "els => els.map(e => e.classList.contains('group-header') ? 'H:' + e.dataset.groupId : e.dataset.conversationId)"
    )
    # (the live server's DB is shared across tests, so other groups may precede it)
    i = order.index(f"H:{gid}")
    assert order[i + 1] == b
    header.click()
    assert page.locator(f".session-item[data-conversation-id='{b}']").count() == 0
    page.locator(f".group-header[data-group-id='{gid}']").click()
    assert page.locator(f".session-item[data-conversation-id='{b}']").count() == 1


def test_context_menu_move_to_group_assigns_session(logged_in_page, live_server):
    page = logged_in_page
    a, b, c, gid = _setup_group(page)
    page.locator(f".session-item[data-conversation-id='{c}']").click(button="right")
    page.click(f".ctx-menu .ctx-item[data-group='{gid}']")
    page.wait_for_function(
        "(id) => sessions.find(s => s.conversation_id === id).group_id", arg=c, timeout=3000
    )


def test_banner_shows_group_with_sibling_navigation_and_overview_opens(logged_in_page, live_server):
    page = logged_in_page
    a, b, c, gid = _setup_group(page)
    page.click(f".session-item[data-conversation-id='{b}']")
    banner = page.locator("#group-banner")
    banner.wait_for(state="visible", timeout=3000)
    assert "Workstream X" in banner.inner_text() and "master" in banner.inner_text()
    banner.locator("[data-nav]").nth(1).click()  # next sibling
    page.wait_for_function("(id) => currentConversationId === id", arg=a, timeout=3000)
    page.click("#group-banner [data-overview]")
    page.wait_for_selector("#group-overview .ov-title", timeout=3000)
    assert "Workstream X" in page.locator("#group-overview").inner_text()
    assert page.locator("#thread-view").is_hidden()
    page.click(f".session-item[data-conversation-id='{a}']")
    page.wait_for_selector("#group-overview", state="hidden", timeout=3000)


def test_drag_session_onto_group_chip_moves_it(logged_in_page, live_server):
    page = logged_in_page
    a, b, c, gid = _setup_group(page)
    _open_dropdown(page)
    # The open dropdown overlays the list, so dispatch the drag events directly.
    dt = page.evaluate_handle("() => new DataTransfer()")
    src = page.locator(f".session-item[data-conversation-id='{c}']")
    src.dispatch_event("dragstart", {"dataTransfer": dt})
    tgt = page.locator(f".group-chip[data-group='{gid}']")
    tgt.dispatch_event("dragover", {"dataTransfer": dt})
    tgt.dispatch_event("drop", {"dataTransfer": dt})
    page.wait_for_function(
        "(id) => sessions.find(s => s.conversation_id === id).group_id", arg=c, timeout=3000
    )


def test_drag_reorders_sessions_inside_grouped_view_and_persists(logged_in_page, live_server):
    """Grouped view: dropping a session on the upper half of a sibling moves it
    above that sibling (manual order saved server-side as group_order), so the
    user can put important sessions higher. The master stays first."""
    page = logged_in_page
    a, b, c, gid = _setup_group(page)  # a, b in group; b is master
    d = _create_session(page, "delta task")
    _api(page, "PUT", f"/api/sessions/{d}/group", {"group_id": gid})
    page.evaluate("refreshSessionList()")
    page.click(".group-chip[data-action='toggle-view']")
    page.wait_for_selector(f".session-item[data-conversation-id='{d}']", timeout=3000)
    ids = [i for i in _ids(page) if i in (a, b, d)]
    assert ids[0] == b
    last = ids[-1]
    first_sibling = ids[1]
    box = page.locator(f".session-item[data-conversation-id='{first_sibling}']").bounding_box()
    dt = page.evaluate_handle("() => new DataTransfer()")
    page.evaluate("([dt, id]) => dt.setData('text/plain', id)", [dt, last])
    page.locator(f".session-item[data-conversation-id='{first_sibling}']").dispatch_event(
        "drop", {"dataTransfer": dt, "clientY": box["y"] + 2})
    page.wait_for_function(
        "(id) => sessions.find(s => s.conversation_id === id).group_order !== null", arg=last, timeout=3000)
    ids2 = [i for i in _ids(page) if i in (a, b, d)]
    assert ids2[0] == b and ids2[1] == last


def test_drag_group_header_reorders_groups_in_grouped_view(logged_in_page, live_server):
    """Grouped view: dragging a group header onto another header moves that
    whole group ahead of it (saved server-side), so the user can put e.g. 'Bot
    development' first. Headers accept both session and group drops."""
    page = logged_in_page
    a, b, c, gid = _setup_group(page)
    g2 = _api(page, "POST", "/api/groups", {"title": "Second WS"})
    d = _create_session(page, "second ws session")
    _api(page, "PUT", f"/api/sessions/{d}/group", {"group_id": g2["id"]})
    page.evaluate("refreshSessionList()")
    page.click(".group-chip[data-action='toggle-view']")
    page.wait_for_selector(f".group-header[data-group-id='{g2['id']}']", timeout=3000)
    order = lambda: page.locator(".group-header[data-group-id]").evaluate_all("els => els.map(e => e.dataset.groupId)")
    assert order().index(gid) < order().index(g2["id"])
    dt = page.evaluate_handle("() => new DataTransfer()")
    page.evaluate("([dt, id]) => dt.setData('application/x-group-id', id)", [dt, g2["id"]])
    page.locator(f".group-header[data-group-id='{gid}']").dispatch_event("drop", {"dataTransfer": dt})
    page.wait_for_function(
        "([a, b]) => { const h = [...document.querySelectorAll('.group-header[data-group-id]')].map(e => e.dataset.groupId); return h.indexOf(a) < h.indexOf(b); }",
        arg=[g2["id"], gid], timeout=3000)
