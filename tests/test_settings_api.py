"""
Settings REST API (settings_api.py): contract + the hard security requirement that NO endpoint ever
returns or logs a secret value. Every test uses a tmp config via DOP_AGENT_CONFIG and a tmp secrets dir;
the real ~/.dop-agent is never touched.
"""

from __future__ import annotations

import json
import logging
import os
import stat

import pytest
import yaml
from fastapi.testclient import TestClient

import config
import settings_api
import web_server
from test_web_server import _login, _use_tmp_store

H = {"X-Requested-With": "dop", "Origin": "http://testserver"}
PLANTED_ENV = "xoxb-PLANTED-ENV-SECRET-111"
PLANTED_FILE = "glpat-PLANTED-FILE-SECRET-222"
PLANTED_VALUE = "glpat-PLANTED-BODY-SECRET-333"
ALL_PLANTED = (PLANTED_ENV, PLANTED_FILE, PLANTED_VALUE)


@pytest.fixture
def api(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    cfg = tmp_path / "config.yaml"
    monkeypatch.setenv("DOP_AGENT_CONFIG", str(cfg))
    monkeypatch.setenv("DOP_AGENT_SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.setenv("SLACK_BOT_TOKEN", PLANTED_ENV)
    for var in ("SESSION_TIMEOUT", "WEB_UI_PORT", "MODEL_ROUTING", "TABLE_RENDER_MODE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(settings_api, "_dependent_watches", lambda *a, **k: [])
    monkeypatch.setattr(settings_api, "_STATUS", {})
    config.reload()
    client = TestClient(web_server.app)
    _login(client, monkeypatch)
    client.cfg = cfg
    client.tmp = tmp_path
    yield client
    monkeypatch.delenv("DOP_AGENT_CONFIG", raising=False)
    config.reload()


def _walk_strings(o):
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for k, v in o.items():
            yield str(k)
            yield from _walk_strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk_strings(v)


def _assert_clean(resp):
    text = resp.text
    for s in ALL_PLANTED:
        assert s not in text, f"secret leaked in {resp.request.method} {resp.request.url}"


def _gl_draft(**over):
    d = {"name": "gl", "driver": "gitlab", "base_url": "https://gitlab.example.com",
         "auth": {"type": "private_token", "fields": {"token": {"mode": "file", "value": PLANTED_VALUE}}}}
    d.update(over)
    return d


def test_zero_config_get_returns_effective_values_with_source_labels(api):
    r = api.get("/api/settings")
    assert r.status_code == 200
    j = r.json()
    assert j["file_present"] is False and j["revision"] == "0"
    st = j["settings"]
    assert st["agent.session_timeout_s"]["source"] == "default" and st["agent.session_timeout_s"]["value"] == 120.0
    tok = st["adapters.slack.bot_token"]
    assert tok["source"] == "env" and tok["env_var"] == "SLACK_BOT_TOKEN" and tok["set"] is True and tok["value"] is None
    assert tok["restart_required"] is True and st["unfurl.cache_ttl_s"]["restart_required"] is False
    _assert_clean(r)


def test_requires_auth_and_csrf_header_and_origin(api):
    anon = TestClient(web_server.app)
    assert anon.get("/api/settings").status_code == 401
    assert anon.patch("/api/settings", json={"changes": {}}, headers=H).status_code == 401
    assert api.patch("/api/settings", json={"changes": {"agent.model_routing.mode": "on"}}).status_code == 403
    bad = api.patch("/api/settings", json={"changes": {"agent.model_routing.mode": "on"}},
                    headers={"X-Requested-With": "dop", "Origin": "http://evil.example"})
    assert bad.status_code == 403
    assert not api.cfg.exists()


def test_save_creates_file_chmod_600_and_returns_revision_and_hot_restart_flags(api):
    r = api.patch("/api/settings", headers=H, json={"revision": "0", "changes": {
        "unfurl.cache_ttl_s": 30, "adapters.web.port": 12345}})
    assert r.status_code == 200, r.text
    j = r.json()
    assert sorted(j["applied"]) == ["adapters.web.port", "unfurl.cache_ttl_s"]
    assert [x["path"] for x in j["restart_required"]] == ["adapters.web.port"]
    assert stat.S_IMODE(api.cfg.stat().st_mode) == 0o600
    assert yaml.safe_load(api.cfg.read_text())["adapters"]["web"]["port"] == 12345
    g = api.get("/api/settings").json()
    assert g["revision"] == j["revision"] and g["settings"]["unfurl.cache_ttl_s"]["source"] == "file"
    assert {"path": "adapters.web.port", "reason": "read once at daemon startup"} in g["restart_pending"]


def test_validation_errors_are_per_field_and_nothing_is_written(api):
    body = {"changes": {"adapters.web.port": "not-a-number", "agent.model_routing.mode": "maybe",
                        "nope.nothing": 1, "agent.usage_widget": "yes"}}
    r = api.patch("/api/settings", headers=H, json=body)
    assert r.status_code == 422 and r.json()["detail"] == "Validation failed"
    codes = {e["path"]: e["code"] for e in r.json()["errors"]}
    assert codes["adapters.web.port"] == "invalid_type" and codes["nope.nothing"] == "unknown_setting"
    assert codes["agent.usage_widget"] == "invalid_type"
    assert not api.cfg.exists()
    v = api.post("/api/settings/validate", headers=H, json={"changes": {"agent.model_routing.mode": "maybe"}}).json()
    assert v["valid"] is False and v["errors"][0]["code"] == "invalid_choice"
    ok = api.post("/api/settings/validate", headers=H, json={"changes": {"agent.model_routing.mode": "off"}}).json()
    assert ok["valid"] is True and not api.cfg.exists()


def test_revision_mismatch_returns_409_with_current_revision(api):
    api.patch("/api/settings", headers=H, json={"changes": {"agent.model_routing.mode": "on"}})
    r = api.patch("/api/settings", headers=H, json={"revision": "stale", "changes": {"agent.model_routing.mode": "off"}})
    assert r.status_code == 409 and r.json()["revision"] == api.get("/api/settings").json()["revision"]


def test_save_is_atomic_keeps_backup_and_leaves_no_temp_files(api, monkeypatch):
    api.patch("/api/settings", headers=H, json={"changes": {"agent.model_routing.mode": "on"}})
    before = api.cfg.read_bytes()
    api.patch("/api/settings", headers=H, json={"changes": {"agent.model_routing.mode": "off"}})
    assert (api.cfg.parent / "config.yaml.bak").read_bytes() == before
    boom = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
    monkeypatch.setattr(os, "replace", boom)
    r = api.patch("/api/settings", headers=H, json={"changes": {"agent.model_routing.mode": "dry"}})
    assert r.status_code == 500
    monkeypatch.undo()
    assert yaml.safe_load(api.cfg.read_text())["agent"]["model_routing"]["mode"] == "off"
    assert not [p for p in api.cfg.parent.iterdir() if p.name.startswith(".config.")]


def test_refs_are_preserved_across_saves_and_inline_secrets_rejected(api):
    api.cfg.write_text("version: 1\nadapters:\n  slack:\n    bot_token: ${env:SLACK_BOT_TOKEN}\n")
    os.chmod(api.cfg, 0o600)
    config.reload()
    r = api.patch("/api/settings", headers=H, json={"changes": {"agent.model_routing.mode": "on"}})
    assert r.status_code == 200
    assert yaml.safe_load(api.cfg.read_text())["adapters"]["slack"]["bot_token"] == "${env:SLACK_BOT_TOKEN}"
    bad = api.patch("/api/settings", headers=H, json={"changes": {"adapters.slack.bot_token": "xoxb-inline-123456"}})
    assert bad.status_code == 422 and bad.json()["errors"][0]["code"] == "inline_secret"
    assert "xoxb-inline-123456" not in bad.text
    ok = api.patch("/api/settings", headers=H, json={"changes": {
        "adapters.telegram.bot_token": {"mode": "env", "env": "TG_TOKEN"}}})
    assert ok.status_code == 200
    assert yaml.safe_load(api.cfg.read_text())["adapters"]["telegram"]["bot_token"] == "${env:TG_TOKEN}"


def test_secret_value_is_written_to_chmod_600_file_and_ref_stored_never_echoed(api):
    r = api.patch("/api/settings", headers=H, json={"changes": {
        "adapters.telegram.bot_token": {"mode": "file", "value": PLANTED_VALUE, "name": "tg"}}})
    assert r.status_code == 200, r.text
    _assert_clean(r)
    f = api.tmp / "secrets" / "tg"
    assert f.read_text() == PLANTED_VALUE and stat.S_IMODE(f.stat().st_mode) == 0o600
    assert stat.S_IMODE(f.parent.stat().st_mode) == 0o700
    assert PLANTED_VALUE not in api.cfg.read_text()
    st = api.get("/api/settings").json()["settings"]["adapters.telegram.bot_token"]
    assert st["ref"].startswith("${file:") and st["ref"].endswith("/tg}") and st["state"] == "set" and st["value"] is None
    config.reload()
    assert config.secret_value("adapters.telegram.bot_token") == PLANTED_VALUE
    os.chmod(f, 0o644)
    assert api.get("/api/settings").json()["settings"]["adapters.telegram.bot_token"]["state"] == "bad_mode"


def test_secret_value_from_remote_client_is_refused(api, monkeypatch):
    monkeypatch.setattr(settings_api, "_LOCAL_CLIENTS", set())
    r = api.patch("/api/settings", headers=H, json={"changes": {
        "adapters.telegram.bot_token": {"mode": "file", "value": PLANTED_VALUE}}})
    assert r.status_code == 422 and r.json()["errors"][0]["code"] == "secret_value_remote"
    _assert_clean(r)
    assert not (api.tmp / "secrets").exists()


def test_failed_save_rolls_back_secret_file(api):
    r = api.patch("/api/settings", headers=H, json={"changes": {
        "adapters.telegram.bot_token": {"mode": "file", "value": PLANTED_VALUE, "name": "tg"},
        "adapters.web.port": "bad"}})
    assert r.status_code == 422
    assert not (api.tmp / "secrets" / "tg").exists()


def test_broken_config_file_blocks_writes_without_leaking_content(api):
    api.cfg.write_text(f"version: 1\nadapters:\n  slack:\n    bot_token: {PLANTED_FILE}\n")
    r = api.patch("/api/settings", headers=H, json={"changes": {"agent.model_routing.mode": "on"}})
    assert r.status_code == 409 and r.json()["file_errors"]
    _assert_clean(r)
    _assert_clean(api.get("/api/settings"))
    y = api.get("/api/settings/yaml")
    _assert_clean(y)
    api.cfg.write_text("a: [unclosed\n  secret: " + PLANTED_FILE)
    _assert_clean(api.get("/api/settings"))
    _assert_clean(api.get("/api/settings/yaml"))


def test_yaml_preview_masks_inline_values_and_keeps_refs(api):
    api.patch("/api/settings", headers=H, json={"changes": {
        "adapters.slack.bot_token": "${env:SLACK_BOT_TOKEN}", "agent.model_routing.mode": "on"}})
    j = api.get("/api/settings/yaml").json()
    assert "${env:SLACK_BOT_TOKEN}" in j["yaml"] and "mode:" in j["yaml"]
    assert j["mode_ok"] is True and j["revision"] != "0"
    assert settings_api._mask_doc({"x": {"api_token": "plain"}}) == {"x": {"api_token": "***"}}
    _assert_clean(api.get("/api/settings/yaml"))


def test_schema_describes_sections_types_and_restart_flags(api):
    j = api.get("/api/settings/schema").json()
    port = j["properties"]["adapters"]["properties"]["web"]["properties"]["port"]
    assert port["type"] == "integer" and port["x-restart-required"] is True and port["x-env"] == ["WEB_UI_PORT"]
    mode = j["properties"]["agent"]["properties"]["model_routing"]["properties"]["mode"]
    assert mode["enum"] == ["on", "dry", "off"]
    assert j["properties"]["agent"]["properties"]["auto_title"]["x-restart-required"] is False
    assert j["properties"]["adapters"]["properties"]["slack"]["properties"]["bot_token"]["x-secret"] is True
    assert j["config_revision"] == "0"


def test_drivers_catalog_exposes_auth_fields_with_secret_flag(api):
    j = api.get("/api/drivers").json()
    gl = next(d for d in j if d["id"] == "gitlab")
    assert gl["auth_types"]["private_token"]["fields"]["token"] == {"secret": True, "required": True}
    assert "file" in gl["secret_modes"]


def test_base_url_change_with_secret_needs_confirmation(api):
    assert api.post("/api/integrations", headers=H, json=_gl_draft()).status_code == 201
    r = api.patch("/api/integrations/gl", headers=H, json={"base_url": "https://evil.example"})
    assert r.status_code == 422 and "base_url_change_unconfirmed" in r.text
    t = api.post("/api/integrations/test", headers=H, json=_gl_draft(base_url="https://evil.example"))
    assert "base_url_change_unconfirmed" in t.text
    same = api.patch("/api/integrations/gl", headers=H, json={"base_url": "https://gitlab.example.com/"})
    assert same.status_code in (200, 422) and "base_url_change_unconfirmed" not in same.text
    ok = api.patch("/api/integrations/gl", headers=H, json={"base_url": "https://evil.example", "confirm_base_url": True})
    assert ok.status_code == 200 and ok.json()["base_url"] == "https://evil.example"


def test_integration_crud_roundtrip_with_secret_state_and_no_leak(api):
    r = api.post("/api/integrations", headers=H, json=_gl_draft())
    assert r.status_code == 201, r.text
    _assert_clean(r)
    v = r.json()
    f = v["auth"]["fields"]["token"]
    assert f["state"] == "set" and f["ref"].endswith("/integration_gl_token}") and v["valid"] and v["enabled"]
    assert api.post("/api/integrations", headers=H, json=_gl_draft()).status_code == 409
    assert api.post("/api/integrations", headers=H, json=_gl_draft(name="Bad Name")).status_code == 422
    lst = api.get("/api/integrations")
    _assert_clean(lst)
    assert [i["name"] for i in lst.json()] == ["gl"]

    p = api.patch("/api/integrations/gl", headers=H, json={"enabled": False, "options": {"title": "Bob"}})
    assert p.status_code == 200 and p.json()["enabled"] is False and p.json()["status"]["state"] == "disabled"
    raw = yaml.safe_load(api.cfg.read_text())["integrations"]["gl"]
    assert raw["auth"]["token"] == f["ref"] and raw["options"] == {"title": "Bob"}

    rot = api.patch("/api/integrations/gl", headers=H, json={"secret": {"field": "token", "mode": "env", "env": "GL_T"}})
    assert rot.status_code == 200 and rot.json()["auth"]["fields"]["token"]["ref"] == "${env:GL_T}"
    assert api.patch("/api/integrations/gl", headers=H, json={"driver": "jira"}).status_code == 400
    assert api.patch("/api/integrations/nope", headers=H, json={"enabled": True}).status_code == 404

    assert api.delete("/api/integrations/gl", headers=H).status_code == 400
    d = api.delete("/api/integrations/gl?confirm=gl", headers=H)
    assert d.status_code == 200 and d.json()["paused_watches"] == 0
    assert api.get("/api/integrations").json() == []


def test_integration_validation_reports_driver_errors_per_field(api):
    r = api.post("/api/integrations", headers=H, json={"name": "gl", "driver": "gitlab", "base_url": "ftp://x",
                                                       "auth": {"type": "private_token", "fields": {}}})
    assert r.status_code == 422
    paths = {e["path"] for e in r.json()["errors"]}
    assert "integrations.gl.base_url" in paths and "integrations.gl.auth.token" in paths
    assert api.post("/api/integrations", headers=H, json={"name": "x", "driver": "nope"}).json()["errors"][0]["code"] == "driver_unknown"
    assert not api.cfg.exists()


def test_delete_integration_pauses_dependent_watches(api, monkeypatch):
    api.post("/api/integrations", headers=H, json=_gl_draft())
    import watchers
    paused = []
    monkeypatch.setattr(settings_api, "_dependent_watches", lambda *a, **k: [7, 8])
    monkeypatch.setattr(watchers, "set_status", lambda wid, st: paused.append((wid, st)) or True)
    d = api.delete("/api/integrations/gl?confirm=gl", headers=H)
    assert d.json()["paused_watches"] == 2 and paused == [(7, "paused"), (8, "paused")]


def test_draft_test_connection_uses_mocked_driver_persists_nothing_and_scrubs_leaks(api, monkeypatch):
    from drivers.package import PackageDriver as GitLabDriver
    seen = {}

    def fake(self):
        seen["token"] = self.integration.auth.get("token")
        return {"ok": False, "message": f"401 for token {seen['token']}"}

    monkeypatch.setattr(GitLabDriver, "test_connection", fake)
    r = api.post("/api/integrations/test", headers=H, json=_gl_draft())
    assert r.status_code == 200, r.text
    j = r.json()
    assert seen["token"] == PLANTED_VALUE
    assert j["ok"] is False and j["error"]["code"] == "connection_failed" and "latency_ms" in j
    _assert_clean(r)
    assert not api.cfg.exists() and not (api.tmp / "secrets").exists()

    monkeypatch.setattr(GitLabDriver, "test_connection", lambda self: {"ok": True, "message": "authenticated as bob"})
    ok = api.post("/api/integrations/test", headers=H, json=_gl_draft()).json()
    assert ok["ok"] is True and ok["identity"] == "authenticated as bob"


def test_saved_test_connection_updates_status_and_invalid_draft_reports_errors(api, monkeypatch):
    from drivers.package import PackageDriver as GitLabDriver
    monkeypatch.setattr(GitLabDriver, "test_connection", lambda self: {"ok": True, "message": "authenticated as bob"})
    api.post("/api/integrations", headers=H, json=_gl_draft())
    t = api.post("/api/integrations/test", headers=H, json={"name": "gl"}).json()
    assert t["ok"] is True
    st = api.get("/api/integrations").json()[0]["status"]
    assert st["state"] == "ok" and st["checked_at"]
    assert api.post("/api/integrations/test", headers=H, json={"name": "zzz"}).status_code == 404
    bad = api.post("/api/integrations/test", headers=H, json={"driver": "gitlab", "name": "d", "base_url": "nope"}).json()
    assert bad["ok"] is False and bad["error"]["code"] == "invalid_config"


def test_draft_test_inherits_saved_secret_when_field_omitted(api, monkeypatch):
    from drivers.package import PackageDriver as GitLabDriver
    api.post("/api/integrations", headers=H, json=_gl_draft())
    got = {}
    monkeypatch.setattr(GitLabDriver, "test_connection", lambda self: got.update(t=self.integration.auth.get("token")) or {"ok": True, "message": "x"})
    api.post("/api/integrations/test", headers=H, json={"name": "gl", "driver": "gitlab", "base_url": "https://other.example.com",
                                                       "auth": {"type": "private_token", "fields": {}}})
    assert "t" not in got
    api.post("/api/integrations/test", headers=H, json={"name": "gl", "driver": "gitlab", "base_url": "https://gitlab.example.com/",
                                                       "auth": {"type": "private_token", "fields": {}}})
    assert got["t"] == PLANTED_VALUE


def test_test_connection_timeout_is_reported(api, monkeypatch):
    import time as _t
    from drivers.package import PackageDriver as GitLabDriver
    monkeypatch.setattr(settings_api, "TEST_TIMEOUT_S", 0.2)
    monkeypatch.setattr(settings_api, "PACKAGE_TEST_TIMEOUT_S", 0.2)
    monkeypatch.setattr(GitLabDriver, "test_connection", lambda self: _t.sleep(1) or {"ok": True, "message": ""})
    j = api.post("/api/integrations/test", headers=H, json=_gl_draft()).json()
    assert j["error"]["code"] == "timeout"


def test_import_env_dry_run_masks_previews_then_applies(api, monkeypatch):
    env = api.tmp / ".env"
    env.write_text(f"SESSION_TIMEOUT=300\nSLACK_BOT_TOKEN={PLANTED_ENV}\nTELEGRAM_BOT_TOKEN={PLANTED_FILE}\n"
                   "WEB_UI_PORT=abc\nWEIRD_VAR=1\n")
    monkeypatch.delenv("SLACK_BOT_TOKEN")
    body = {"dry_run": True, "env_path": str(env), "secret_handling": {"TELEGRAM_BOT_TOKEN": "file"}}
    r = api.post("/api/settings/import-env", headers=H, json=body)
    assert r.status_code == 200, r.text
    _assert_clean(r)
    j = r.json()
    by = {m["var"]: m for m in j["mappings"]}
    assert j["unmapped"] == ["WEIRD_VAR"] and by["SESSION_TIMEOUT"]["preview"] == "300"
    assert by["SLACK_BOT_TOKEN"]["preview"].startswith("***") and by["WEB_UI_PORT"]["action"] == "error"
    assert not api.cfg.exists()

    env.write_text(f"SESSION_TIMEOUT=300\nSLACK_BOT_TOKEN={PLANTED_ENV}\nTELEGRAM_BOT_TOKEN={PLANTED_FILE}\n")
    a = api.post("/api/settings/import-env", headers=H, json={**body, "dry_run": False})
    assert a.status_code == 200, a.text
    _assert_clean(a)
    doc = yaml.safe_load(api.cfg.read_text())
    assert doc["agent"]["session_timeout_s"] == 300.0
    assert doc["adapters"]["slack"]["bot_token"] == "${env:SLACK_BOT_TOKEN}"
    assert doc["adapters"]["telegram"]["bot_token"].startswith("${file:")
    assert (api.tmp / "secrets" / "telegram_bot_token").read_text() == PLANTED_FILE
    assert api.post("/api/settings/import-env", headers=H, json={"env_path": "/etc/passwd"}).status_code == 400


def test_reload_picks_up_external_edit_and_reports_pending_restart(api):
    api.cfg.write_text("version: 1\nadapters:\n  web:\n    port: 4321\nagent:\n  model_routing:\n    mode: 'off'\n")
    os.chmod(api.cfg, 0o600)
    j = api.post("/api/settings/reload", headers=H).json()
    assert j["file_errors"] == [] and any(x["path"] == "adapters.web.port" for x in j["restart_required"])
    assert api.get("/api/settings").json()["settings"]["agent.model_routing.mode"]["value"] == "off"


def test_watch_types_lists_registry_and_builtin_types(api, monkeypatch):
    monkeypatch.setenv("X_GL", "tok")
    api.post("/api/integrations", headers=H, json=_gl_draft(auth={"type": "private_token", "fields": {
        "token": {"mode": "env", "env": "X_GL"}}}))
    j = api.get("/api/watch-types").json()
    assert "mr" not in j["registry"] and "pipeline" not in j["registry"]
    assert "time" in j["builtin"]


def test_no_endpoint_response_or_log_contains_planted_secrets(api, monkeypatch, caplog):
    """Sweep: create secrets via env, file ref and request value, hit every endpoint, scan everything."""
    caplog.set_level(logging.DEBUG)
    sec = api.tmp / "secrets"
    sec.mkdir(mode=0o700)
    (sec / "gl").write_text(PLANTED_FILE)
    os.chmod(sec / "gl", 0o600)
    api.patch("/api/settings", headers=H, json={"changes": {
        "adapters.telegram.bot_token": {"mode": "file", "value": PLANTED_VALUE, "name": "tg"},
        "adapters.slack.bot_token": "${env:SLACK_BOT_TOKEN}"}})
    api.post("/api/integrations", headers=H, json=_gl_draft(auth={"type": "private_token", "fields": {
        "token": {"mode": "file", "name": "gl"}}}))
    from drivers.package import PackageDriver as GitLabDriver
    monkeypatch.setattr(GitLabDriver, "test_connection", lambda self: {"ok": False, "message": f"bad {PLANTED_FILE}"})
    resps = [api.get(u) for u in ("/api/settings", "/api/settings/schema", "/api/settings/yaml", "/api/drivers",
                                  "/api/integrations", "/api/watch-types")]
    resps += [api.post("/api/integrations/test", headers=H, json={"name": "gl"}),
              api.post("/api/integrations/test", headers=H, json=_gl_draft()),
              api.post("/api/settings/validate", headers=H, json={"changes": {
                  "adapters.web.password": {"mode": "file", "value": PLANTED_VALUE}, "bogus": PLANTED_VALUE}}),
              api.patch("/api/settings", headers=H, json={"changes": {"adapters.web.password": PLANTED_VALUE}}),
              api.post("/api/settings/reload", headers=H),
              api.patch("/api/integrations/gl", headers=H, json={"enabled": True})]
    for r in resps:
        _assert_clean(r)
    for rec in caplog.records:
        assert not any(s in rec.getMessage() for s in ALL_PLANTED)
    assert config.secret_value("adapters.telegram.bot_token") == PLANTED_VALUE


def test_restart_requires_auth_csrf_and_confirmation_then_restarts(api, monkeypatch):
    calls = []
    monkeypatch.setattr(settings_api, "_do_restart", lambda: calls.append(1))
    assert api.post("/api/settings/restart", json={"confirm": True}).status_code == 403
    assert api.post("/api/settings/restart", headers=H, json={}).status_code == 400
    assert api.post("/api/settings/restart", headers=H, json={"confirm": "yes"}).status_code == 400
    anon = TestClient(web_server.app)
    assert anon.post("/api/settings/restart", headers=H, json={"confirm": True}).status_code == 401
    r = api.post("/api/settings/restart", headers=H, json={"confirm": True})
    assert r.status_code == 200 and r.json()["restarting"] is True
    import time
    time.sleep(1.5)
    assert calls == [1]


def test_do_restart_hard_exits_under_launchd_else_reexecs(monkeypatch):
    exits, execs = [], []
    monkeypatch.setattr(os, "_exit", lambda c: exits.append(c))
    monkeypatch.setattr(os, "execv", lambda *a: execs.append(a))
    monkeypatch.setenv("XPC_SERVICE_NAME", "com.dop-agent")
    settings_api._do_restart()
    assert exits == [0] and not execs
    monkeypatch.delenv("XPC_SERVICE_NAME")
    settings_api._do_restart()
    assert len(execs) == 1


# --- chat adapters ---------------------------------------------------------------------------------

def _fake_http(monkeypatch, calls=None, **replies):
    import adapter_catalog

    def fake(method, url, **kw):
        if calls is not None:
            calls.append(url)
        for key, reply in replies.items():
            if key in url:
                return reply
        return {"ok": False, "error": "unexpected"}
    monkeypatch.setattr(adapter_catalog, "_http", fake)


def test_adapters_list_hides_secrets_and_reports_setup_state(api):
    r = api.get("/api/adapters")
    assert r.status_code == 200
    a = {x["id"]: x for x in r.json()["adapters"]}
    assert set(a) == {"slack", "telegram"}
    assert a["slack"]["fields"]["bot_token"]["state"] == "set"
    assert a["slack"]["configured"] is False  # app_token missing
    assert a["slack"]["enabled"] is False and a["slack"]["status"] == {"state": "disabled", "detail": "not configured"}
    assert a["telegram"]["status"]["detail"] == "not configured"
    assert a["telegram"]["running"] is False
    assert PLANTED_ENV not in r.text


def test_adapter_patch_writes_refs_only_and_reports_restart(api):
    body = {"fields": {"bot_token": {"mode": "file", "value": PLANTED_VALUE, "name": "tg"},
                       "allowed_user_ids": ["42"]}, "enabled": True}
    r = api.patch("/api/adapters/telegram", headers=H, json=body)
    assert r.status_code == 200, r.text
    assert PLANTED_VALUE not in r.text
    assert {x["path"] for x in r.json()["restart_required"]} >= {"adapters.telegram.bot_token"}
    saved = yaml.safe_load(api.cfg.read_text())
    assert saved["adapters"]["telegram"]["bot_token"].startswith("${file:")
    assert PLANTED_VALUE not in api.cfg.read_text()
    assert r.json()["adapter"]["configured"] is True
    bad = api.patch("/api/adapters/telegram", headers=H, json={"fields": {"nope": 1}})
    assert bad.status_code == 422
    assert api.patch("/api/adapters/web", headers=H, json={}).status_code == 404
    assert api.patch("/api/adapters/telegram", json={}).status_code == 403


def test_adapter_test_slack_and_telegram_never_echo_tokens(api, monkeypatch):
    calls: list = []
    _fake_http(monkeypatch, calls, **{"auth.test": {"ok": True, "user": "dop", "team": "ACME"},
                                       "apps.connections.open": {"ok": False, "error": "invalid_auth"}})
    draft = {"id": "slack", "fields": {"app_token": {"mode": "file", "value": "xapp-PLANTED-APP-TOKEN"}}}
    r = api.post("/api/adapters/test", headers=H, json=draft)
    assert r.status_code == 200 and r.json()["ok"] is False
    assert "app token: invalid_auth" in r.json()["error"]["message"]
    assert "xapp-PLANTED-APP-TOKEN" not in r.text
    assert not api.cfg.exists() or "xapp-PLANTED" not in api.cfg.read_text()  # nothing persisted

    _fake_http(monkeypatch, getMe={"ok": True, "result": {"username": "dop_bot"}})
    ok = api.post("/api/adapters/test", headers=H, json={"id": "telegram", "fields": {
        "bot_token": {"mode": "file", "value": PLANTED_VALUE}, "allowed_user_ids": ["42"]}})
    assert ok.json()["ok"] is True and ok.json()["identity"] == "@dop_bot"
    assert PLANTED_VALUE not in ok.text
    miss = api.post("/api/adapters/test", headers=H, json={"id": "telegram", "fields": {
        "bot_token": {"mode": "file", "value": PLANTED_VALUE}}})
    assert miss.json()["error"]["code"] == "invalid_config"


def test_adapter_saved_test_records_status(api, monkeypatch):
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-1-ENV-APP-TOKEN")
    config.reload()
    _fake_http(monkeypatch, **{"auth.test": {"ok": True, "user": "dop", "team": "ACME"},
                                "apps.connections.open": {"ok": True}})
    assert api.post("/api/adapters/test", headers=H, json={"id": "slack"}).json()["ok"] is True
    slack = {x["id"]: x for x in api.get("/api/adapters").json()["adapters"]}["slack"]
    assert slack["status"]["state"] == "ok" and slack["status"]["detail"] == "dop @ ACME"


def test_scrub_masks_single_char_secret_only_as_whole_token():
    """Security: secrets shorter than 4 chars used to be skipped (leaked verbatim). They are now masked,
    but only as whole tokens so a 1-char secret like 'a' does not mangle ordinary words."""
    out = settings_api._scrub("pass a banana abc", {"a"})
    assert out == f"pass {settings_api.MASK} banana abc"
    assert settings_api._scrub("token=xyz1", {"xyz1"}) == f"token={settings_api.MASK}"
