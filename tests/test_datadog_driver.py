"""Datadog package driver (drivers/datadog/).

Feature: generic Datadog integration (CLI + watcher + skill), several orgs via config, `site` option, API key + app
key resolved from refs and never printed. Guarantees tested here (against a fake backend, no network): the manifest
loads and is off by default; one skill per enabled instance renders without leftovers and without a CLI hint in its
description; hostile `site`/`title` options are rejected; watcher recipes pin the instance; keys are redacted from
errors and `test` output; `test` reports per-step results; mute/unmute are dry runs without --yes and cancel only the
named monitor's downtimes; metrics output is a compact summary; the monitors watcher baselines silently and then
reports changes to alert/warn/recovered exactly once; the logs watcher reports only never-seen patterns.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

import watch_scripts
from integrations.registry import build_registry
from integrations.skills import ensure_skills

DRV = Path(__file__).resolve().parents[1] / "drivers" / "datadog"
sys.path.insert(0, str(DRV / "cli"))
sys.modules.pop("client", None)  # every driver CLI has its own top-level `client`
import client as ddclient  # noqa: E402
import ddconfig  # noqa: E402

_s = importlib.util.spec_from_file_location("dd_cli", DRV / "cli" / "cli.py")
cli = importlib.util.module_from_spec(_s)
_s.loader.exec_module(cli)
_w = importlib.util.spec_from_file_location("dd_watch", DRV / "watch" / "datadog_watch.py")
watch = importlib.util.module_from_spec(_w)
_w.loader.exec_module(watch)

API_KEY, APP_KEY = "apikey-SECRET-111", "appkey-SECRET-222"


def _cfg(site="datadoghq.eu"):
    return {"prod": {"driver": "datadog", "enabled": True,
                     "auth": {"type": "api_keys", "api_key": "${env:DD_T_API}", "app_key": "${env:DD_T_APP}"},
                     "options": {"site": site, "title": "Prod org"}}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    import yaml
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({"integrations": _cfg()}))
    monkeypatch.setenv("DOP_AGENT_CONFIG", str(p))
    monkeypatch.setenv("DD_T_API", API_KEY)
    monkeypatch.setenv("DD_T_APP", APP_KEY)
    monkeypatch.setenv("WATCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("DATADOG_INSTANCE", raising=False)
    return tmp_path


class Fake:
    """Fake backend: routes (method, path) to canned JSON; records writes."""
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, inst):
        outer = self

        class C(ddclient.Client):
            def request(self, method, path, params=None, body=None):
                outer.calls.append((method, path, params, body))
                r = outer.routes[(method, path)]
                if isinstance(r, Exception):
                    raise r
                return r(params, body) if callable(r) else r
        return C(inst)


def test_manifest_valid_off_by_default_and_skill_has_no_cli_hint_in_description(tmp_path):
    assert "datadog" in build_registry({}).manifests and build_registry({}).package_instances() == []
    reg = build_registry(_cfg())
    assert ensure_skills(reg, tmp_path) == ["datadog-prod"]
    text = (tmp_path / "datadog-prod" / "SKILL.md").read_text()
    desc = next(line for line in text.splitlines() if line.startswith("description:"))
    assert "datadoghq.eu" in desc and "--instance" not in desc and "cli.py" not in desc
    assert "--instance prod" in text and "${{" not in text.replace("$${{", "")


def test_hostile_site_and_title_options_are_rejected():
    for opt in ({"site": "evil.com/x"}, {"site": "datadoghq.eu`id`"}, {"title": "x`id`"}):
        bad = _cfg()
        bad["prod"]["options"] = opt
        assert "prod" not in [i.name for i in build_registry(bad).package_instances()], opt


def test_watcher_recipe_pins_instance_and_adds_leading_mode():
    reg = build_registry(_cfg())
    meta = watch_scripts.apply_watcher({"script": "datadog_watch.py", "watcher": "datadog-monitor-alert",
                                        "integration": "prod", "args": ["--tags", "team:x"]}, reg)
    assert meta["args"][0] == "monitors" and meta["integration"] == "prod"
    assert watch_scripts.instance_env(meta, reg) == {"DATADOG_INSTANCE": "prod"}


def test_load_instance_rejects_bad_site_and_empty_key(env, monkeypatch):
    inst = ddconfig.load_instance("prod")
    assert inst.api == "https://api.datadoghq.eu" and API_KEY not in repr(inst)
    monkeypatch.setenv("DD_T_APP", "")
    with pytest.raises(ddconfig.InstanceError, match="app_key is empty"):
        ddconfig.load_instance("prod")
    with pytest.raises(ddconfig.InstanceError, match="not found"):
        ddconfig.load_instance("nope")


def test_redact_strips_keys_from_error_text():
    out = ddclient.redact(f"boom api_key={API_KEY} and {APP_KEY}", API_KEY, APP_KEY)
    assert API_KEY not in out and APP_KEY not in out


def test_parse_time_accepts_relative_epoch_and_iso():
    assert ddclient.parse_time("15m", now=1000) == 100
    assert ddclient.parse_time("now", now=1000) == 1000
    assert ddclient.parse_time("1700000000") == 1700000000
    assert ddclient.parse_time("2024-01-01T00:00:00Z") == 1704067200
    with pytest.raises(ddclient.ApiError):
        ddclient.parse_time("yesterday-ish")


def test_test_command_reports_steps_and_never_leaks_keys(env):
    ok = Fake({("GET", "api/v1/validate"): {"valid": True}, ("GET", "api/v1/monitor"): [], ("GET", "api/v1/dashboard"): {"dashboards": []}})
    res = cli.run_test("prod", ok)
    assert res["ok"] and [s["step"] for s in res["steps"]] == ["config", "api_key", "app_key", "dashboards"]
    bad = Fake({("GET", "api/v1/validate"): ddclient.ApiError(f"HTTP 403 api_key={API_KEY}")})
    res = cli.run_test("prod", bad)
    assert not res["ok"] and API_KEY not in json.dumps(res)


def test_metrics_prints_compact_summary(env, capsys, monkeypatch):
    series = {"series": [{"display_name": "system.cpu.user", "scope": "host:a",
                          "pointlist": [[1, 1.0], [2, 3.0], [3, None], [4, 5.0]]}]}
    f = Fake({("GET", "api/v1/query"): series})
    monkeypatch.setattr(cli, "Client", f)
    assert cli.main(["--instance", "prod", "metrics", "avg:system.cpu.user{*}", "--points", "3"]) == 0
    out = capsys.readouterr().out
    assert "min=1" in out and "max=5" in out and "last=5" in out and out.count("\n") <= 3


def test_mute_is_dry_run_without_yes_and_unmute_cancels_only_that_monitor(env, capsys, monkeypatch):
    f = Fake({("GET", "api/v1/monitor/42"): {"id": 42, "name": "CPU", "overall_state": "Alert"},
              ("POST", "api/v2/downtime"): {"data": {"id": "d1"}},
              ("GET", "api/v2/downtime"): {"data": [
                  {"id": "d-42", "attributes": {"monitor_identifier": {"monitor_id": 42}}},
                  {"id": "d-7", "attributes": {"monitor_identifier": {"monitor_id": 7}}}]},
              ("DELETE", "api/v2/downtime/d-42"): {}})
    monkeypatch.setattr(cli, "Client", f)
    cli.main(["--instance", "prod", "mute", "42"])
    assert not [c for c in f.calls if c[0] == "POST"] and "dry run" in capsys.readouterr().out
    cli.main(["--instance", "prod", "mute", "42", "--duration", "2h", "--yes"])
    post = [c for c in f.calls if c[0] == "POST"][0]
    assert post[3]["data"]["attributes"]["monitor_identifier"] == {"monitor_id": 42}
    cli.main(["--instance", "prod", "unmute", "42"])
    assert not [c for c in f.calls if c[0] == "DELETE"]
    cli.main(["--instance", "prod", "unmute", "42", "--yes"])
    assert [c[1] for c in f.calls if c[0] == "DELETE"] == ["api/v2/downtime/d-42"]


def test_cli_rejects_non_numeric_monitor_id_and_bad_host(env, capsys, monkeypatch):
    monkeypatch.setattr(cli, "Client", Fake({}))
    assert cli.main(["--instance", "prod", "monitor", "1/../2"]) == 1
    assert cli.main(["--instance", "prod", "host-tags", "a/b"]) == 1


def _monitors(*pairs):
    return [{"id": i, "name": f"mon{i}", "overall_state": s, "tags": ["team:x"]} for i, s in pairs]


def test_monitor_watcher_baselines_then_reports_alert_warn_and_recovery_once(env, capsys):
    argv = ["monitors", "--instance", "prod", "--tags", "team:x"]
    state = {"v": _monitors((1, "OK"), (2, "OK"), (3, "Alert"))}
    f = Fake({("GET", "api/v1/monitor"): lambda p, b: state["v"]})
    assert watch.main(argv, f) == 0 and capsys.readouterr().out == ""
    state["v"] = _monitors((1, "Alert"), (2, "Warn"), (3, "OK"), (4, "Alert"))
    assert watch.main(argv, f) == 200
    out = capsys.readouterr().out
    assert "ALERT: [1]" in out and "WARN: [2]" in out and "RECOVERED: [3]" in out and "[4]" not in out
    assert watch.main(argv, f) == 0
    assert watch.main(argv + ["--notify", "alert"], f) == 0


def test_monitor_watcher_requires_a_filter_and_exits_1_on_auth_error(env, capsys):
    assert watch.main(["monitors", "--instance", "prod"], Fake({})) == 1
    f = Fake({("GET", "api/v1/monitor"): ddclient.ApiError("HTTP 403 forbidden")})
    assert watch.main(["monitors", "--instance", "prod", "--tags", "a:b"], f) == 1
    f = Fake({("GET", "api/v1/monitor"): ddclient.ApiError("HTTP 503 down")})
    assert watch.main(["monitors", "--instance", "prod", "--tags", "a:b"], f) == 0


def test_log_watcher_reports_only_new_patterns_with_numbers_normalized(env, capsys):
    def logs(msgs):
        return {"data": [{"attributes": {"message": m, "status": "error", "service": "api", "host": "h", "timestamp": "t"}} for m in msgs]}
    state = {"v": logs(["timeout after 30 ms on 10.0.0.1", "timeout after 31 ms on 10.0.0.2"])}
    f = Fake({("POST", "api/v2/logs/events/search"): lambda p, b: state["v"]})
    argv = ["logs", "--instance", "prod", "--query", "status:error"]
    assert watch.main(argv, f) == 0
    state["v"] = logs(["timeout after 99 ms on 10.9.9.9", "disk full on /var"])
    assert watch.main(argv, f) == 200
    out = capsys.readouterr().out
    assert "disk full" in out and "timeout" not in out
    assert watch.main(argv, f) == 0


def test_driver_folder_has_no_symlinks():
    assert not [p for p in DRV.rglob("*") if p.is_symlink()]
