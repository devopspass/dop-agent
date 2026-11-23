"""
Playwright tests for the "auto-unfurl Jira/GitLab links into
preview cards" feature.

The real /api/unfurl endpoint (web_server.py) calls out to the live Jira/
GitLab APIs via gitlab_poll.py/jira_poll.py - that's exercised separately by
tests/test_web_server.py's unit tests (with those library calls mocked).
These UI tests instead intercept the browser's own request to /api/unfurl
via Playwright's page.route(), so they can verify purely the frontend
behavior (enhanceLinkPreviews()/renderUnfurlCard()/wireLazyUnfurl() in
app.js) without any real network access or real Jira/GitLab credentials.

Cards are hidden by default and the /api/unfurl fetch itself is deferred
until the trigger link is first hovered (lazy - see wireLazyUnfurl()) -
a small spinner card floats near the cursor while the request is in
flight, then swaps in place for the real card content once it resolves.
Repeat hovers of the same link reuse the cached result (no refetch). Cards
float near the mouse pointer and follow it as it moves (see
positionUnfurlCardNearCursor() in app.js) - so tests must explicitly hover
before asserting card content/position.

Cards recognize four link shapes (Jira ticket, GitLab MR, GitLab pipeline,
GitLab job), each showing a small app icon (webui/icons/jira.svg or
gitlab.svg) plus emoji for MR state/approval and CI status
(status-emoji sets come from the registry projection, see webui/furls.js).
"""

from __future__ import annotations

import json
import time

JIRA_LINK_MESSAGE = "see https://example.atlassian.net/browse/PROJ-123 for details"
GITLAB_MR_LINK_MESSAGE = "see https://gitlab.example.com/group/project/-/merge_requests/42 for review"
GITLAB_PIPELINE_LINK_MESSAGE = "see https://gitlab.example.com/group/project/-/pipelines/9001 for status"
GITLAB_JOB_LINK_MESSAGE = "see https://gitlab.example.com/group/project/-/jobs/12345 for logs"
PLAIN_LINK_MESSAGE = "see https://example.com/some/other/page for details"


def _generic(body: dict) -> dict:
    """Card fixtures -> the generic /api/unfurl payload (furl_api._payload shape)."""
    t = body.get("type")
    if t == "jira":
        return {**body, "integration": "jira", "driver": "jira", "kind": "ticket", "ident": {"key": body["key"]},
                "title": body["summary"], "status_emoji": "",
                "fields": {"key": body["key"], "summary": body["summary"], "status": body["status"]}}
    if t == "gitlab_mr":
        pipe = body.get("pipeline_status") or ""
        return {**body, "integration": "gitlab", "driver": "gitlab", "kind": "merge_request",
                "ident": {"project": body["project"], "id": body["iid"]}, "status": body["state"],
                "status_emoji": {"opened": "🟢", "merged": "🟣", "closed": "🔴"}.get(body["state"], ""),
                "fields": {"title": body["title"], "state": body["state"], "pipeline": pipe,
                           "approvals": body.get("approved")}}
    if t in ("gitlab_pipeline", "gitlab_job"):
        kind = t.split("_")[1]
        emoji = {"success": "✅", "failed": "❌", "running": "🔄"}.get(body["status"], "")
        title = body.get("name") or f"Pipeline #{body['id']}"
        flds = {"status": body["status"], "project": body["project"], "ref": body.get("ref", ""),
                "name": body.get("name", ""), "stage": body.get("stage", "")}
        return {**body, "integration": "gitlab", "driver": "gitlab", "kind": kind,
                "ident": {"project": body["project"], "id": body["id"]}, "title": title,
                "status_emoji": emoji, "fields": flds}
    return body


def _mock_unfurl_route(page, response_body: dict):
    response_body = _generic(response_body)

    def handler(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps(response_body))

    page.route("**/api/unfurl*", handler)


def _seed_session_with_message(page, text):
    page.click("#new-session-btn")
    page.fill("#compose-text", text)
    page.click("#compose-form button[type=submit]")
    page.wait_for_selector(".session-item.active", timeout=5000)
    time.sleep(0.3)


def test_jira_link_gets_an_unfurl_card_on_hover(logged_in_page, live_server):
    page = logged_in_page
    _mock_unfurl_route(
        page,
        {
            "type": "jira",
            "key": "PROJ-123",
            "url": "https://example.atlassian.net/browse/PROJ-123",
            "summary": "Fix logic of whether feature X is enabled",
            "status": "In Progress",
        },
    )
    _seed_session_with_message(page, JIRA_LINK_MESSAGE)

    card = page.locator(".unfurl-card.unfurl-jira").last

    link = page.locator(".bubble.assistant a.unfurl-link[href='https://example.atlassian.net/browse/PROJ-123']").last
    link.hover()
    card.wait_for(state="visible", timeout=2000)
    assert "PROJ-123" in card.inner_text()
    assert "In Progress" in card.inner_text()
    assert card.locator("img.unfurl-icon[src='/static/icons/jira.svg']").count() == 1


def test_gitlab_mr_link_gets_an_unfurl_card_on_hover(logged_in_page, live_server):
    page = logged_in_page
    _mock_unfurl_route(
        page,
        {
            "type": "gitlab_mr",
            "project": "group/project",
            "iid": "42",
            "url": "https://gitlab.example.com/group/project/-/merge_requests/42",
            "title": "fix: something important",
            "state": "opened",
            "merge_status": "mergeable",
            "pipeline_status": "success",
        },
    )
    _seed_session_with_message(page, GITLAB_MR_LINK_MESSAGE)

    card = page.locator(".unfurl-card.unfurl-gitlab_mr").last

    link = page.locator(
        ".bubble.assistant a.unfurl-link[href='https://gitlab.example.com/group/project/-/merge_requests/42']"
    ).last
    link.hover()
    card.wait_for(state="visible", timeout=2000)
    text = card.inner_text()
    assert "42" in text
    assert "fix: something important" in text
    assert "success" in text
    # App icon should be present in the card title.
    assert card.locator("img.unfurl-icon[src='/static/icons/gitlab.svg']").count() == 1
    # Status emoji: opened -> 🟢, pipeline success -> ✅.
    assert "🟢" in text
    assert "✅" in text
    # Row1 shows the repo name (last path segment of data.project), NOT
    # the MR number/status - the opposite of every other card type (see
    # the layout-exception comment in renderUnfurlCard()). Row2 holds the
    # !iid + status + title instead.
    assert card.locator(".unfurl-row1").inner_text().strip() == "project"
    row2_text = card.locator(".unfurl-row2").inner_text()
    assert "42" in row2_text
    assert "fix: something important" in row2_text


def test_gitlab_mr_link_card_shows_pending_approval_emoji(logged_in_page, live_server):
    page = logged_in_page
    _mock_unfurl_route(
        page,
        {
            "type": "gitlab_mr",
            "project": "group/project",
            "iid": "43",
            "url": "https://gitlab.example.com/group/project/-/merge_requests/43",
            "title": "fix: something else",
            "state": "opened",
            "merge_status": "mergeable",
            "pipeline_status": "failed",
            "approved": False,
        },
    )
    _seed_session_with_message(page, "see https://gitlab.example.com/group/project/-/merge_requests/43 for review")

    card = page.locator(".unfurl-card.unfurl-gitlab_mr").last
    link = page.locator(
        ".bubble.assistant a.unfurl-link[href='https://gitlab.example.com/group/project/-/merge_requests/43']"
    ).last
    link.hover()
    card.wait_for(state="visible", timeout=2000)
    text = card.inner_text()
    assert "⏳ approvals" in text  # generic boolean badge
    assert "❌" in text  # failed pipeline


def test_gitlab_pipeline_link_gets_an_unfurl_card_on_hover(logged_in_page, live_server):
    page = logged_in_page
    _mock_unfurl_route(
        page,
        {
            "type": "gitlab_pipeline",
            "project": "group/subgroup/project",
            "id": "9001",
            "url": "https://gitlab.example.com/group/project/-/pipelines/9001",
            "status": "running",
            "ref": "main",
        },
    )
    _seed_session_with_message(page, GITLAB_PIPELINE_LINK_MESSAGE)

    card = page.locator(".unfurl-card.unfurl-gitlab_pipeline").last

    link = page.locator(
        ".bubble.assistant a.unfurl-link[href='https://gitlab.example.com/group/project/-/pipelines/9001']"
    ).last
    link.hover()
    card.wait_for(state="visible", timeout=2000)
    text = card.inner_text()
    assert "9001" in text
    assert "main" in text
    assert "🔄" in text  # running
    assert card.locator("img.unfurl-icon[src='/static/icons/gitlab.svg']").count() == 1
    # Row1 = repo name only (last segment of a nested "group/subgroup/
    # project" path -> "project"), row2 = pipeline number/status/ref.
    assert card.locator(".unfurl-row1").inner_text().strip() == "project"
    row2_text = card.locator(".unfurl-row2").inner_text()
    assert "9001" in row2_text
    assert "main" in row2_text


def test_gitlab_job_link_gets_an_unfurl_card_on_hover(logged_in_page, live_server):
    page = logged_in_page
    _mock_unfurl_route(
        page,
        {
            "type": "gitlab_job",
            "project": "group/project",
            "id": "12345",
            "url": "https://gitlab.example.com/group/project/-/jobs/12345",
            "status": "failed",
            "name": "test",
            "stage": "test",
        },
    )
    _seed_session_with_message(page, GITLAB_JOB_LINK_MESSAGE)

    card = page.locator(".unfurl-card.unfurl-gitlab_job").last

    link = page.locator(
        ".bubble.assistant a.unfurl-link[href='https://gitlab.example.com/group/project/-/jobs/12345']"
    ).last
    link.hover()
    card.wait_for(state="visible", timeout=2000)
    text = card.inner_text()
    assert "12345" in text
    assert "❌" in text  # failed
    assert card.locator("img.unfurl-icon[src='/static/icons/gitlab.svg']").count() == 1
    # Row1 = repo name, row2 = job number/status/name/stage.
    assert card.locator(".unfurl-row1").inner_text().strip() == "project"
    row2_text = card.locator(".unfurl-row2").inner_text()
    assert "12345" in row2_text
    assert "test" in row2_text


def test_unfurl_card_hides_again_after_mouse_leaves(logged_in_page, live_server):
    page = logged_in_page
    _mock_unfurl_route(
        page,
        {
            "type": "jira",
            "key": "PROJ-123",
            "url": "https://example.atlassian.net/browse/PROJ-123",
            "summary": "Fix logic of whether feature X is enabled",
            "status": "In Progress",
        },
    )
    _seed_session_with_message(page, JIRA_LINK_MESSAGE)

    card = page.locator(".unfurl-card.unfurl-jira").last
    link = page.locator(".bubble.assistant a.unfurl-link[href='https://example.atlassian.net/browse/PROJ-123']").last

    link.hover()
    card.wait_for(state="visible", timeout=2000)

    # Move the mouse elsewhere (away from both the link and the card) and
    # confirm the card hides again after the hide-delay.
    page.hover("#thread-title")
    card.wait_for(state="hidden", timeout=2000)


def test_unfurl_card_stays_visible_when_hovering_into_the_card_itself(logged_in_page, live_server):
    page = logged_in_page
    _mock_unfurl_route(
        page,
        {
            "type": "jira",
            "key": "PROJ-123",
            "url": "https://example.atlassian.net/browse/PROJ-123",
            "summary": "Fix logic of whether feature X is enabled",
            "status": "In Progress",
        },
    )
    _seed_session_with_message(page, JIRA_LINK_MESSAGE)

    card = page.locator(".unfurl-card.unfurl-jira").last
    link = page.locator(".bubble.assistant a.unfurl-link[href='https://example.atlassian.net/browse/PROJ-123']").last

    link.hover()
    card.wait_for(state="visible", timeout=2000)

    # Move from the link straight into the card (e.g. to click its inner
    # link) - it must stay visible rather than disappearing mid-transition.
    card.hover()
    time.sleep(0.3)
    assert card.is_visible()


def test_unfurl_card_is_positioned_near_the_cursor(logged_in_page, live_server):
    page = logged_in_page
    _mock_unfurl_route(
        page,
        {
            "type": "jira",
            "key": "PROJ-123",
            "url": "https://example.atlassian.net/browse/PROJ-123",
            "summary": "Fix logic of whether feature X is enabled",
            "status": "In Progress",
        },
    )
    _seed_session_with_message(page, JIRA_LINK_MESSAGE)

    card = page.locator(".unfurl-card.unfurl-jira").last
    link = page.locator(".bubble.assistant a.unfurl-link[href='https://example.atlassian.net/browse/PROJ-123']").last

    link.hover()
    card.wait_for(state="visible", timeout=2000)

    link_box = link.bounding_box()
    card_box = card.bounding_box()
    assert card_box is not None and link_box is not None
    # The card should be floating near the link's position (within a
    # generous tolerance for the cursor offset/clamping logic), not sitting
    # far away in some fixed location on the page.
    assert abs(card_box["x"] - link_box["x"]) < 400
    assert abs(card_box["y"] - link_box["y"]) < 200


def test_unrecognized_link_gets_no_unfurl_card(logged_in_page, live_server):
    page = logged_in_page
    _mock_unfurl_route(page, {"type": None})
    _seed_session_with_message(page, PLAIN_LINK_MESSAGE)
    time.sleep(0.3)

    assert page.locator(".unfurl-card").count() == 0
    # The plain link itself should still render normally.
    assert page.locator(".bubble.assistant a[href='https://example.com/some/other/page']").count() > 0


def test_unfurl_fetch_is_not_triggered_until_hover(logged_in_page, live_server):
    """/api/unfurl must not be called just because a recognized link
    rendered - only once it's actually hovered."""
    page = logged_in_page
    fetch_count = {"n": 0}

    def handler(route):
        fetch_count["n"] += 1
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_generic(
                {
                    "type": "jira",
                    "key": "PROJ-123",
                    "url": "https://example.atlassian.net/browse/PROJ-123",
                    "summary": "Fix logic of whether feature X is enabled",
                    "status": "In Progress",
                }
            )),
        )

    page.route("**/api/unfurl*", handler)
    _seed_session_with_message(page, JIRA_LINK_MESSAGE)

    # Rendering the message alone must not create a card or fetch
    # (fetching is deferred until hover).
    assert page.locator(".unfurl-card").count() == 0
    assert fetch_count["n"] == 0

    link = page.locator(".bubble.assistant a.unfurl-link[href='https://example.atlassian.net/browse/PROJ-123']").last
    link.hover()
    page.wait_for_selector(".unfurl-card.unfurl-jira", state="visible", timeout=2000)
    assert fetch_count["n"] == 1

    # Hovering again (after moving away and back) must not refetch - the
    # result is cached on first successful fetch.
    page.hover("#thread-title")
    link.hover()
    time.sleep(0.2)
    assert fetch_count["n"] == 1


def test_unfurl_card_shows_spinner_while_loading_then_swaps_to_content(logged_in_page, live_server):
    """While the lazily-triggered /api/unfurl request is in flight, a
    small spinner card should float near the cursor; once the response
    resolves, it's replaced in place by the real card content."""
    page = logged_in_page
    pending: dict = {}

    def handler(route):
        # Deliberately do NOT fulfill here - hold the route open and let
        # the main test thread resolve it later (after asserting the
        # loading/spinner state), to simulate a slow network response.
        # A blocking time.sleep() inside this handler (or fulfilling from
        # a background thread) would block/break Playwright's own
        # single-threaded sync dispatcher, since route callbacks run on
        # that same dispatcher - both were tried and failed.
        pending["route"] = route

    page.route("**/api/unfurl*", handler)
    _seed_session_with_message(page, JIRA_LINK_MESSAGE)

    link = page.locator(".bubble.assistant a.unfurl-link[href='https://example.atlassian.net/browse/PROJ-123']").last
    link.hover()

    # Spinner should appear immediately - the request hasn't been
    # fulfilled at all yet at this point.
    spinner = page.locator(".unfurl-card.unfurl-card-spinner.unfurl-card-visible")
    spinner.wait_for(state="visible", timeout=1000)
    assert page.locator(".unfurl-card.unfurl-jira").count() == 0

    # Simulate the slow network response, then fulfill from the main
    # thread (the only thread allowed to drive the sync Playwright client).
    time.sleep(0.3)
    pending["route"].fulfill(
        status=200,
        content_type="application/json",
        body=json.dumps(_generic(
            {
                "type": "jira",
                "key": "PROJ-123",
                "url": "https://example.atlassian.net/browse/PROJ-123",
                "summary": "Fix logic of whether feature X is enabled",
                "status": "In Progress",
            }
        )),
    )

    # Once the response resolves, the same floating card element swaps to
    # the real jira card content.
    page.wait_for_selector(".unfurl-card.unfurl-jira.unfurl-card-visible", timeout=2000)
    assert page.locator(".unfurl-card.unfurl-card-spinner").count() == 0
    card = page.locator(".unfurl-card.unfurl-jira").last
    assert "PROJ-123" in card.inner_text()


def test_unfurl_failed_lookup_removes_placeholder_and_does_not_retry(logged_in_page, live_server):
    """If the lookup fails (e.g. unrecognized/404), the spinner
    placeholder should be removed rather than left as an empty/broken
    card, and re-hovering the same link should not keep retrying."""
    page = logged_in_page
    fetch_count = {"n": 0}

    def handler(route):
        fetch_count["n"] += 1
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"type": None}))

    page.route("**/api/unfurl*", handler)
    _seed_session_with_message(page, JIRA_LINK_MESSAGE)

    link = page.locator(".bubble.assistant a.unfurl-link[href='https://example.atlassian.net/browse/PROJ-123']").last
    link.hover()
    time.sleep(0.3)
    assert page.locator(".unfurl-card").count() == 0
    assert fetch_count["n"] == 1

    page.hover("#thread-title")
    link.hover()
    time.sleep(0.3)
    assert page.locator(".unfurl-card").count() == 0
    assert fetch_count["n"] == 1
