"""Grafana package driver: generic multi-instance config, skill rendering, furl links, CLI, OAuth state, watcher.

Feature: Grafana is a package driver (CLI + skill + watcher recipes) with NO built-in host or company.
Guarantees: every instance (host/auth) gets its own skill; links route to the instance with the matching host;
the CLI resolves host+token from the named instance (token never in argv, never printed); writes are dry runs
without --yes; OAuth session state is chmod 600 and the rotated refresh token is persisted; the watcher records a
silent baseline, then reports each new alert / dashboard version / datasource failure once.
"""
import importlib.util
import json
import re
import stat
import sys
from pathlib import Path

import pytest

from integrations.registry import build_registry
from integrations.skills import ensure_skills
import watch_scripts

DRV = Path(__file__).resolve().parents[1] / "drivers" / "grafana"
sys.path.insert(0, str(DRV / "cli"))
import grafana_api as api  # noqa: E402
import grafana_config as gconf  # noqa: E402
import grafana_furl as furl  # noqa: E402
import grafana_oauth as oauth  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gw = _load("grafana_watch_mod", DRV / "watch" / "grafana_watch.py")
cli = _load("grafana_cli_mod", DRV / "cli" / "cli.py")


def _cfg():
    return {
        "one": {"driver": "grafana", "enabled": True, "base_url": "https://grafana.one.example",
                "auth": {"type": "service_account_token", "token": "x"}, "options": {}},
        "two": {"driver": "grafana", "enabled": True, "base_url": "https://grafana.two.example",
                "auth": {"type": "oauth_cdp"}, "options": {"idp_url": "https://idp.two.example", "title": "Second"}},
    }


def test_each_instance_gets_its_own_skill_with_its_own_host_and_no_leftover_templates(tmp_path):
    reg = build_registry(_cfg())
    assert not [i.errors for i in reg.integrations.values() if i.errors]
    assert ensure_skills(reg, tmp_path) == ["grafana-one", "grafana-two"]
    one = (tmp_path / "grafana-one" / "SKILL.md").read_text()
    assert "https://grafana.one.example" in one and "--instance one" in one and "grafana.two.example" not in one
    assert "${{" not in one.replace("$${{", "")
    assert str(DRV / "cli" / "cli.py") in one


@pytest.mark.parametrize("path,kind", [("/d/abc123/my-board?orgId=1&from=now-6h", "dashboard"), ("/d-solo/abc123/x", "dashboard"),
                                       ("/dashboards/f/fold_1/some-name", "folder"), ("/alerting/grafana/ruleuid1/view", "alert")])
def test_links_route_to_the_instance_with_the_matching_host(path, kind):
    reg = build_registry(_cfg())
    hit = reg.resolve_url("https://grafana.two.example" + path)
    assert hit is not None and hit[0].name == "two"
    assert reg.resolve_url("https://other.example" + path) is None


def test_watcher_recipe_pins_instance_and_exports_it_to_the_script():
    reg = build_registry(_cfg())
    meta = watch_scripts.apply_watcher({"script": "grafana_watch.py", "watcher": "grafana-alerts-firing",
                                        "integration": "two"}, reg)
    assert meta["args"][0] == "alerts"
    assert watch_scripts.instance_env({"integration": "two"}, reg)["GRAFANA_INSTANCE"] == "two"
    reg.integrations["two"].enabled = False
    with pytest.raises(ValueError):
        watch_scripts.instance_env({"integration": "two"}, reg)


def test_option_patterns_reject_injection_in_idp_url_and_org_id():
    bad = _cfg()
    bad["two"]["options"] = {"idp_url": "https://x.example/`id`", "org_id": "1;ls"}
    errs = [e for i in build_registry(bad).integrations.values() for e in i.errors]
    assert len(errs) >= 2


def _write_cfg(tmp_path, monkeypatch, insts):
    p = tmp_path / "config.yaml"
    import yaml
    p.write_text(yaml.safe_dump({"integrations": insts}))
    monkeypatch.setenv("DOP_AGENT_CONFIG", str(p))
    monkeypatch.setenv("DOP_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("GRAFANA_INSTANCE", raising=False)


def test_instance_resolution_token_ref_and_ambiguity(tmp_path, monkeypatch):
    monkeypatch.setenv("GF_TEST_TOKEN", "s3cret")
    _write_cfg(tmp_path, monkeypatch, {
        "a": {"driver": "grafana", "base_url": "https://a.example/", "auth": {"type": "service_account_token", "token": "${env:GF_TEST_TOKEN}"}},
        "b": {"driver": "grafana", "base_url": "https://b.example", "auth": {"type": "oauth_cdp"}, "options": {"idp_url": "https://i.example"}},
        "off": {"driver": "grafana", "enabled": False, "base_url": "https://c.example"}})
    with pytest.raises(gconf.InstanceError, match="enabled grafana instances: a, b"):
        gconf.load_instance(None)
    a = gconf.load_instance("a")
    assert a.base_url == "https://a.example" and a.token == "s3cret" and "s3cret" not in repr(a)
    assert gconf.load_instance("b").auth_type == "oauth_cdp"
    with pytest.raises(gconf.InstanceError):
        gconf.load_instance("off")


def test_oauth_instance_without_idp_url_is_rejected(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch, {"b": {"driver": "grafana", "base_url": "https://b.example", "auth": {"type": "oauth_cdp"}}})
    with pytest.raises(gconf.InstanceError, match="idp_url"):
        gconf.load_instance("b")


class FakeClient:
    def __init__(self, routes=None):
        self.routes, self.calls = routes or {}, []
        self.base = "https://g.example"
        self.inst = gconf.Instance("one", self.base, "service_account_token", "tok", {})

    def request(self, method, path, params=None, body=None):
        self.calls.append((method, path, body))
        return {"ok": True}

    def get(self, path, params=None):
        self.calls.append(("GET", path, params))
        r = self.routes.get(path)
        if isinstance(r, Exception):
            raise r
        return r() if callable(r) else r


def test_dashboard_writes_are_dry_runs_without_yes_and_real_with_yes(monkeypatch, capsys, tmp_path):
    fc = FakeClient({"/api/dashboards/uid/u1": {"dashboard": {"uid": "u1", "title": "T", "version": 3}, "meta": {}}})
    monkeypatch.setattr(api, "client", lambda inst=None: fc)
    f = tmp_path / "d.json"
    f.write_text(json.dumps({"title": "T", "id": 9}))
    assert cli.main(["dashboard", "delete", "u1"]) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    assert not [c for c in fc.calls if c[0] != "GET"]
    assert cli.main(["dashboard", "save", "--file", str(f), "--folder-uid", "F", "--yes"]) == 0
    method, path, body = fc.calls[-1]
    assert (method, path) == ("POST", "/api/dashboards/db") and "id" not in body["dashboard"] and body["folderUid"] == "F"
    n = len(fc.calls)
    assert cli.main(["api", "DELETE", "/api/x"]) == 0 and len(fc.calls) == n


def test_dashboard_summary_lists_panels_queries_and_variables_compactly(monkeypatch, capsys):
    dash = {"uid": "u", "title": "T", "version": 2, "templating": {"list": [{"name": "env"}]},
            "panels": [{"type": "row", "panels": [{"id": 2, "type": "timeseries", "title": "Rate",
                                                   "datasource": {"uid": "prom"}, "targets": [{"expr": "rate(x[5m])"}]}]},
                       {"id": 3, "type": "stat", "title": "S", "targets": []}]}
    monkeypatch.setattr(api, "client", lambda inst=None: FakeClient({"/api/dashboards/uid/u": {"dashboard": dash, "meta": {"folderTitle": "F"}}}))
    assert cli.main(["dashboard", "get", "u"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["variables"] == ["env"] and out["folder"] == "F"
    assert out["panels"][0] == {"id": 2, "type": "timeseries", "title": "Rate", "datasource": "prom", "queries": ["rate(x[5m])"]}


def test_fetch_builds_cards_for_each_kind(monkeypatch):
    fc = FakeClient({"/api/dashboards/uid/u": {"dashboard": {"title": "T", "tags": ["a", "b"], "version": 4},
                                               "meta": {"url": "/d/u/t", "folderTitle": "F", "updated": "2026-01-01"}},
                     "/api/folders/f": {"title": "Fold", "url": "/dashboards/f/f/fold"},
                     "/api/v1/provisioning/alert-rules/r": {"title": "Rule", "isPaused": True, "ruleGroup": "g"}})
    monkeypatch.setattr(api, "client", lambda inst=None: fc)
    d = furl.fetch("one", "dashboard", {"uid": "u"})
    assert d["title"] == "T" and d["url"] == "https://g.example/d/u/t" and d["fields"]["tags"] == "a, b"
    assert furl.fetch("one", "folder", {"uid": "f"})["title"] == "Fold"
    assert furl.fetch("one", "alert", {"uid": "r"})["status"] == "paused"


def test_test_command_reports_steps_and_never_leaks_the_token(monkeypatch, tmp_path):
    _write_cfg(tmp_path, monkeypatch, {"one": {"driver": "grafana", "base_url": "https://g.example",
                                               "auth": {"type": "service_account_token", "token": "SUPERSECRET"}}})
    monkeypatch.setattr(api.Client, "get", lambda self, p, params=None: {"login": "sa-bot"} if p == "/api/user" else {"name": "Main", "id": 1})
    res = furl.run_test("one")
    assert res["ok"] and [s["step"] for s in res["steps"]] == ["python", "config", "auth", "org"]
    assert "SUPERSECRET" not in json.dumps(res)
    monkeypatch.setattr(api.Client, "get", lambda self, p, params=None: (_ for _ in ()).throw(api.ApiError(401, "invalid token")))
    bad = furl.run_test("one")
    assert not bad["ok"] and "401" in bad["message"]


def test_oauth_refresh_rotates_and_persists_refresh_token_with_private_permissions(monkeypatch, tmp_path):
    _write_cfg(tmp_path, monkeypatch, {})
    inst = gconf.Instance("p", "https://g.example", "oauth_cdp", "", {"idp_url": "https://idp.example"})
    oauth.save_state("p", {"client_id": "c1", "access_token": "old", "refresh_token": "r1", "expires_at": 0})
    monkeypatch.setattr(oauth, "discover", lambda idp: {"token_endpoint": "https://idp.example/token"})

    class R:
        status_code, ok = 200, True
        def raise_for_status(self): pass
        def json(self): return {"access_token": "new", "refresh_token": "r2", "expires_in": 3600}

    import requests
    seen = {}
    monkeypatch.setattr(requests, "post", lambda url, data=None, **kw: seen.update(data) or R())
    assert oauth.access_token(inst) == "new"
    assert seen["grant_type"] == "refresh_token" and seen["refresh_token"] == "r1"
    st = oauth.load_state("p")
    assert st["refresh_token"] == "r2" and st["client_id"] == "c1"
    assert stat.S_IMODE(oauth.state_path("p").stat().st_mode) == 0o600
    assert oauth.access_token(inst) == "new"  # still valid: no second refresh


def _watch(monkeypatch, tmp_path, fc, argv):
    monkeypatch.setenv("WATCH_STATE_DIR", str(tmp_path / "st"))
    monkeypatch.setattr(api, "client", lambda inst=None: fc)
    return gw.main(argv)


def _alerts(*insts):
    return {"data": {"groups": [{"file": "Folder", "rules": [
        {"name": n, "uid": u, "alerts": [{"state": s, "labels": {"alertname": n, "severity": sev}, "activeAt": "t"}]}
        for n, u, s, sev in insts]}]}}


def test_alert_watch_baseline_then_each_new_firing_instance_once_with_label_filter(monkeypatch, tmp_path, capsys):
    box = {"v": _alerts(("CPU", "u1", "Alerting", "critical"))}
    fc = FakeClient({"/api/prometheus/grafana/api/v1/rules": lambda: box["v"]})
    argv = ["alerts", "--label", "severity=critical", "--resolved"]
    assert _watch(monkeypatch, tmp_path, fc, argv) == 0  # baseline
    box["v"] = _alerts(("CPU", "u1", "Alerting", "critical"), ("Disk", "u2", "Alerting", "critical"), ("Mem", "u3", "Alerting", "warning"))
    assert gw.main(argv) == gw.RC_CHANGED
    out = capsys.readouterr().out
    assert "Disk" in out and "CPU" not in out and "Mem" not in out and "https://g.example/alerting/grafana/u2/view" in out
    assert gw.main(argv) == 0
    box["v"] = _alerts(("Disk", "u2", "Alerting", "critical"))
    assert gw.main(argv) == gw.RC_CHANGED and "no longer" in capsys.readouterr().out


def test_dashboard_watch_reports_new_version_only_and_requires_a_target(monkeypatch, tmp_path, capsys):
    ver = {"v": 1}
    fc = FakeClient({"/api/dashboards/uid/u1": lambda: {"dashboard": {"title": "T", "version": ver["v"]}, "meta": {"url": "/d/u1/t", "updatedBy": "bob"}}})
    assert _watch(monkeypatch, tmp_path, fc, ["dashboard-changed"]) == 1
    assert gw.main(["dashboard-changed", "--uid", "u1"]) == 0
    assert gw.main(["dashboard-changed", "--uid", "u1"]) == 0
    ver["v"] = 2
    assert gw.main(["dashboard-changed", "--uid", "u1"]) == gw.RC_CHANGED
    out = capsys.readouterr().out
    assert "v1 -> v2" in out and "bob" in out


def test_datasource_watch_fires_on_transition_to_unhealthy(monkeypatch, tmp_path, capsys):
    st = {"s": "OK"}
    fc = FakeClient({"/api/datasources": [{"uid": "d1", "name": "prom", "type": "prometheus"}],
                     "/api/datasources/uid/d1/health": lambda: {"status": st["s"], "message": "boom"}})
    assert _watch(monkeypatch, tmp_path, fc, ["datasource-health"]) == 0
    st["s"] = "ERROR"
    assert gw.main(["datasource-health"]) == gw.RC_CHANGED and "UNHEALTHY" in capsys.readouterr().out
    assert gw.main(["datasource-health"]) == 0


def test_watch_transient_errors_are_silent_and_auth_errors_exit_1(monkeypatch, tmp_path):
    fc = FakeClient({"/api/prometheus/grafana/api/v1/rules": api.ApiError(503, "down")})
    assert _watch(monkeypatch, tmp_path, fc, ["alerts"]) == 0
    fc.routes["/api/prometheus/grafana/api/v1/rules"] = api.ApiError(401, "bad token")
    assert gw.main(["alerts"]) == 1


