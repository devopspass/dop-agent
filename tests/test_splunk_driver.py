"""Splunk package driver: manifest/skill render, read-only SPL guard, async search, CLI `test`, watcher.

Feature: Splunk is a read-only package driver (CLI + skill + two watcher recipes). Guarantees: every instance gets
its own skill without leftover templates; SPL that writes/executes is refused before any request; searches are
bounded (window, result cap) and a job that does not finish is cancelled; the token is only sent as a Bearer
header and never printed (errors are redacted); the watcher records a silent baseline then reports each new
fired alert / new result row once. Everything runs against a local fake Splunk HTTP server (no credentials).
"""
import importlib.util
import json
import stat
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from integrations.registry import build_registry
from integrations.skills import ensure_skills

DRV = Path(__file__).resolve().parents[1] / "drivers" / "splunk"
sys.path.insert(0, str(DRV / "cli"))
import splunk_api as api  # noqa: E402
import splunk_config as sconf  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cli = _load("splunk_cli_mod", DRV / "cli" / "cli.py")
sw = _load("splunk_watch_mod", DRV / "watch" / "splunk_watch.py")
TOKEN = "s3cr3t-token-value"


class Fake(BaseHTTPRequestHandler):
    state = {"rows": [{"host": "a", "_time": "t1", "_raw": "x"}], "fired": ["sid1"], "polls": 0, "never_done": False,
             "posts": [], "auth": set()}

    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        st = Fake.state
        st["auth"].add(self.headers.get("Authorization"))
        path = urlparse(self.path).path
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            return self._send({"messages": [{"text": "Unauthorized"}]}, 401)
        if path.endswith("/server/info"):
            return self._send({"entry": [{"content": {"version": "9.2.1", "product_type": "enterprise"}}]})
        if path.endswith("/current-context"):
            return self._send({"entry": [{"content": {"username": "bot", "roles": ["user"]}}]})
        if path.endswith("/fired_alerts"):
            return self._send({"entry": [{"name": "CPU high", "content": {"triggered_alert_count": len(st["fired"])}}]})
        if "/fired_alerts/" in path:
            return self._send({"entry": [{"name": s, "content": {"severity": 3, "trigger_time": 1}} for s in st["fired"]]})
        if path.endswith("/results"):
            return self._send({"results": st["rows"]})
        if "/search/jobs/" in path:
            st["polls"] += 1
            done = not st["never_done"]
            return self._send({"entry": [{"content": {"dispatchState": "DONE" if done else "RUNNING", "isDone": done,
                                                      "resultCount": len(st["rows"]), "search": "search x"}}]})
        if path.endswith("/data/indexes"):
            return self._send({"entry": [{"name": "main", "content": {"totalEventCount": "5"}}]})
        self._send({"messages": [{"text": "nope"}]}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(n).decode())
        Fake.state["posts"].append((urlparse(self.path).path, form))
        if urlparse(self.path).path.endswith("/search/jobs"):
            return self._send({"sid": "job1"})
        self._send({"messages": []})


@pytest.fixture()
def splunk(tmp_path, monkeypatch):
    Fake.state.update(rows=[{"host": "a", "_time": "t1", "_raw": "x"}], fired=["sid1"], polls=0, never_done=False,
                      posts=[], auth=set())
    srv = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"""integrations:
  sp:
    driver: splunk
    enabled: true
    base_url: http://127.0.0.1:{srv.server_port}
    auth: {{type: token, token: '${{env:SPLUNK_TEST_TOKEN}}'}}
    options: {{index: main, max_results: 5}}
""")
    monkeypatch.setenv("DOP_AGENT_CONFIG", str(cfg))
    monkeypatch.setenv("SPLUNK_TEST_TOKEN", TOKEN)
    monkeypatch.setenv("WATCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SPLUNK_INSTANCE", "sp")
    monkeypatch.setattr(api.time, "sleep", lambda s: None)
    yield Fake.state
    srv.shutdown()


def test_each_instance_gets_its_own_skill_with_no_leftover_templates(tmp_path):
    cfg = {"one": {"driver": "splunk", "enabled": True, "base_url": "https://one.example:8089",
                   "auth": {"type": "token", "token": "x"}, "options": {"app": "search"}}}
    reg = build_registry(cfg)
    assert not reg.integrations["one"].errors
    assert "splunk-one" in ensure_skills(reg, tmp_path)
    text = (tmp_path / "splunk-one" / "SKILL.md").read_text()
    assert "https://one.example:8089" in text and "--instance one" in text and "${{" not in text
    assert str(DRV / "cli" / "cli.py") in text
    desc = text.split("\n")[2]
    assert "--instance" not in desc and "cli.py" not in desc


@pytest.mark.parametrize("spl", ["index=a | delete", "| outputlookup x.csv", "index=a | stats count | collect index=b",
                                 "index=a [ | sendemail to=x ]", "| script foo", ""])
def test_write_or_execute_spl_is_refused_before_any_request(spl):
    with pytest.raises(ValueError):
        api.check_spl(spl)


def test_spl_gets_search_prefix_and_default_index_only_when_missing():
    assert api.build_spl("error | head 5", "main") == "search index=main error | head 5"
    assert api.build_spl("index=other error", "main") == "search index=other error"
    assert api.build_spl("| metadata type=sourcetypes", "main") == "| metadata type=sourcetypes"


def test_search_is_bounded_by_window_and_instance_result_cap(splunk, capsys):
    splunk["rows"] = [{"host": str(i), "_time": "t", "_raw": "r", "_cd": "1:2"} for i in range(8)]
    assert cli.main(["--instance", "sp", "search", "error", "--count", "999", "--earliest", "-1h"]) == 0
    out = capsys.readouterr().out
    path, form = splunk["posts"][0]
    assert form["earliest_time"] == ["-1h"] and form["max_count"] == ["5"] and form["search"] == ["search index=main error"]
    assert "host\t_time\t_raw" in out and "_cd" not in out
    assert TOKEN not in out and splunk["auth"] == {f"Bearer {TOKEN}"}


def test_unfinished_search_is_cancelled_and_reported(splunk, monkeypatch):
    splunk["never_done"] = True
    t = iter(range(0, 1000, 10))
    monkeypatch.setattr(api.time, "monotonic", lambda: next(t))
    r = api.run_search(api.client("sp"), "error", timeout=30)
    assert r["state"] == "TIMEOUT" and any(p.endswith("/control") and f == {"action": ["cancel"]} for p, f in splunk["posts"])


def test_cli_test_reports_steps_and_redacts_bad_token(splunk, monkeypatch):
    res = cli.run_test("sp")
    assert res["ok"] and [s["step"] for s in res["steps"]] == ["config", "auth", "whoami"]
    monkeypatch.setenv("SPLUNK_TEST_TOKEN", "wrong-token")
    bad = cli.run_test("sp")
    assert not bad["ok"] and "wrong-token" not in json.dumps(bad)


def test_missing_instance_fails_instead_of_falling_back(splunk):
    with pytest.raises(sconf.InstanceError):
        sconf.load_instance("other")


def test_redact_hides_token_like_values():
    assert "abc123" not in api.redact("Authorization: Bearer abc123 token=abc123")


def test_watcher_fired_alert_baseline_then_reports_new_instance_once(splunk, capsys):
    assert sw.main(["fired"]) == 0
    assert sw.main(["fired"]) == 0
    splunk["fired"].append("sid2")
    assert sw.main(["fired"]) == sw.RC_CHANGED
    out = capsys.readouterr().out
    assert "CPU high" in out and "sid2" in out and "sid1" not in out
    assert sw.main(["fired"]) == 0


def test_watcher_results_reports_only_new_rows_after_baseline(splunk, capsys):
    args = ["results", "--spl", "error | table host"]
    assert sw.main(args) == 0
    splunk["rows"].append({"host": "b", "_time": "t2", "_raw": "y"})
    assert sw.main(args) == sw.RC_CHANGED
    out = capsys.readouterr().out
    assert "host=b" in out and "host=a" not in out
    assert sw.main(args) == 0


def test_watcher_rejects_write_spl_with_error_exit(splunk, capsys):
    assert sw.main(["results", "--spl", "index=a | delete"]) == 1
    assert "not allowed" in capsys.readouterr().err


def test_watcher_and_cli_scripts_are_executable_and_not_group_writable():
    for p in (DRV / "watch" / "splunk_watch.py", DRV / "cli" / "cli.py"):
        mode = p.stat().st_mode
        assert mode & stat.S_IXUSR and not mode & (stat.S_IWGRP | stat.S_IWOTH)


def test_list_limits_never_become_unbounded_count_zero():
    """Splunk treats count=0 as 'return everything'; --limit 0 must be clamped to at least 1 (and at most 200)."""
    assert api.lim(0) == 1 and api.lim(-5) == 1 and api.lim(10**6) == 200 and api.lim(30) == 30
