"""ServiceNow package driver: Table API helper, CLI behaviour, watcher semantics, skill rendering, option validation.

Feature: `drivers/servicenow` serves ServiceNow ITSM (incidents, changes, requests/RITMs, tasks, journal, attachments list,
generic read-only table query, assignment groups). Writes are draft-first: without `--confirm` nothing is sent. Watchers record a
silent baseline and exit 200 only for real changes; transient HTTP errors stay silent. The HTTP layer (`snowapi.request`) is
replaced by a strict fake so unexpected requests fail the test; no real instance or credentials are needed.
"""
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

from drivers.manifest import substitute_base_url
from integrations.registry import build_registry
from integrations.skills import ensure_skills

ROOT = Path(__file__).resolve().parents[1] / "drivers" / "servicenow"
BASE = "https://snow.example.net"
SYS = "a" * 32
INST = {"driver": "servicenow", "base_url": BASE, "auth": {"type": "basic", "token": "${env:SNOW_TEST_TOKEN}"},
        "options": {"username": "svc.user", "title": "Main SNOW"}}


def _load(rel, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def cli(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "cli"))
    m = _load("cli/cli.py", "snow_cli_t")
    monkeypatch.setattr(m.api, "request", m.api.request)  # restored after the test (Fake replaces it)
    monkeypatch.setattr(m.api, "configure", lambda instance=None: None)  # connection already set by use()
    m.api.use(BASE, "svc.user", "unit-test-token")
    return m


class Fake:
    """Replaces snowapi.request; routes are (method, path, table-or-None) -> result dict / callable(query, data) / ApiError."""

    def __init__(self, cli, routes):
        self.routes, self.calls = routes, []
        cli.api.request = self

    def __call__(self, method, path, query=None, data=None):
        self.calls.append((method, path, query, data))
        r = self.routes.get((method, path))
        if r is None:
            raise AssertionError(f"unexpected {method} {path} {query}")
        r = r(query, data) if callable(r) else r
        if isinstance(r, Exception):
            raise r
        return r


def cell(v, d=None):
    return {"value": v, "display_value": d if d is not None else v}


def row(number, state="In Progress", **kw):
    r = {"number": cell(number), "short_description": cell("Printer broken"), "state": cell("2", state), "priority": cell("2", "2 - High"),
         "assigned_to": cell("u1", "Ann"), "assignment_group": cell("g1", "Service Desk"), "sys_updated_on": cell("2026-01-02 03:04:05"),
         "sys_id": cell(SYS), "approval": cell("requested", "Requested")}
    r.update(kw)
    return r


def run(cli, argv, capsys):
    cli.main(argv)
    return capsys.readouterr().out


# ---------- Table API helper ----------
def test_table_list_builds_query_order_and_clamps_limit(cli):
    f = Fake(cli, {("GET", "/api/now/table/incident"): {"result": []}})
    cli.api.table_list("incident", "active=true", ["number"], 9999, order="-sys_updated_on")
    q = f.calls[0][2]
    assert q["sysparm_query"] == "active=true^ORDERBYDESCsys_updated_on" and q["sysparm_limit"] == 200 and q["sysparm_display_value"] == "all"


@pytest.mark.parametrize("table", ["Incident", "a;b", "../x", "x"])
def test_bad_table_names_rejected_before_any_request(cli, table):
    f = Fake(cli, {})
    with pytest.raises(cli.ApiError):
        cli.api.table_list(table)
    assert f.calls == []


def test_user_query_blocks_server_side_scripts_and_clause_injection_in_values(cli):
    with pytest.raises(cli.ApiError):
        cli.api.check_user_query("assigned_to=javascript:gs.getUserID()")
    with pytest.raises(cli.ApiError):
        cli.group_clause("x^active=false")
    assert cli.group_clause("Service Desk") == "assignment_group.name=Service Desk"
    assert cli.group_clause(SYS) == f"assignment_group={SYS}"


def test_auth_headers_basic_and_bearer_and_token_never_in_errors(cli, monkeypatch):
    cli.api.use(BASE, "svc.user", "s3cret-token", "basic")
    assert cli.api._auth().startswith("Basic ")
    cli.api.use(BASE, "", "s3cret-token", "bearer")
    assert cli.api._auth() == "Bearer s3cret-token"
    assert "s3cret-token" not in cli.api.redact("HTTP 401: bad token=s3cret-token and Authorization: Bearer s3cret-token")
    with pytest.raises(cli.ApiError):
        cli.api.request("GET", "https://evil.example/api/now/table/x")


# ---------- CLI reads ----------
def test_incidents_listing_is_compact_and_scoped_to_me_and_group(cli, capsys):
    f = Fake(cli, {("GET", "/api/now/table/incident"): {"result": [row("INC0010001")]}})
    out = run(cli, ["incidents", "--mine", "--group", "Service Desk", "--limit", "5"], capsys)
    assert "INC0010001 | In Progress | P2 | Ann | Service Desk | 2026-01-02 03:04:05" in out and "incident: 1 row(s)" in out
    assert f.calls[0][2]["sysparm_query"].startswith("active=true^assigned_to=javascript:gs.getUserID()^assignment_group.name=Service Desk")


def test_cutover_lists_changes_with_schedule_window(cli, capsys):
    r = row("CHG0030001", start_date=cell("2026-02-01 22:00:00"), end_date=cell("2026-02-02 02:00:00"))
    f = Fake(cli, {("GET", "/api/now/table/change_request"): {"result": [r]}})
    out = run(cli, ["cutover", "--days", "7"], capsys)
    assert "2026-02-01 22:00:00→2026-02-02 02:00:00" in out and "approval=Requested" in out
    assert "start_date>=" in f.calls[0][2]["sysparm_query"] and f.calls[0][2]["sysparm_query"].endswith("ORDERBYstart_date")


def test_ticket_prefix_maps_to_table_and_unknown_prefix_fails(cli, capsys):
    f = Fake(cli, {("GET", "/api/now/table/change_request"): {"result": [row("CHG0030001")]},
                   ("GET", f"/api/now/table/change_request/{SYS}"): {"result": row("CHG0030001")},
                   ("GET", "/api/now/table/sys_journal_field"): {"result": [
                       {"element": cell("work_notes"), "value": cell("checked"), "sys_created_by": cell("bob"), "sys_created_on": cell("2026-01-01 00:00:00")}]}})
    out = run(cli, ["ticket", "chg0030001", "--comments", "2"], capsys)
    assert "CHG0030001 (change_request)" in out and "approval: Requested" in out and "note by bob: checked" in out
    with pytest.raises(SystemExit):
        run(cli, ["ticket", "XYZ0000001"], capsys)
    assert len(f.calls) == 3


def test_generic_query_is_read_only_get_with_flattened_rows(cli, capsys):
    f = Fake(cli, {("GET", "/api/now/table/problem"): {"result": [row("PRB1000001")]}})
    out = run(cli, ["query", "problem", "--query", "active=true", "--fields", "number,state"], capsys)
    assert '"number": "PRB1000001"' in out and all(c[0] == "GET" for c in f.calls)


# ---------- CLI writes (draft-first) ----------
def test_comment_without_confirm_is_a_draft_and_sends_nothing(cli, capsys):
    f = Fake(cli, {("GET", "/api/now/table/incident"): {"result": [row("INC0010001")]}})
    out = run(cli, ["comment", "INC0010001", "hello", "--work-note"], capsys)
    assert out.startswith("DRAFT (nothing sent") and '"work_notes": "hello"' in out
    assert all(c[0] == "GET" for c in f.calls)


def test_comment_with_confirm_patches_the_right_journal_field(cli, capsys):
    f = Fake(cli, {("GET", "/api/now/table/incident"): {"result": [row("INC0010001")]}, ("PATCH", f"/api/now/table/incident/{SYS}"): {"result": {}}})
    out = run(cli, ["comment", "INC0010001", "hi", "--confirm"], capsys)
    assert out.startswith("SENDING") and f.calls[-1][3] == {"comments": "hi"}


def test_transition_resolves_state_label_through_choices(cli, capsys):
    f = Fake(cli, {("GET", "/api/now/table/incident"): {"result": [row("INC0010001")]},
                   ("GET", "/api/now/table/sys_choice"): {"result": [{"value": cell("6"), "label": cell("Resolved")}]},
                   ("PATCH", f"/api/now/table/incident/{SYS}"): {"result": row("INC0010001", "Resolved")}})
    assert "DRAFT" in run(cli, ["transition", "INC0010001", "Resolved"], capsys)
    assert not any(c[0] == "PATCH" for c in f.calls)
    out = run(cli, ["transition", "INC0010001", "Resolved", "--note", "fixed", "--confirm"], capsys)
    assert f.calls[-1][3] == {"state": "6", "work_notes": "fixed"} and "state is now Resolved" in out


def test_create_change_validates_dates_and_needs_confirm(cli, capsys):
    f = Fake(cli, {("POST", "/api/now/table/change_request"): {"result": row("CHG0030009")}})
    out = run(cli, ["create-change", "--short", "Patch", "--type", "normal", "--start", "2026-03-01 10:00:00"], capsys)
    assert "DRAFT" in out and f.calls == []
    with pytest.raises(SystemExit):
        run(cli, ["create-change", "--short", "x", "--start", "tomorrow"], capsys)
    assert "created CHG0030009" in run(cli, ["create-change", "--short", "Patch", "--confirm"], capsys)


# ---------- fetch / test ----------
def test_fetch_builds_furl_card_json(cli, capsys):
    Fake(cli, {("GET", f"/api/now/table/incident/{SYS}"): {"result": row("INC0010001")}})
    out = json.loads(run(cli, ["fetch", "--kind", "ticket", "--ident", json.dumps({"table": "incident", "sys_id": SYS})], capsys))
    assert out["title"] == "Printer broken" and out["status"] == "In Progress" and out["url"] == f"{BASE}/incident.do?sys_id={SYS}"
    assert out["fields"]["number"] == "INC0010001"


def test_test_command_reports_steps_and_table_access_is_optional(cli, capsys):
    Fake(cli, {("GET", "/api/now/table/sys_user"): {"result": [{"name": cell("Svc User")}]},
               ("GET", "/api/now/table/incident"): {"result": [{"sys_id": cell(SYS)}]},
               ("GET", "/api/now/table/change_request"): cli.ApiError("HTTP 403 GET x: denied", 403)})
    out = json.loads(run(cli, ["test"], capsys))
    assert out["ok"] and [s["name"] for s in out["steps"]] == ["config", "auth", "incident", "change"]
    assert "change not readable" in out["message"] and "unit-test-token" not in json.dumps(out)


def test_test_command_reports_auth_failure_with_hint(cli, capsys):
    Fake(cli, {("GET", "/api/now/table/sys_user"): cli.ApiError("HTTP 401 GET x: bad", 401)})
    out = json.loads(run(cli, ["test"], capsys))
    assert not out["ok"] and "scope/expiry" in out["message"] + out["steps"][-1]["message"]


# ---------- watcher ----------
@pytest.fixture
def watch(cli, monkeypatch, tmp_path):
    monkeypatch.setenv("WATCH_STATE_DIR", str(tmp_path))
    w = _load("watch/snow_watch.py", "snow_watch_t")
    w.load_cli = lambda: cli

    def go(argv):
        monkeypatch.setattr(sys, "argv", ["snow_watch.py"] + argv)
        with pytest.raises(SystemExit) as e:
            w.main()
        return e.value.code
    return go


def test_mine_watch_baseline_silent_then_reports_updates_once(cli, watch, capsys):
    data = {"rows": []}
    Fake(cli, {("GET", f"/api/now/table/{t}"): (lambda q, d: {"result": data["rows"]}) for t in
               ["incident", "change_request", "sc_req_item", "sc_task", "change_task"]})
    assert watch(["mine"]) == 0
    data["rows"] = [row("INC0010001", sys_updated_on=cell("2999-01-01 00:00:00"), sys_updated_by=cell("bob"))]
    assert watch(["mine", "--tables", "incident"]) == 0  # different args = own baseline
    assert watch(["mine", "--tables", "incident"]) == 200
    out = capsys.readouterr().out
    assert "UPDATED INC0010001 [In Progress]" in out and "by bob" in out and f"{BASE}/incident.do?sys_id={SYS}" in out
    data["rows"] = []
    assert watch(["mine", "--tables", "incident"]) == 0


def test_group_watch_requires_group_and_reports_new_tickets(cli, watch, capsys):
    assert watch(["group"]) == 1
    data = {"rows": [row("INC0010005", sys_created_on=cell("2999-01-01 00:00:00"))]}
    f = Fake(cli, {("GET", "/api/now/table/incident"): lambda q, d: {"result": data["rows"]}})
    assert watch(["group", "--group", "Service Desk", "--tables", "incident"]) == 0
    assert watch(["group", "--group", "Service Desk", "--tables", "incident"]) == 200
    assert "NEW INC0010005" in capsys.readouterr().out
    assert "assignment_group.name=Service Desk" in f.calls[-1][2]["sysparm_query"]


def test_change_watch_reports_state_approval_new_and_left(cli, watch, capsys):
    data = {"rows": [row("CHG1", "Scheduled"), row("CHG2", "Scheduled")]}
    Fake(cli, {("GET", "/api/now/table/change_request"): lambda q, d: {"result": data["rows"]}})
    assert watch(["change", "--group", "Network"]) == 0
    assert watch(["change", "--group", "Network"]) == 0
    data["rows"] = [row("CHG1", "Implement", approval=cell("approved", "Approved")), row("CHG3", "New")]
    assert watch(["change", "--group", "Network"]) == 200
    out = capsys.readouterr().out
    assert "CHANGED CHG1" in out and "Scheduled→Implement" in out and "Requested→Approved" in out
    assert "NEW CHG3" in out and "LEFT   CHG2" in out


def test_watch_transient_error_is_silent_but_auth_error_fails(cli, watch, capsys):
    Fake(cli, {("GET", "/api/now/table/change_request"): cli.ApiError("HTTP 503 GET x: down", 503)})
    assert watch(["change"]) == 0
    Fake(cli, {("GET", "/api/now/table/change_request"): cli.ApiError("HTTP 401 GET x: token=sekret", 401)})
    assert watch(["change"]) == 1
    assert "sekret" not in capsys.readouterr().err


# ---------- manifest / skill / config ----------
def test_skill_renders_generic_with_absolute_paths_and_short_description(tmp_path):
    reg = build_registry({"snow-main": INST})
    assert ensure_skills(reg, tmp_path) == ["servicenow-snow-main"]
    text = (tmp_path / "servicenow-snow-main" / "SKILL.md").read_text()
    desc = re.search(r'^description: "(.*)"$', text, re.M).group(1)
    assert "${{" not in text and BASE in text and "--instance snow-main" in text and "unit-test-token" not in text
    assert "cli.py" not in desc and "--instance" not in desc and len(desc) < 200
    assert str(ROOT / "cli" / "cli.py") in text and str(ROOT / "watch") in text


@pytest.mark.parametrize("opts", [{"username": "a b"}, {"username": 'a"x'}, {"title": "<x>"}])
def test_invalid_options_are_rejected(opts):
    assert build_registry({"s": {**INST, "options": opts}}).get("s").errors


def test_furl_pattern_matches_classic_and_nav_links_only_for_the_instance():
    reg = build_registry({"s": INST})
    pat = re.compile(substitute_base_url(reg.manifests["servicenow"].kinds["ticket"].url_match, BASE))
    for u in (f"{BASE}/incident.do?sys_id={SYS}", f"{BASE}/nav_to.do?uri=change_request.do?sys_id={SYS}",
              f"{BASE}/nav_to.do?uri=%2Fincident.do%3Fsys_id%3D{SYS}", f"{BASE}/incident.do?sysparm_view=x&sys_id={SYS}"):
        assert pat.match(u), u
    m = pat.match(f"{BASE}/nav_to.do?uri=change_request.do?sys_id={SYS}")
    assert (m.group("table"), m.group("sys_id")) == ("change_request", SYS)
    assert not pat.match(f"https://other.example.net/incident.do?sys_id={SYS}")




def _cfg(tmp_path, monkeypatch, doc):
    import yaml
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({"integrations": doc}))
    monkeypatch.setenv("DOP_AGENT_CONFIG", str(p))


def test_configure_resolves_env_ref_token_and_never_falls_back_to_disabled_instance(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "cli"))
    import snowapi
    import snowconfig
    monkeypatch.setenv("SNOW_TEST_TOKEN", "tok-from-env")
    _cfg(tmp_path, monkeypatch, {"a": {**INST, "enabled": True}, "b": {**INST, "base_url": "https://b.example.net", "enabled": False},
                                 "c": {**INST, "auth": {"type": "bearer", "token": ""}}})
    snowapi.configure("a")
    assert snowapi.CONF["base_url"] == BASE and snowapi.CONF["username"] == "svc.user" and "tok-from-env" not in json.dumps(snowapi.CONF)
    with pytest.raises(snowapi.ApiError) as e:
        snowapi.configure("b")
    assert "not found/enabled" in str(e.value) and "tok-from-env" not in str(e.value)
    with pytest.raises(snowapi.ApiError):
        snowapi.configure("c")
    assert snowconfig.InstanceError


def test_create_incident_resolves_group_and_caller_to_sys_ids(cli, capsys):
    """Reference fields must be sent as sys_ids: names are looked up first (and ambiguity/absence is an error)."""
    gid, uid = "b" * 32, "c" * 32
    f = Fake(cli, {("GET", "/api/now/table/sys_user_group"): {"result": [{"sys_id": gid}]},
                   ("GET", "/api/now/table/sys_user"): {"result": [{"sys_id": uid}]}})
    out = run(cli, ["create-incident", "--short", "x", "--group", "Service Desk", "--caller", "jdoe"], capsys)
    assert gid in out and uid in out and all(c[0] == "GET" for c in f.calls)
    Fake(cli, {("GET", "/api/now/table/sys_user_group"): {"result": []}})
    with pytest.raises(SystemExit):
        run(cli, ["create-incident", "--short", "x", "--group", "Nope"], capsys)


def test_watch_timestamp_baseline_uses_raw_utc_not_display_value(cli, watch, capsys):
    """Display values may be in the user's time zone; the stored cursor must be the raw UTC value."""
    data = {"rows": []}
    Fake(cli, {("GET", "/api/now/table/incident"): lambda q, d: {"result": data["rows"]}})
    assert watch(["group", "--group", "G", "--tables", "incident"]) == 0
    data["rows"] = [row("INC1", sys_created_on={"value": "2999-01-01 10:00:00", "display_value": "01/01/2999 11:00:00"})]
    assert watch(["group", "--group", "G", "--tables", "incident"]) == 200
    state = next(Path(__import__("os").environ["WATCH_STATE_DIR"]).glob("snow-group-*.json"))
    assert json.loads(state.read_text())["ts"] == "2999-01-01 10:00:00"
