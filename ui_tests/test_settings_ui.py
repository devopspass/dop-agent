"""
Playwright tests for the Settings UI tabs (General / Integrations / Advanced) built on the
settings REST API (webui/settings.js, settings_api.py). The Browser tab (quiet hours, Recent
window) is covered by test_settings_quiet_hours.py and must keep working.

Each test runs against a throwaway config (DOP_AGENT_CONFIG / DOP_AGENT_SECRETS_DIR under tmp_path),
never ~/.dop-agent. The connection test of the wizard is mocked at the network layer.

Guarantees covered: the General tab edits + saves a field into the YAML file; a stale revision is
reported as a 409 conflict instead of overwriting; secret values are write-only and never end up
in the DOM; the add-integration wizard (driver -> connection -> test -> watches -> save) creates the
integration and refreshes link-hover recognition immediately; delete needs the typed name; Esc closes
the inner dialog before the settings overlay.
"""

from __future__ import annotations

import json

import pytest

import config

SECRET = "S3cr3t-VALUE-9f8e7d"


@pytest.fixture(autouse=True)
def tmp_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    monkeypatch.setenv("DOP_AGENT_CONFIG", str(cfg))
    monkeypatch.setenv("DOP_AGENT_SECRETS_DIR", str(tmp_path / "secrets"))
    for v in ("WATCHER_POLL_INTERVAL", "COPILOT_GITHUB_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(v, raising=False)
    config.reload()
    yield cfg
    monkeypatch.undo()
    config.reload()


def _open(page, tab):
    page.click("#settings-link")
    page.click(f'#settings-tabs [data-stab="{tab}"]')


def _dom_leaks(page, text):
    return page.evaluate(
        """(t) => document.documentElement.outerHTML.includes(t) ||
        [...document.querySelectorAll('input,textarea')].some(i => i.value.includes(t))""",
        text,
    )


def test_settings_has_tabs_and_keeps_browser_prefs(logged_in_page):
    page = logged_in_page
    page.click("#settings-link")
    assert page.is_visible('#settings-pane-integrations')
    assert page.eval_on_selector_all("#settings-tabs .sx-tab", "e => e[0].dataset.stab") == "integrations"
    page.click('#settings-tabs [data-stab="browser"]')
    assert page.is_visible("#settings-quiet-hours-enabled")
    assert page.is_visible("#settings-recent-hours")
    for t in ("browser", "agent", "transcription", "adapters", "mcp", "watchers", "unfurl", "integrations", "advanced"):
        assert page.is_visible(f'#settings-tabs [data-stab="{t}"]')
    page.click('#settings-tabs [data-stab="agent"]')
    page.wait_for_selector('[data-section="agent"]')
    assert page.get_attribute('[data-row="agent.workdir"] .sx-info', "data-tip")
    assert not page.is_visible("#settings-quiet-hours-enabled")
    for sec in ("transcription", "adapters", "mcp", "watchers", "unfurl"):
        page.click(f'#settings-tabs [data-stab="{sec}"]')
        page.wait_for_selector(f'[data-section="{sec}"]')
        assert page.is_visible(f'[data-section="{sec}"]')


def test_edit_general_field_and_save_writes_config_file(logged_in_page, tmp_config):
    page = logged_in_page
    _open(page, "watchers")
    page.wait_for_selector("#sx-f-watchers-tick_s")
    assert page.is_disabled("#sx-save")
    page.fill("#sx-f-watchers-tick_s", "45")
    assert page.is_enabled("#sx-save")
    assert "1 unsaved change" in page.inner_text("#sx-dirty-count")
    page.click("#sx-save")
    page.wait_for_selector(".sx-banner:has-text('Saved 1 setting')")
    assert "tick_s: 45" in tmp_config.read_text()
    assert page.input_value("#sx-f-watchers-tick_s") == "45"
    assert page.is_disabled("#sx-save")


def test_revert_discards_edits(logged_in_page):
    page = logged_in_page
    _open(page, "watchers")
    page.fill("#sx-f-watchers-tick_s", "99")
    page.click("#sx-revert")
    assert page.input_value("#sx-f-watchers-tick_s") == "60"


def test_stale_revision_shows_conflict_and_does_not_overwrite(logged_in_page, tmp_config, _fail_on_console_errors):
    page = logged_in_page
    _open(page, "watchers")
    page.wait_for_selector("#sx-f-watchers-tick_s")
    tmp_config.write_text("watchers:\n  coalesce_window_s: 9\n")
    page.fill("#sx-f-watchers-tick_s", "45")
    page.click("#sx-save")
    page.wait_for_selector("#sx-conflict")
    _fail_on_console_errors.clear()  # the browser logs the expected 409 as a console error
    assert "tick_s" not in tmp_config.read_text()
    page.click('[data-act="conflict-reload"]')
    page.wait_for_selector("#sx-save:not([disabled])")
    page.click("#sx-save")
    page.wait_for_selector(".sx-banner:has-text('Saved')")
    assert "tick_s: 45" in tmp_config.read_text() and "coalesce_window_s" in tmp_config.read_text()


def test_secret_setting_is_write_only_and_never_in_dom(logged_in_page, tmp_path):
    page = logged_in_page
    _open(page, "agent")
    row = page.locator('[data-row="agent.copilot.github_token"]')
    row.locator("select").select_option("file")
    row.locator('input[type=password]').fill(SECRET)
    page.click("#sx-save")
    page.wait_for_selector(".sx-banner:has-text('Saved')")
    assert not _dom_leaks(page, SECRET)
    assert "Currently set" in page.inner_text('[data-row="agent.copilot.github_token"]')
    files = list((tmp_path / "secrets").iterdir())
    assert len(files) == 1 and files[0].read_text().strip() == SECRET
    assert not _dom_leaks(page, SECRET)


def _mock_test(page, ok=True):
    def handler(route):
        body = {"ok": True, "identity": "tester@example.com", "latency_ms": 12} if ok else {
            "ok": False, "latency_ms": 3, "error": {"code": "connection_failed", "message": "401 Unauthorized"}}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
    page.route("**/api/integrations/test", handler)


def _wizard_add_jira(page, name="myjira"):
    page.click("#sx-add-int")
    page.click('[data-driver="jira"]')
    page.fill("#sx-w-name", name)
    page.fill("#sx-w-base", "https://acme.atlassian.net")
    page.fill('[data-field="user"]', "me@acme.com")
    page.locator(".sx-modal .sx-secret select").select_option("file")
    page.locator('.sx-modal .sx-secret input[type=password]').fill(SECRET)
    page.click("#sx-wiz-next")


def test_add_integration_wizard_with_mocked_test_connection(logged_in_page, tmp_config):
    page = logged_in_page
    _mock_test(page, ok=True)
    _open(page, "integrations")
    page.wait_for_selector("#sx-add-int")
    assert "No integrations" in page.inner_text("#settings-pane-integrations")
    _wizard_add_jira(page)
    page.click("#sx-test-btn")
    page.wait_for_selector("#sx-test-out:has-text('Connected')")
    assert "tester@example.com" in page.inner_text("#sx-test-out")
    assert not _dom_leaks(page, SECRET)
    page.click("#sx-wiz-next")
    page.check('[data-kind="ticket"]')
    page.click("#sx-wiz-save")
    page.wait_for_selector('[data-int="myjira"]')
    assert not _dom_leaks(page, SECRET)
    text = tmp_config.read_text()
    assert "myjira" in text and SECRET not in text
    # hover recognition is refreshed without a page reload
    assert page.evaluate("Furls.isLink('https://acme.atlassian.net/browse/AB-12')")


def test_wizard_shows_failed_connection_test(logged_in_page):
    page = logged_in_page
    _mock_test(page, ok=False)
    _open(page, "integrations")
    _wizard_add_jira(page)
    page.click("#sx-test-btn")
    page.wait_for_selector("#sx-test-out:has-text('401 Unauthorized')")


def test_delete_integration_requires_typed_name(logged_in_page, tmp_config):
    page = logged_in_page
    _mock_test(page)
    _open(page, "integrations")
    _wizard_add_jira(page, "delme")
    page.click("#sx-wiz-next")
    page.click("#sx-wiz-save")
    page.wait_for_selector('[data-int="delme"]')
    page.click('[data-int="delme"] [data-act="delete"]')
    assert page.is_disabled("#sx-del-go")
    page.fill("#sx-del-confirm", "wrong")
    assert page.is_disabled("#sx-del-go")
    page.fill("#sx-del-confirm", "delme")
    page.click("#sx-del-go")
    page.wait_for_selector('[data-int="delme"]', state="detached")
    assert "delme" not in tmp_config.read_text()


def test_add_integration_driver_step_has_search_that_filters_cards(logged_in_page):
    """Feature: the 'Add integration' driver step has a search box; typing filters
    driver cards by title/id/kinds and shows an empty-state when nothing matches."""
    page = logged_in_page
    _open(page, "integrations")
    page.click("#sx-add-int")
    total = page.locator(".sx-modal .sx-drv").count()
    page.fill("#sx-drv-search", "jira")
    visible = page.locator(".sx-modal .sx-drv:visible")
    assert 1 <= visible.count() < total
    assert page.is_visible('[data-driver="jira"]')
    page.fill("#sx-drv-search", "zzz-no-such-driver")
    assert page.locator(".sx-modal .sx-drv:visible").count() == 0
    assert page.is_visible("#sx-drv-none")
    page.fill("#sx-drv-search", "")
    assert page.locator(".sx-modal .sx-drv:visible").count() == total


def test_escape_closes_inner_dialog_before_overlay(logged_in_page):
    page = logged_in_page
    _open(page, "integrations")
    page.click("#sx-add-int")
    assert page.is_visible(".sx-modal")
    page.keyboard.press("Escape")
    assert not page.is_visible(".sx-modal")
    assert page.is_visible("#settings-overlay")
    page.keyboard.press("Escape")
    assert not page.is_visible("#settings-overlay")


def test_focus_stays_inside_settings_overlay(logged_in_page):
    page = logged_in_page
    page.click("#settings-link")
    for _ in range(40):
        page.keyboard.press("Tab")
        assert page.evaluate("document.getElementById('settings-overlay').contains(document.activeElement)")


def test_advanced_tab_yaml_import_reload_and_watch_types(logged_in_page, tmp_path, tmp_config):
    page = logged_in_page
    tmp_config.write_text("watchers:\n  tick_s: 30\n")
    env = tmp_path / "x.env"
    env.write_text(f"WATCHER_POLL_INTERVAL=77\nCOPILOT_GITHUB_TOKEN={SECRET}\n")
    _open(page, "advanced")
    page.wait_for_selector("#sx-yaml:has-text('tick_s: 30')")
    page.fill("#sx-import-path", str(env))
    page.click("#sx-import-dry")
    page.wait_for_selector("#sx-import-out table")
    assert "Dry run" in page.inner_text("#sx-import-out")
    assert not _dom_leaks(page, SECRET)
    assert "77" not in tmp_config.read_text()
    page.click("#sx-reload-btn")
    page.wait_for_selector("#sx-reload-out .sx-banner")
    assert page.is_visible("#sx-wt-out table")


def test_general_tab_usable_at_narrow_width(logged_in_page):
    page = logged_in_page
    page.set_viewport_size({"width": 400, "height": 700})
    _open(page, "watchers")
    page.wait_for_selector("#sx-f-watchers-tick_s")
    wide = page.evaluate("""() => { const b = document.querySelector('.sx-body'); const r = b.getBoundingClientRect();
        return [...b.querySelectorAll('*')].filter(n => n.getBoundingClientRect().right > r.right + 1).map(n => n.tagName + '.' + n.className).slice(0, 5); }""")
    assert wide == []


def test_integrations_list_shows_driver_icon_from_registry_icon_url(logged_in_page, tmp_config):
    """The integrations list takes each driver's resolved `icon_url` from /api/drivers (no hardcoded
    jira/gitlab check): a freshly added Jira integration shows an <img> pointing at the
    icon_url, and an icon that fails to load falls back to the driver's first letter."""
    page = logged_in_page
    _mock_test(page, ok=True)
    _open(page, "integrations")
    page.wait_for_selector("#sx-add-int")
    _wizard_add_jira(page)
    page.click("#sx-test-btn")
    page.wait_for_selector("#sx-test-out:has-text('Connected')")
    page.click("#sx-wiz-next")
    page.check('[data-kind="ticket"]')
    page.click("#sx-wiz-save")
    page.wait_for_selector('[data-int="myjira"] .sx-ico img')
    assert page.get_attribute('[data-int="myjira"] .sx-ico img', "src") == "/static/icons/jira.svg"


def test_integration_status_dot_refreshes_from_checking_to_ok(logged_in_page):
    """The integrations list shows 'checking…' while the server runs a background test and re-fetches
    on its own until the status settles, turning the dot green (ok) without the user touching anything."""
    page = logged_in_page
    calls = {"n": 0}
    item = {"name": "myjira", "driver": "jira", "title": "Jira", "enabled": True, "valid": True, "errors": [],
            "base_url": "https://acme.atlassian.net", "auth": {"type": "api_token", "fields": {}},
            "options": {}, "kinds": {}, "watch_count": 0}

    def handler(route):
        if route.request.method != "GET":
            return route.continue_()
        calls["n"] += 1
        st = {"state": "checking", "detail": "", "checked_at": None} if calls["n"] == 1 else {
            "state": "ok", "detail": "tester@example.com", "checked_at": "2026-01-01T00:00:00+00:00"}
        route.fulfill(status=200, content_type="application/json", body=json.dumps([{**item, "status": st}]))
    page.route("**/api/integrations", handler)
    _open(page, "integrations")
    page.wait_for_selector('[data-int="myjira"] .sx-d-warn')
    assert "checking" in page.inner_text('[data-int="myjira"] .sx-status')
    page.wait_for_selector('[data-int="myjira"] .sx-d-ok', timeout=8000)
    assert "ok" in page.inner_text('[data-int="myjira"] .sx-status')


def test_adapters_card_edit_saves_ref_hides_secret_and_leaves_general(logged_in_page, tmp_config):
    page = logged_in_page
    _open(page, "integrations")
    page.wait_for_selector('[data-adapter="telegram"]')
    assert page.is_visible('[data-adapter="slack"]')
    page.click('#settings-tabs [data-stab="adapters"]')
    page.wait_for_selector('[data-section="adapters"]')
    assert not page.query_selector('#sx-f-adapters-telegram-bot_token')
    assert page.query_selector('#sx-f-adapters-web-port')
    page.click('#settings-tabs [data-stab="integrations"]')
    for _ in range(5):  # the pane re-renders once while integrations/adapters load
        page.click('[data-adapter="telegram"] [data-act="edit-adapter"]')
        if page.query_selector(".sx-modal"):
            break
        page.wait_for_timeout(300)
    page.select_option(".sx-modal .sx-secret select", "file")
    page.fill('.sx-modal input[aria-label="Secret value"]', SECRET)
    page.fill('input[data-field="allowed_user_ids"]', "42, 43")
    page.click("#sx-adapter-save")
    page.wait_for_selector(".sx-modal", state="detached")
    assert not _dom_leaks(page, SECRET)
    saved = tmp_config.read_text()
    assert "${file:" in saved and SECRET not in saved
    assert config.value("adapters.telegram.allowed_user_ids") == ["42", "43"]


def test_search_filters_settings_across_sections_and_esc_restores_tab(logged_in_page, tmp_config):
    """Feature: the Settings search box filters ALL sections (label/path/env var/description),
    showing matches grouped under section titles whatever tab is active; Esc clears it and the
    normal tab view returns. Unmatched queries show 'No matching settings'."""
    page = logged_in_page
    _open(page, "agent")
    page.wait_for_selector('[data-section="agent"]')
    page.fill("#settings-search", "WATCHER_POLL_INTERVAL")
    page.wait_for_selector('[data-row="watchers.tick_s"]')
    assert not page.query_selector('[data-section="agent"]')
    assert page.locator("#settings-tabs .sx-tab.active").count() == 0
    assert page.inner_text('[data-section="watchers"] .sx-hd').strip().lower().startswith("watchers")
    page.fill("#settings-search", "zzz-no-such-setting")
    page.wait_for_selector("#sx-no-match")
    assert page.inner_text("#sx-no-match") == "No matching settings"
    page.fill("#settings-search", "tick")
    page.wait_for_selector('[data-row="watchers.tick_s"]')
    page.press("#settings-search", "Escape")
    page.wait_for_selector('[data-section="agent"]')
    assert page.input_value("#settings-search") == ""
    assert page.get_attribute('#settings-tabs [data-stab="agent"]', "aria-selected") == "true"
    assert page.is_visible("#settings-overlay")


def test_search_keeps_dirty_edit_while_filtering_and_saves(logged_in_page, tmp_config):
    """Edits made on a search result stay dirty (save bar count) when the filter changes and
    still save through the normal Save button."""
    page = logged_in_page
    _open(page, "integrations")
    page.fill("#settings-search", "tick_s")
    sel = '[data-row="watchers.tick_s"] input'
    page.wait_for_selector(sel)
    page.fill(sel, "45")
    page.wait_for_function("document.getElementById('sx-dirty-count').textContent.includes('1 unsaved')")
    page.fill("#settings-search", "workdir")
    page.wait_for_selector('[data-row="agent.workdir"]')
    page.fill("#settings-search", "tick_s")
    page.wait_for_selector(sel)
    assert page.input_value(sel) == "45"
    page.wait_for_function("!document.getElementById('sx-save').disabled")
    page.click("#sx-save")
    page.wait_for_function("document.getElementById('sx-dirty-count').textContent.includes('No unsaved')")
    assert "tick_s: 45" in tmp_config.read_text()
