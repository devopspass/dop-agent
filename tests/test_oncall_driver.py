"""oncall package driver (PagerDuty / Opsgenie): CLI behaviour, watcher semantics, skill rendering, options, token safety.

Feature: `drivers/oncall` serves standalone PagerDuty or Opsgenie through one command set selected by the `backend` option
(not the Opsgenie-inside-JSM case, which stays in the `jsm` driver). Writes are confirm-first: without --confirm only a PREVIEW is
printed and no request is made. Watchers record a silent baseline and exit 200 only for real changes; transient HTTP errors stay
silent. The HTTP seam (`cli.call`) is replaced by a strict fake so unexpected requests fail the test; no real credentials are used.
"""
import importlib.util
import json
import re
from pathlib import Path

import pytest

from integrations.registry import build_registry
from integrations.skills import ensure_skills

ROOT = Path(__file__).resolve().parents[1] / "drivers" / "oncall"
PD_BASE, OG_BASE = "https://api.pagerduty.example", "https://api.opsgenie.example"


def _inst(backend, base, **opts):
    return {"driver": "oncall", "base_url": base, "auth": {"type": "api_key", "token": "${env:ONCALL_TEST_TOKEN}"},
            "options": {"backend": backend, "title": "Main", **opts}}


def _load(rel, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _cli(backend, **kw):
    m = _load("cli/cli.py", f"oncall_cli_{backend}")
    m.use(PD_BASE if backend == "pagerduty" else OG_BASE, backend, "unit-test-token", **kw)
    m.configure = lambda instance=None: None
    return m


@pytest.fixture
def pdc():
    return _cli("pagerduty", from_email="me@example.com")


@pytest.fixture
def ogc():
    return _cli("opsgenie", subdomain="acme")


class Fake:
    """Route table: (method, path) -> response | callable(query, data) | 'HTTP ...' error string; unknown routes fail the test."""

    def __init__(self, cli, routes):
        self.routes, self.calls = routes, []
        cli.call = self

    def __call__(self, method, path, query=None, data=None):
        self.calls.append((method, path, query, data))
        r = self.routes.get((method, path))
        if r is None:
            raise AssertionError(f"unexpected {method} {path}")
        if isinstance(r, str):
            raise SystemExit(r)
        return r(query, data) if callable(r) else r


def run(cli, argv, capsys):
    cli.main(argv)
    return capsys.readouterr().out


PD_INC = {"id": "PABC", "incident_number": 7, "title": "DB down", "status": "triggered", "urgency": "high",
          "service": {"summary": "Billing"}, "created_at": "2026-01-02T03:04:05Z", "html_url": "https://x.pagerduty.example/i/7",
          "assignments": [{"assignee": {"summary": "Ann"}}]}
OG_ALERT = {"id": "uuid-1", "tinyId": "42", "message": "Disk full", "status": "open", "acknowledged": False, "priority": "P2",
            "teams": [{"name": "infra"}], "createdAt": "2026-01-02T03:04:05.1Z", "owner": ""}


# ---------- incidents / alerts ----------
def test_pagerduty_incidents_use_open_status_and_service_filters(pdc, capsys):
    f = Fake(pdc, {("GET", "/incidents"): {"incidents": [PD_INC], "more": False}})
    out = run(pdc, ["incidents", "--service", "PSVC", "--team", "PTEAM"], capsys)
    assert "PABC #7 [triggered] high DB down | Billing | 2026-01-02T03:04 | Ann" in out and "-- 1 incident(s)" in out
    q = f.calls[0][2]
    assert q["statuses[]"] == ["triggered", "acknowledged"] and q["service_ids[]"] == ["PSVC"] and q["team_ids[]"] == ["PTEAM"]


def test_opsgenie_alerts_map_to_incident_status_and_team_query(ogc, capsys):
    ack = {**OG_ALERT, "id": "uuid-2", "tinyId": "43", "acknowledged": True}
    f = Fake(ogc, {("GET", "/v2/alerts"): {"data": [OG_ALERT, ack]}})
    out = run(ogc, ["incidents", "--team", "infra"], capsys)
    assert "uuid-1 #42 [triggered] P2 Disk full | infra" in out and "[acknowledged]" in out and "-- 2 alert(s)" in out
    assert f.calls[0][2]["query"] == 'status: open AND teams: "infra"'


def test_opsgenie_rejects_service_filter_and_injected_query(ogc, capsys):
    f = Fake(ogc, {})
    with pytest.raises(SystemExit):
        run(ogc, ["incidents", "--service", "x"], capsys)
    with pytest.raises(SystemExit):
        run(ogc, ["incidents", "--team", 'a" OR status:closed "'], capsys)
    assert f.calls == []


def test_incident_detail_shows_link_and_notes_for_both_backends(pdc, ogc, capsys):
    Fake(pdc, {("GET", "/incidents/PABC"): {"incident": PD_INC},
               ("GET", "/incidents/PABC/notes"): {"notes": [{"created_at": "2026-01-02T04:00:00Z", "user": {"summary": "Bob"}, "content": "looking"}]}})
    out = run(pdc, ["incident", "PABC", "--notes", "2"], capsys)
    assert "url: https://x.pagerduty.example/i/7" in out and "note 2026-01-02T04:00 Bob: looking" in out
    f = Fake(ogc, {("GET", "/v2/alerts/42"): {"data": OG_ALERT}, ("GET", "/v2/alerts/42/notes"): {"data": [{"createdAt": "2026-01-02T04:00:00Z", "owner": "bob", "note": "hi"}]}})
    out = run(ogc, ["incident", "42", "--notes", "1"], capsys)
    assert "url: https://acme.app.opsgenie.com/alert/detail/uuid-1/details" in out and "note 2026-01-02T04:00 bob: hi" in out
    assert f.calls[0][2] == {"identifierType": "tiny"}


def test_timeline_is_sorted_and_compact(pdc, capsys):
    Fake(pdc, {("GET", "/incidents/PABC/log_entries"): {"log_entries": [
        {"created_at": "2026-01-02T03:10:00Z", "type": "acknowledge_log_entry", "agent": {"summary": "Ann"}, "summary": "Acked"},
        {"created_at": "2026-01-02T03:04:00Z", "type": "trigger_log_entry", "summary": "Triggered"}], "more": False}})
    out = run(pdc, ["timeline", "PABC"], capsys).splitlines()
    assert out[0].startswith("2026-01-02T03:04 trigger") and out[1].startswith("2026-01-02T03:10 acknowledge Ann") and out[-1] == "-- 2 event(s)"


# ---------- on-call / escalations / services ----------
def test_oncall_by_schedule_name_pagerduty(pdc, capsys):
    f = Fake(pdc, {("GET", "/schedules"): {"schedules": [{"id": "PS1", "name": "Primary"}, {"id": "PS2", "name": "Primary EU"}], "more": False},
                   ("GET", "/oncalls"): {"oncalls": [{"user": {"summary": "Ann"}, "start": "2026-01-01T00:00:00Z", "end": "2026-01-08T00:00:00Z"}], "more": False}})
    out = run(pdc, ["oncall", "--name", "Primary"], capsys)
    assert "Primary: Ann (2026-01-01T00:00 -> 2026-01-08T00:00)" in out
    assert f.calls[-1][2]["schedule_ids[]"] == ["PS1"]


def test_oncall_next_opsgenie_uses_next_endpoint(ogc, capsys):
    f = Fake(ogc, {("GET", "/v2/schedules"): {"data": [{"id": "S1", "name": "Ops"}]},
                   ("GET", "/v2/schedules/S1/next-on-calls"): {"data": {"nextOnCallRecipients": ["bob"]}}})
    out = run(ogc, ["oncall", "--schedule", "S1", "--next"], capsys)
    assert "Ops: bob" in out and f.calls[-1][2]["flat"] == "true"


def test_escalation_policy_levels_are_listed(pdc, ogc, capsys):
    Fake(pdc, {("GET", "/escalation_policies/PE1"): {"escalation_policy": {"id": "PE1", "name": "Default", "num_loops": 2, "services": [{"summary": "Billing"}],
               "escalation_rules": [{"escalation_delay_in_minutes": 30, "targets": [{"summary": "Ann", "type": "user_reference"}]}]}}})
    out = run(pdc, ["escalation", "PE1"], capsys)
    assert "level 1: after 30 min -> Ann (user)" in out and "services: Billing" in out
    Fake(ogc, {("GET", "/v2/escalations/E1"): {"data": {"id": "E1", "name": "Esc", "rules": [{"condition": "if-not-acked", "notifyType": "default", "delay": {"timeAmount": 5}, "recipient": {"type": "user", "username": "bob"}}]}}})
    assert "rule 1: if-not-acked default after 5 min -> user bob" in run(ogc, ["escalation", "E1"], capsys)


def test_services_on_opsgenie_falls_back_to_teams(ogc, capsys):
    Fake(ogc, {("GET", "/v2/teams"): {"data": [{"id": "T1", "name": "infra"}]}})
    out = run(ogc, ["services"], capsys)
    assert "listing teams" in out and "T1 infra" in out


# ---------- writes: confirm-first ----------
def test_writes_without_confirm_only_preview_and_send_nothing(pdc, ogc, capsys):
    f = Fake(pdc, {("GET", "/schedules"): {"schedules": [{"id": "PS1", "name": "P"}], "more": False}})
    for argv in (["ack", "PABC"], ["resolve", "PABC"], ["note", "PABC", "hello"],
                 ["override", "PS1", "--user", "PU1", "--start", "2026-03-01T09:00:00+01:00", "--end", "2026-03-01T17:00:00+01:00"]):
        assert run(pdc, argv, capsys).startswith("PREVIEW (nothing sent")
    assert all(c[0] == "GET" for c in f.calls)
    f = Fake(ogc, {})
    assert run(ogc, ["resolve", "42"], capsys).startswith("PREVIEW") and f.calls == []


def test_confirmed_pagerduty_ack_and_note_payloads(pdc, capsys):
    f = Fake(pdc, {("PUT", "/incidents"): {}, ("POST", "/incidents/PABC/notes"): {}})
    assert run(pdc, ["ack", "PABC", "--confirm"], capsys).startswith("OK acknowledge incident PABC")
    run(pdc, ["note", "PABC", "checked", "--confirm"], capsys)
    assert f.calls[0][3] == {"incidents": [{"id": "PABC", "type": "incident_reference", "status": "acknowledged"}]}
    assert f.calls[1][3] == {"note": {"content": "checked"}}


def test_confirmed_opsgenie_close_and_override_payloads(ogc, capsys):
    f = Fake(ogc, {("POST", "/v2/alerts/42/close"): {}, ("GET", "/v2/schedules"): {"data": [{"id": "S1", "name": "Ops"}]},
                   ("POST", "/v2/schedules/S1/overrides"): {}})
    run(ogc, ["resolve", "42", "--note", "fixed", "--confirm"], capsys)
    run(ogc, ["override", "S1", "--user", "bob@example.com", "--start", "2026-03-01T09:00:00+01:00", "--end", "2026-03-01T17:00:00Z", "--confirm"], capsys)
    close = next(c for c in f.calls if c[1].endswith("/close"))
    assert close[3] == {"note": "fixed"} and close[2] == {"identifierType": "tiny"}
    ov = next(c for c in f.calls if c[1].endswith("/overrides"))
    assert ov[3] == {"user": {"type": "user", "username": "bob@example.com"}, "startDate": "2026-03-01T08:00:00Z", "endDate": "2026-03-01T17:00:00Z"}


def test_override_rejects_reversed_window_and_raw_write_needs_confirm(pdc, capsys):
    Fake(pdc, {("GET", "/schedules"): {"schedules": [{"id": "PS1", "name": "P"}], "more": False}})
    with pytest.raises(SystemExit) as e:
        run(pdc, ["override", "PS1", "--user", "PU1", "--start", "2026-03-02T00:00:00Z", "--end", "2026-03-01T00:00:00Z"], capsys)
    assert "end must be after start" in str(e.value)
    f = Fake(pdc, {})
    assert run(pdc, ["raw", "DELETE", "/incidents/PABC"], capsys).startswith("PREVIEW") and f.calls == []


def test_pagerduty_write_without_from_email_fails_before_network(monkeypatch):
    m = _cli("pagerduty")
    m.use(PD_BASE, "pagerduty", "tok")
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("network used"))
    with pytest.raises(SystemExit) as e:
        m.call("PUT", "/incidents", None, {})
    assert "from_email" in str(e.value)


def test_pagerduty_preview_fails_early_without_from_email_and_rejects_note_flag(capsys):
    m = _cli("pagerduty")
    f = Fake(m, {})
    with pytest.raises(SystemExit) as e:
        run(m, ["ack", "PABC"], capsys)
    assert "from_email" in str(e.value) and f.calls == []
    m.use(PD_BASE, "pagerduty", "tok", from_email="me@example.com")
    with pytest.raises(SystemExit) as e:
        run(m, ["resolve", "PABC", "--note", "x"], capsys)
    assert "Opsgenie-only" in str(e.value)


@pytest.mark.parametrize("bad", ["../x", "a b", "x;y", ""])
def test_bad_ids_are_rejected_before_any_request(pdc, capsys, bad):
    f = Fake(pdc, {})
    with pytest.raises(SystemExit):
        run(pdc, ["incident", bad], capsys)
    assert f.calls == []


# ---------- fetch / test ----------
def test_fetch_builds_card_json(pdc, capsys):
    Fake(pdc, {("GET", "/incidents/PABC"): {"incident": PD_INC}})
    out = json.loads(run(pdc, ["fetch", "--kind", "incident", "--ident", '{"id": "PABC"}'], capsys))
    assert out["title"] == "DB down" and out["status"] == "triggered" and out["url"].endswith("/i/7") and out["fields"]["service"] == "Billing"


def test_test_command_reports_steps_per_backend(pdc, ogc, capsys):
    Fake(pdc, {("GET", "/abilities"): {"abilities": []}, ("GET", "/services"): {"services": [{"id": "S"}]}})
    out = json.loads(run(pdc, ["test"], capsys))
    assert out["ok"] is True and [s["name"] for s in out["steps"]] == ["config", "auth", "data"]
    Fake(ogc, {("GET", "/v2/account"): {"data": {"name": "acme"}}, ("GET", "/v2/teams"): {"data": []}})
    out = json.loads(run(ogc, ["test"], capsys))
    assert out["ok"] is True and "acme" in out["message"]


def test_test_command_fails_with_hint_on_auth_error_and_never_leaks_token(pdc, capsys):
    Fake(pdc, {("GET", "/abilities"): "HTTP 401 GET /abilities: Token token=unit-test-token rejected"})
    out = capsys.readouterr() and run(pdc, ["test"], capsys)
    d = json.loads(out)
    assert d["ok"] is False and "401" in d["message"] and "check auth.token" in d["message"] and "unit-test-token" not in out


def test_redact_hides_token_forms():
    m = _cli("pagerduty")
    assert "unit-test-token" not in m.redact("boom unit-test-token") and "secret123" not in m.redact("Authorization: secret123")


# ---------- watcher ----------
@pytest.fixture
def wmod():
    return _load("watch/oncall_watch.py", "oncall_watch_t")


def _watch(wmod, monkeypatch, tmp_path, cli, argv):
    monkeypatch.setenv("WATCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(wmod, "load_cli", lambda: cli)
    return wmod.main(argv)


def test_incident_watch_baseline_then_new_only_then_optional_resolved(wmod, monkeypatch, tmp_path, pdc, capsys):
    items = {"v": [PD_INC]}
    Fake(pdc, {("GET", "/incidents"): lambda q, d: {"incidents": items["v"], "more": False}})
    go = lambda *extra: _watch(wmod, monkeypatch, tmp_path, pdc, ["incidents", "--team", "PT", *extra])
    assert go() == 0 and capsys.readouterr().out == ""
    items["v"] = [PD_INC, {**PD_INC, "id": "PNEW", "incident_number": 8}]
    assert go() == 200
    out = capsys.readouterr().out
    assert "PNEW" in out and "PABC" not in out
    assert go() == 0
    items["v"] = [{**PD_INC, "id": "PNEW", "incident_number": 8}]
    assert go() == 0
    items["v"] = []
    assert go("--report-resolved") == 200 and "GONE  id=PNEW" in capsys.readouterr().out


def test_oncall_watch_reports_change_only(wmod, monkeypatch, tmp_path, ogc, capsys):
    now = {"v": ["ann"]}
    Fake(ogc, {("GET", "/v2/schedules"): {"data": [{"id": "S1", "name": "Ops"}]},
               ("GET", "/v2/schedules/S1/on-calls"): lambda q, d: {"data": {"onCallRecipients": now["v"]}}})
    go = lambda: _watch(wmod, monkeypatch, tmp_path, ogc, ["oncall", "--schedule", "S1"])
    assert go() == 0 and go() == 0
    now["v"] = ["bob"]
    assert go() == 200 and "on-call changed in Ops: ann -> bob" in capsys.readouterr().out


def test_transient_error_is_silent_but_auth_error_fails(wmod, monkeypatch, tmp_path, pdc):
    Fake(pdc, {("GET", "/incidents"): "HTTP 503 GET /incidents: x"})
    assert _watch(wmod, monkeypatch, tmp_path, pdc, ["incidents"]) == 0
    Fake(pdc, {("GET", "/incidents"): "HTTP 401 GET /incidents: x"})
    assert _watch(wmod, monkeypatch, tmp_path, pdc, ["incidents"]) == 1


def test_watcher_script_is_executable_and_not_writable_by_others():
    mode = (ROOT / "watch" / "oncall_watch.py").stat().st_mode
    assert mode & 0o100 and not mode & 0o022


# ---------- manifest / skill / config ----------
def test_skill_renders_generic_with_absolute_paths_and_no_cli_hint_in_description(tmp_path):
    reg = build_registry({"pd-main": _inst("pagerduty", PD_BASE)})
    assert ensure_skills(reg, tmp_path) == ["oncall-pd-main"]
    text = (tmp_path / "oncall-pd-main" / "SKILL.md").read_text()
    assert "${{" not in text and PD_BASE in text and "--instance pd-main" in text and "unit-test-token" not in text
    assert str(ROOT / "cli" / "cli.py") in text and str(ROOT / "watch") in text and "jsm" in text
    desc = re.search(r'^description: "(.*)"$', text, re.M).group(1)
    assert "--instance" not in desc and "cli.py" not in desc and desc.startswith("Use when: ANY ")


@pytest.mark.parametrize("opts", [{}, {"backend": "datadog"}, {"backend": "opsgenie", "subdomain": "a b"}, {"backend": "pagerduty", "from_email": "a b@x"}])
def test_invalid_or_missing_options_are_rejected(opts):
    assert build_registry({"o": {**_inst("pagerduty", PD_BASE), "options": opts}}).get("o").errors


def test_valid_instances_load_without_errors():
    reg = build_registry({"a": _inst("pagerduty", PD_BASE, from_email="me@example.com"), "b": _inst("opsgenie", OG_BASE, subdomain="acme")})
    assert not reg.get("a").errors and not reg.get("b").errors




def _cfg(tmp_path, monkeypatch, doc):
    import yaml
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({"integrations": doc}))
    monkeypatch.setenv("DOP_AGENT_CONFIG", str(p))


def test_configure_resolves_token_by_instance_and_never_falls_back(tmp_path, monkeypatch):
    m = _load("cli/cli.py", "oncall_cli_cfg")
    monkeypatch.setenv("ONCALL_TEST_TOKEN", "tok-from-env")
    monkeypatch.delenv("ONCALL_INSTANCE", raising=False)
    _cfg(tmp_path, monkeypatch, {"a": {**_inst("pagerduty", PD_BASE), "enabled": True}, "b": {**_inst("opsgenie", OG_BASE), "enabled": False}})
    m.configure("a")
    assert m.CONF["backend"] == "pagerduty" and "tok-from-env" not in json.dumps(m.CONF)
    with pytest.raises(SystemExit) as e:
        m.configure("b")
    assert "not found/enabled" in str(e.value) and "tok-from-env" not in str(e.value)


def test_configure_needs_instance_when_ambiguous_and_rejects_empty_token(tmp_path, monkeypatch):
    m = _load("cli/cli.py", "oncall_cli_cfg2")
    monkeypatch.delenv("ONCALL_INSTANCE", raising=False)
    monkeypatch.setenv("ONCALL_TEST_TOKEN", "")
    _cfg(tmp_path, monkeypatch, {"a": _inst("pagerduty", PD_BASE), "b": _inst("opsgenie", OG_BASE)})
    with pytest.raises(SystemExit) as e:
        m.configure(None)
    assert "--instance" in str(e.value)
    with pytest.raises(SystemExit) as e:
        m.configure("a")
    assert "auth.token" in str(e.value)
