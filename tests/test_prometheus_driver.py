"""Prometheus / Alertmanager package driver: config, skill rendering, CLI, `test`, silences and the watcher.

Feature: Prometheus is a generic package driver (CLI + skill + watcher recipes) talking directly to the Prometheus
and Alertmanager HTTP APIs against a local fake backend (no real credentials needed).
Guarantees: instance URLs/auth come from config.yaml (secret refs resolved, never printed, bearer/basic headers
really sent); range queries are bounded (<=7d, <=500 points) and results summarized; silence create/expire are dry
runs without --yes; option patterns reject injection; the watcher records a silent baseline and then reports each
newly firing alert / down target / threshold crossing once (and resolution with --resolved).
"""
import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml

from integrations.registry import build_registry
from integrations.skills import ensure_skills
import watch_scripts

DRV = Path(__file__).resolve().parents[1] / "drivers" / "prometheus"
sys.path.insert(0, str(DRV / "cli"))
import prom_api as api  # noqa: E402
import prom_config as pconf  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pw = _load("prometheus_watch_mod", DRV / "watch" / "prometheus_watch.py")
cli = _load("prometheus_cli_mod", DRV / "cli" / "cli.py")

STATE = {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _route(self, method):
        STATE.setdefault("auth", []).append(self.headers.get("Authorization"))
        path = self.path.split("?")[0]
        STATE.setdefault("calls", []).append((method, self.path))
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            STATE["body"] = json.loads(self.rfile.read(n))
        if STATE.get("require_auth") and self.headers.get("Authorization") != STATE["require_auth"]:
            return self._send({"status": "error", "error": "unauthorized token=abc123"}, 401)
        routes = STATE["routes"]
        if (method, path) in routes:
            r = routes[(method, path)]
            return self._send(r() if callable(r) else r)
        self._send({"status": "error", "error": "not found"}, 404)

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_DELETE(self):
        self._route("DELETE")


@pytest.fixture
def backend(tmp_path, monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    STATE.clear()
    STATE["routes"] = {}
    STATE["calls"] = []
    url = f"http://127.0.0.1:{srv.server_port}"
    monkeypatch.setenv("PROM_TEST_TOKEN", "s3cret")
    p = tmp_path / "config.yaml"
    monkeypatch.setenv("DOP_AGENT_CONFIG", str(p))
    monkeypatch.setenv("WATCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("PROMETHEUS_INSTANCE", raising=False)

    def configure(auth=None, am=True):
        opts = {"prometheus_url": url, **({"alertmanager_url": url} if am else {})}
        p.write_text(yaml.safe_dump({"integrations": {"p1": {"driver": "prometheus", "enabled": True, "options": opts,
                                                             "auth": auth or {"type": "none"}}}}))
    configure()
    STATE["configure"] = configure
    yield STATE
    srv.shutdown()


def _vec(*items):
    return {"status": "success", "data": {"resultType": "vector", "result": [
        {"metric": m, "value": [1, str(v)]} for m, v in items]}}


def test_each_instance_gets_its_own_skill_and_bad_options_are_rejected(tmp_path):
    cfg = {"one": {"driver": "prometheus", "enabled": True, "options": {"prometheus_url": "https://prom.one.example", "title": "One"}, "auth": {"type": "none"}}}
    reg = build_registry(cfg)
    assert not [i.errors for i in reg.integrations.values() if i.errors]
    assert ensure_skills(reg, tmp_path) == ["prometheus-one"]
    text = (tmp_path / "prometheus-one" / "SKILL.md").read_text()
    assert "https://prom.one.example" in text and "--instance one" in text and "${{" not in text.replace("$${{", "")
    assert str(DRV / "cli" / "cli.py") in text
    cfg["one"]["options"]["alertmanager_url"] = "https://x.example/`id`"
    cfg["one"]["options"]["prometheus_url"] = "https://p.example\n--evil"
    errs = [e for i in build_registry(cfg).integrations.values() for e in i.errors]
    assert len(errs) >= 2


def test_watcher_recipe_pins_instance_and_exports_it_to_the_script():
    cfg = {"one": {"driver": "prometheus", "enabled": True, "options": {"prometheus_url": "https://prom.one.example"}, "auth": {"type": "none"}}}
    reg = build_registry(cfg)
    meta = watch_scripts.apply_watcher({"script": "prometheus_watch.py", "watcher": "prometheus-threshold", "integration": "one"}, reg)
    assert meta["args"][0] == "threshold"
    assert watch_scripts.instance_env({"integration": "one"}, reg)["PROMETHEUS_INSTANCE"] == "one"


def test_basic_auth_requires_username_and_secret_not_logged_in_repr(tmp_path, monkeypatch, backend):
    backend_cfg = yaml.safe_load(Path(pconf.config_path()).read_text())
    opts = backend_cfg["integrations"]["p1"]["options"]
    Path(pconf.config_path()).write_text(yaml.safe_dump({"integrations": {"p1": {"driver": "prometheus", "options": opts,
        "auth": {"type": "basic", "username": "u", "password": "${env:PROM_TEST_TOKEN}"}}}}))
    inst = pconf.load_instance("p1")
    assert inst.secret == "s3cret" and "s3cret" not in repr(inst)
    Path(pconf.config_path()).write_text(yaml.safe_dump({"integrations": {"p1": {"driver": "prometheus", "options": opts,
        "auth": {"type": "basic", "password": "${env:PROM_TEST_TOKEN}"}}}}))
    with pytest.raises(pconf.InstanceError, match="username"):
        pconf.load_instance("p1")


def test_bearer_token_is_sent_and_errors_never_leak_secrets(backend, capsys):
    backend_cfg = yaml.safe_load(Path(pconf.config_path()).read_text())
    opts = backend_cfg["integrations"]["p1"]["options"]
    Path(pconf.config_path()).write_text(yaml.safe_dump({"integrations": {"p1": {"driver": "prometheus", "options": opts,
        "auth": {"type": "bearer", "token": "${env:PROM_TEST_TOKEN}"}}}}))
    backend["routes"][("GET", "/api/v1/query")] = _vec(({"job": "a"}, 1))
    backend["require_auth"] = "Bearer s3cret"
    assert cli.main(["--instance", "p1", "query", "up"]) == 0
    assert "s3cret" not in capsys.readouterr().out
    backend["require_auth"] = "Bearer other"
    assert cli.main(["--instance", "p1", "query", "up"]) == 1
    err = capsys.readouterr().err
    assert "401" in err and "abc123" not in err and "s3cret" not in err


def test_range_query_is_bounded_and_summarized(backend, capsys):
    backend["routes"][("GET", "/api/v1/query_range")] = {"status": "success", "data": {"resultType": "matrix", "result": [
        {"metric": {"job": "a"}, "values": [[1, "1"], [2, "3"], [3, "NaN"]]}]}}
    assert cli.main(["--instance", "p1", "range", "up", "--range", "1h"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["series"][0] == {"labels": {"job": "a"}, "points": 3, "last": 3.0, "min": 1.0, "max": 3.0, "avg": 2.0}
    assert out["step_s"] == 8 and "start=" in backend["calls"][-1][1]
    assert cli.main(["--instance", "p1", "range", "up", "--range", "8d"]) == 1
    assert cli.main(["--instance", "p1", "range", "up", "--range", "7d", "--step", "10s"]) == 1


def test_silence_create_and_expire_are_dry_runs_without_yes(backend, capsys):
    backend["routes"][("POST", "/api/v2/silences")] = {"silenceID": "abc-123"}
    backend["routes"][("GET", "/api/v2/silence/abcdef12")] = {"id": "abcdef12", "status": {"state": "active"},
                                                             "matchers": [{"name": "a", "value": "b", "isRegex": False, "isEqual": True}]}
    backend["routes"][("DELETE", "/api/v2/silence/abcdef12")] = {}
    base = ["--instance", "p1", "silence-create", "--matcher", "alertname=Foo", "--matcher", "env!~dev.*", "--duration", "2h", "--comment", "maintenance"]
    assert cli.main(base) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    assert not [c for c in backend["calls"] if c[0] == "POST"]
    assert cli.main(base + ["--yes"]) == 0
    assert backend["body"]["matchers"][1] == {"name": "env", "value": "dev.*", "isRegex": True, "isEqual": False}
    assert json.loads(capsys.readouterr().out)["silenceID"] == "abc-123"
    assert cli.main(["--instance", "p1", "silence-expire", "abcdef12"]) == 0
    assert not [c for c in backend["calls"] if c[0] == "DELETE"]
    assert cli.main(["--instance", "p1", "silence-expire", "abcdef12", "--yes"]) == 0
    assert [c for c in backend["calls"] if c[0] == "DELETE"]
    assert cli.main(["--instance", "p1", "silence-create", "--matcher", "a=~.*", "--duration", "1h", "--comment", "x", "--yes"]) == 1
    assert cli.main(["--instance", "p1", "silence-expire", "../x"]) == 1


def test_alertmanager_commands_fail_clearly_without_alertmanager_url(backend, capsys):
    backend["configure"](am=False)
    assert cli.main(["--instance", "p1", "silences"]) == 1
    assert "alertmanager_url" in capsys.readouterr().err


def test_test_subcommand_reports_each_step(backend, capsys):
    backend["routes"][("GET", "/api/v1/status/buildinfo")] = {"status": "success", "data": {"version": "2.50.0"}}
    backend["routes"][("GET", "/api/v1/targets")] = {"status": "success", "data": {"activeTargets": [{"health": "up"}, {"health": "down"}]}}
    backend["routes"][("GET", "/api/v2/status")] = {"versionInfo": {"version": "0.27"}}
    assert cli.main(["--instance", "p1", "test"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["ok"] and [s["step"] for s in res["steps"]] == ["python", "config", "prometheus", "targets", "alertmanager"]
    assert "1 down" in res["steps"][3]["detail"]
    del backend["routes"][("GET", "/api/v1/status/buildinfo")]
    cli.main(["--instance", "p1", "test"])
    assert not json.loads(capsys.readouterr().out)["ok"]


def _run(argv, capsys):
    rc = pw.main(argv)
    return rc, capsys.readouterr().out


def test_alert_watcher_baselines_then_reports_new_and_resolved_alerts(backend, capsys):
    def alerts(*names):
        return {"status": "success", "data": {"alerts": [
            {"labels": {"alertname": n, "severity": "critical"}, "state": "firing", "activeAt": "t", "annotations": {"summary": "s"}} for n in names]}}
    backend["routes"][("GET", "/api/v1/alerts")] = alerts("A")
    args = ["alerts", "--instance", "p1", "--label", "severity=critical", "--resolved"]
    assert _run(args, capsys) == (0, "")
    backend["routes"][("GET", "/api/v1/alerts")] = alerts("A", "B")
    rc, out = _run(args, capsys)
    assert rc == 200 and "[firing] B" in out and "A " not in out
    backend["routes"][("GET", "/api/v1/alerts")] = alerts("B")
    rc, out = _run(args, capsys)
    assert rc == 200 and "[resolved]" in out and "alertname=A" in out
    assert _run(args, capsys) == (0, "")


def test_target_down_watcher_reports_newly_down_and_recovered_targets(backend, capsys):
    def tg(health):
        return {"status": "success", "data": {"activeTargets": [
            {"labels": {"job": "node", "instance": "h1:9100"}, "health": health, "lastError": "connection refused"}]}}
    backend["routes"][("GET", "/api/v1/targets")] = tg("up")
    args = ["target-down", "--instance", "p1", "--job", "node", "--resolved"]
    assert _run(args, capsys) == (0, "")
    backend["routes"][("GET", "/api/v1/targets")] = tg("down")
    rc, out = _run(args, capsys)
    assert rc == 200 and "h1:9100" in out and "connection refused" in out
    assert _run(args, capsys) == (0, "")
    backend["routes"][("GET", "/api/v1/targets")] = tg("up")
    rc, out = _run(args, capsys)
    assert rc == 200 and "up again" in out


def test_threshold_watcher_fires_when_a_series_crosses_the_value(backend, capsys):
    backend["routes"][("GET", "/api/v1/query")] = _vec(({"instance": "a"}, 10))
    args = ["threshold", "--instance", "p1", "--query", "cpu", "--op", "gt", "--value", "80", "--resolved"]
    assert _run(args, capsys) == (0, "")
    backend["routes"][("GET", "/api/v1/query")] = _vec(({"instance": "a"}, 95))
    rc, out = _run(args, capsys)
    assert rc == 200 and "instance=a = 95" in out
    assert _run(args, capsys) == (0, "")
    assert pw.main(["threshold", "--instance", "p1", "--query", "cpu"]) == 1


def test_watcher_treats_server_errors_as_transient_and_auth_errors_as_failures(backend, capsys):
    backend["routes"][("GET", "/api/v1/targets")] = {"status": "success", "data": {"activeTargets": []}}
    assert pw.main(["target-down", "--instance", "p1"]) == 0
    backend["routes"].clear()
    assert pw.main(["target-down", "--instance", "p1"]) == 1
    assert pw.main(["target-down", "--instance", "nope"]) == 1


def test_watch_and_cli_scripts_are_not_group_or_world_writable():
    import stat
    mode = (DRV / "watch" / "prometheus_watch.py").stat().st_mode
    assert mode & stat.S_IXUSR and not mode & (stat.S_IWGRP | stat.S_IWOTH)
