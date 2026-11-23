"""JSM package driver: CLI behaviour, watcher semantics, skill rendering, option validation, genericity.

Feature: `drivers/jsm` serves Jira Service Management (requests, queues, SLAs, approvals via the service desk REST API;
Ops alerts and on-call via the Ops REST API). Writes support --dry-run and never send anything then; watcher modes record a
silent baseline and exit 200 only for real changes; transient HTTP errors stay silent; nothing site-specific is built in.
The HTTP layer (`cli.call`) is replaced by a strict fake so unexpected requests fail the test.
"""
import importlib.util
import json
import re
from pathlib import Path

import pytest

from drivers.manifest import substitute_base_url
from integrations.registry import build_registry
from integrations.skills import ensure_skills

ROOT = Path(__file__).resolve().parents[1] / "drivers" / "jsm"
BASE = "https://jsm.example.net"
INST = {"driver": "jsm", "base_url": BASE, "auth": {"type": "api_token", "token": "${env:JSM_TEST_TOKEN}"},
        "options": {"email": "me@example.com", "title": "Main JSM"}}


def _load(rel, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def cli(monkeypatch):
    m = _load("cli/cli.py", "jsm_cli_t")
    m.use(BASE, "me@example.com", "unit-test-token")
    return m


class Fake:
    """Route table: (api, method, path) -> response or callable; unknown routes raise."""

    def __init__(self, cli, routes):
        self.routes, self.calls = routes, []
        cli.call = self

    def __call__(self, api, method, path, query=None, data=None):
        self.calls.append((api, method, path, query, data))
        r = self.routes.get((api, method, path))
        if r is None:
            raise AssertionError(f"unexpected {api} {method} {path}")
        if isinstance(r, str):
            raise SystemExit(r)
        return r(query, data) if callable(r) else r


def _req(key, status="Open", summary="Printer broken"):
    return {"issueKey": key, "currentStatus": {"status": status}, "reporter": {"displayName": "Ann"},
            "createdDate": {"iso8601": "2026-01-02T03:04:05+0000"},
            "requestFieldValues": [{"fieldId": "summary", "label": "Summary", "value": summary}]}


def _run_cli(cli, argv, capsys):
    cli.use(BASE, "me@example.com", "unit-test-token", cli.CONF["cloud_id"])
    cli.configure = lambda instance=None: None  # connection already set by use()
    cli.main(argv)
    return capsys.readouterr().out


def test_requests_listing_is_compact_and_paginated_by_limit(cli, capsys):
    Fake(cli, {("desk", "GET", "/request"): {"values": [_req("ABC-1"), _req("ABC-2", "Resolved")], "isLastPage": True}})
    out = _run_cli(cli, ["requests", "--limit", "5"], capsys)
    assert "ABC-1 [Open] Printer broken | Ann | 2026-01-02T03:04" in out and "-- 2 request(s)" in out


def test_write_commands_dry_run_sends_nothing(cli, capsys):
    f = Fake(cli, {("desk", "GET", "/request/ABC-1/transition"): {"values": [{"id": "11", "name": "Resolve"}]}})
    out = _run_cli(cli, ["comment", "abc-1", "hello", "--dry-run"], capsys)
    assert out.startswith("DRY-RUN POST desk:/request/ABC-1/comment") and '"public": true' in out
    out = _run_cli(cli, ["transition", "ABC-1", "resolve", "--dry-run"], capsys)
    assert "DRY-RUN POST" in out and '"id": "11"' in out
    assert all(c[1] == "GET" for c in f.calls)


def test_internal_comment_is_not_public(cli, capsys):
    f = Fake(cli, {("desk", "POST", "/request/ABC-1/comment"): {}})
    assert _run_cli(cli, ["comment", "ABC-1", "note", "--internal"], capsys).startswith("OK comment on ABC-1 (internal)")
    assert f.calls[0][4] == {"body": "note", "public": False}


def test_approve_requires_exactly_one_answerable_approval(cli, capsys):
    Fake(cli, {("desk", "GET", "/request/ABC-1/approval"): {"values": [
        {"id": "1", "name": "A", "canAnswerApproval": True}, {"id": "2", "name": "B", "canAnswerApproval": True}]}})
    with pytest.raises(SystemExit) as e:
        _run_cli(cli, ["approve", "ABC-1", "--dry-run"], capsys)
    assert "exactly one approval" in str(e.value)
    out = _run_cli(cli, ["approve", "ABC-1", "2", "--dry-run"], capsys)
    assert "/request/ABC-1/approval/2" in out and '"decision": "approve"' in out


@pytest.mark.parametrize("key", ["ABC-1; rm", "../x", "abc", "ABC-"])
def test_bad_request_keys_are_rejected_before_any_request(cli, capsys, key):
    f = Fake(cli, {})
    with pytest.raises(SystemExit):
        _run_cli(cli, ["request", key], capsys)
    assert f.calls == []


def test_sla_rows_flag_breached_and_remaining(cli):
    Fake(cli, {("desk", "GET", "/request/ABC-1/sla"): {"values": [
        {"name": "TTR", "ongoingCycle": {"breached": False, "remainingTime": {"millis": 60000, "friendly": "1m"}, "goalDuration": {"friendly": "4h"}}},
        {"name": "TTFR", "ongoingCycle": {"breached": True, "remainingTime": {"millis": -5, "friendly": "-1m"}}}]}})
    rows = cli.sla_rows("ABC-1")
    assert [(r["name"], r["state"]) for r in rows] == [("TTR", "running"), ("TTFR", "BREACHED")]


def test_oncall_payload_shapes_are_flattened(cli):
    assert cli.flatten({"onCallRecipients": ["u1", "u2"]}) == ["u1", "u2"]
    assert cli.flatten({"onCallParticipants": [{"type": "team", "id": "t", "onCallParticipants": [{"type": "user", "id": "u"}]}]}) == ["team:t", "user:u"]


def test_fetch_builds_furl_card_json(cli, capsys):
    Fake(cli, {("desk", "GET", "/request/ABC-9"): _req("ABC-9", "In progress")})
    out = json.loads(_run_cli(cli, ["fetch", "--kind", "agent_request", "--ident", '{"key": "ABC-9"}'], capsys))
    assert out["title"] == "Printer broken" and out["status"] == "In progress" and out["url"] == f"{BASE}/browse/ABC-9"


def test_test_command_reports_steps_and_ops_is_optional(cli, capsys):
    Fake(cli, {("jira", "GET", "/myself"): {"displayName": "Me"}, ("desk", "GET", "/servicedesk"): {"values": [{"id": "1"}], "isLastPage": True},
               ("ops", "GET", "/schedules"): "HTTP 403 GET ops:/schedules: no scope"})
    cli.CONF["cloud_id"] = "6622a674-c6a6-48a8-bc30-e040b1e28689"
    out = json.loads(_run_cli(cli, ["test"], capsys))
    assert out["ok"] is True and "Ops alerts/on-call unavailable" in out["message"]
    assert [s["name"] for s in out["steps"]] == ["config", "auth", "service_desks", "ops"]


def test_test_command_fails_on_auth_error(cli, capsys):
    Fake(cli, {("jira", "GET", "/myself"): "HTTP 401 GET jira:/myself: bad"})
    out = json.loads(_run_cli(cli, ["test"], capsys))
    assert out["ok"] is False and "401" in out["message"]


def test_token_is_never_printed_in_errors(cli, monkeypatch):
    cli.use(BASE, "me@example.com", "SECRET-TOK")
    _, auth = cli.creds()
    monkeypatch.setattr(cli, "_open", lambda *a, **k: (_ for _ in ()).throw(cli.Http(401, "x")))
    with pytest.raises(SystemExit) as e:
        cli.call("desk", "GET", "/request")
    assert "SECRET-TOK" not in str(e.value) and auth not in str(e.value)


# ---------- watcher ----------
class WCli:
    """Stand-in for the loaded CLI module used by jsm_watch."""
    CONF = {"base_url": BASE}

    def __init__(self, cli, **kw):
        self.real = cli
        self.__dict__.update(kw)

    def configure(self, instance=None):
        pass

    def __getattr__(self, n):
        return getattr(self.real, n)


def _watch(monkeypatch, tmp_path, cli, argv):
    w = _load("watch/jsm_watch.py", "jsm_watch_t")
    monkeypatch.setenv("WATCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(w, "load_cli", lambda: cli)
    monkeypatch.setattr("sys.argv", ["jsm_watch.py", *argv])
    with pytest.raises(SystemExit) as e:
        w.main()
    return e.value.code


def _alert(i, msg="disk"):
    return {"id": i, "tinyId": f"T{i}", "message": msg, "priority": "P1", "status": "open", "createdAt": "x"}


def test_alerts_baseline_silent_then_new_and_gone_reported(monkeypatch, tmp_path, cli, capsys):
    holder = {"rows": [_alert("1")]}
    wc = WCli(cli, fetch_alerts=lambda q, n: holder["rows"])
    assert _watch(monkeypatch, tmp_path, wc, ["alerts"]) == 0
    assert _watch(monkeypatch, tmp_path, wc, ["alerts"]) == 0
    holder["rows"] = [_alert("2")]
    assert _watch(monkeypatch, tmp_path, wc, ["alerts"]) == 200
    out = capsys.readouterr().out
    assert "NEW   T2" in out and "GONE  id=1" in out


def test_requests_watch_reports_new_and_status_change(monkeypatch, tmp_path, cli, capsys):
    rows = {"v": [_req("ABC-1")]}
    wc = WCli(cli, list_requests=lambda *a, **k: rows["v"])
    assert _watch(monkeypatch, tmp_path, wc, ["requests"]) == 0
    rows["v"] = [_req("ABC-1", "Resolved"), _req("ABC-2")]
    assert _watch(monkeypatch, tmp_path, wc, ["requests"]) == 200
    out = capsys.readouterr().out
    assert "[status Open -> Resolved] ABC-1" in out and "[new] ABC-2" in out and f"{BASE}/browse/ABC-2" in out


def test_sla_watch_reports_each_breach_once(monkeypatch, tmp_path, cli, capsys):
    sla = {"v": []}
    wc = WCli(cli, list_requests=lambda *a, **k: [_req("ABC-1")], sla_rows=lambda k: sla["v"])
    assert _watch(monkeypatch, tmp_path, wc, ["sla", "--minutes", "10"]) == 0
    sla["v"] = [{"name": "TTR", "state": "running", "remaining_ms": 60000, "remaining": "1m", "goal": "4h"}]
    assert _watch(monkeypatch, tmp_path, wc, ["sla", "--minutes", "10"]) == 200
    assert "ABC-1 SLA 'TTR' DUE" in capsys.readouterr().out
    assert _watch(monkeypatch, tmp_path, wc, ["sla", "--minutes", "10"]) == 0


def test_oncall_watch_reports_change_only(monkeypatch, tmp_path, cli, capsys):
    now = {"v": ["u1"]}
    wc = WCli(cli, resolve_schedule=lambda s, n: ("S1", "Primary"), on_call=lambda sid: now["v"], user_name=lambda i: i.upper())
    assert _watch(monkeypatch, tmp_path, wc, ["oncall", "--schedule", "S1"]) == 0
    assert _watch(monkeypatch, tmp_path, wc, ["oncall", "--schedule", "S1"]) == 0
    now["v"] = ["u2"]
    assert _watch(monkeypatch, tmp_path, wc, ["oncall", "--schedule", "S1"]) == 200
    assert "Primary: U1 -> U2" in capsys.readouterr().out


def test_queue_watch_needs_ids_and_reports_new_issues(monkeypatch, tmp_path, cli, capsys):
    assert _watch(monkeypatch, tmp_path, WCli(cli), ["queue"]) == 1
    rows = {"v": [{"key": "ABC-1", "fields": {"summary": "a"}}]}
    wc = WCli(cli, queue_issues=lambda d, q, n: rows["v"])
    assert _watch(monkeypatch, tmp_path, wc, ["queue", "--desk", "1", "--queue", "2"]) == 0
    rows["v"].append({"key": "ABC-2", "fields": {"summary": "b"}})
    assert _watch(monkeypatch, tmp_path, wc, ["queue", "--desk", "1", "--queue", "2"]) == 200
    assert "ABC-2" in capsys.readouterr().out


def test_transient_error_is_silent_but_auth_error_fails(monkeypatch, tmp_path, cli):
    def boom(msg):
        def f(*a, **k):
            raise SystemExit(msg)
        return f
    assert _watch(monkeypatch, tmp_path, WCli(cli, fetch_alerts=boom("HTTP 503 GET ops:/alerts: x")), ["alerts"]) == 0
    assert _watch(monkeypatch, tmp_path, WCli(cli, fetch_alerts=boom("HTTP 403 GET ops:/alerts: x")), ["alerts"]) == 1


# ---------- manifest / skill / genericity ----------
def test_skill_renders_generic_with_absolute_paths(tmp_path):
    reg = build_registry({"jsm-main": INST})
    assert ensure_skills(reg, tmp_path) == ["jsm-jsm-main"]
    text = (tmp_path / "jsm-jsm-main" / "SKILL.md").read_text()
    assert "${{" not in text and BASE in text and "--instance jsm-main" in text and "unit-test-token" not in text
    assert str(ROOT / "cli" / "cli.py") in text and str(ROOT / "watch") in text


@pytest.mark.parametrize("opts", [{}, {"email": "a b@x"}, {"email": "a@x.com", "cloud_id": "not-a-guid"}])
def test_invalid_or_missing_options_are_rejected(opts):
    assert build_registry({"j": {**INST, "options": opts}}).get("j").errors


def test_furl_patterns_match_portal_and_agent_links_only_for_the_instance():
    reg = build_registry({"j": INST})
    kinds = reg.manifests["jsm"].kinds
    portal = re.compile(substitute_base_url(kinds["request"].url_match, BASE))
    agent = re.compile(substitute_base_url(kinds["agent_request"].url_match, BASE))
    assert portal.match(f"{BASE}/servicedesk/customer/portal/5/ABC-12").group("key") == "ABC-12"
    assert agent.match(f"{BASE}/jira/servicedesk/projects/ABC/queues/custom/3/ABC-12").group("key") == "ABC-12"
    assert not portal.match("https://other.example.net/servicedesk/customer/portal/5/ABC-12")


# ---------- token convention (same as the gitlab driver: auth.token ref resolved from config.yaml) ----------
def _cfg(tmp_path, monkeypatch, doc):
    import yaml
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({"integrations": doc}))
    monkeypatch.setenv("DOP_AGENT_CONFIG", str(p))


def test_configure_resolves_env_ref_token_from_config_by_instance(cli, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JSM_TEST_TOKEN", "tok-from-env")
    _cfg(tmp_path, monkeypatch, {"a": {**INST, "enabled": True, "options": {"email": "a@example.com", "cloud_id": "auto"}},
                                 "b": {**INST, "base_url": "https://b.example.net", "enabled": False}})
    cli.configure("a")
    assert cli.CONF["base_url"] == BASE and cli.CONF["email"] == "a@example.com" and cli.CONF["cloud_id"] == ""
    assert "tok-from-env" not in json.dumps(cli.CONF)
    with pytest.raises(SystemExit) as e:  # disabled instances are never used as a fallback
        cli.configure("b")
    assert "not found/enabled" in str(e.value) and "tok-from-env" not in str(e.value)


def test_configure_requires_instance_name_when_ambiguous_and_reports_unresolved_token(cli, tmp_path, monkeypatch):
    monkeypatch.delenv("JSM_TEST_TOKEN", raising=False)
    monkeypatch.delenv("JSM_INSTANCE", raising=False)
    _cfg(tmp_path, monkeypatch, {"a": INST, "b": INST})
    with pytest.raises(SystemExit) as e:
        cli.configure(None)
    assert "--instance" in str(e.value)
    with pytest.raises(SystemExit) as e:
        cli.configure("a")
    assert "required" in str(e.value)
