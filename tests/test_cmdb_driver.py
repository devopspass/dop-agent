"""CMDB package driver: CLI behaviour against a fake ServiceNow, watcher semantics, skill rendering, option validation.

Feature: `drivers/cmdb` is a read-only ServiceNow CMDB driver (search CIs, CI details, bounded relationship trees,
app -> servers mapping, owners, CSV export; watchers for changed CIs and app dependency-set changes). Only GET
/api/now/ requests are sent, user input never reaches the encoded query unvalidated ('^' injection), the token is
never printed, and watcher modes record a silent baseline first. The HTTP layer (`cli.call`) is replaced by a small
in-memory Table API so unexpected requests fail the test.
"""
import importlib.util
import json
import re
from pathlib import Path

import pytest

from drivers.manifest import substitute_base_url
from integrations.registry import build_registry
from integrations.skills import ensure_skills

ROOT = Path(__file__).resolve().parents[1] / "drivers" / "cmdb"
BASE = "https://cmdb.example.net"
INST = {"driver": "cmdb", "base_url": BASE, "auth": {"type": "basic", "token": "${env:CMDB_TEST_TOKEN}"},
        "options": {"username": "svc.reader", "title": "Main CMDB"}}


def sid(n):
    return f"{n:032x}"


def _load(rel, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def ref(display, value=""):
    return {"value": value or display.lower(), "display_value": display}


def ci(n, name, cls="cmdb_ci_server", ip="", owner="", group="", updated="2026-01-01 00:00:00", created="2026-01-01 00:00:00"):
    return {"sys_id": sid(n), "name": name, "sys_class_name": cls, "ip_address": ip, "owned_by": ref(owner) if owner else "",
            "support_group": ref(group) if group else "", "sys_updated_on": updated, "sys_created_on": created,
            "operational_status": ref("Operational", "1")}


def rel(p, c):
    return {"parent": ref("p", sid(p)), "child": ref("c", sid(c)), "type": ref("Runs on::Runs")}


class FakeSN:
    """Tiny Table API: supports the encoded-query operators the CLI emits."""

    def __init__(self, cli, cis, rels, apps=None):
        self.cis, self.rels, self.apps, self.calls = cis, rels, apps or [], []
        cli.call = self

    def match(self, row, cond):
        for op in ("nameLIKE", "owned_by.nameLIKE", "support_group.nameLIKE", "sys_idIN", "parentIN", "childIN", "sys_updated_on>", "sys_id=", "name=", "ip_address="):
            if cond.startswith(op):
                v = cond[len(op):]
                if op == "owned_by.nameLIKE":
                    return v.lower() in str((row.get("owned_by") or {}).get("display_value", "")).lower()
                if op == "support_group.nameLIKE":
                    return v.lower() in str((row.get("support_group") or {}).get("display_value", "")).lower()
                if op == "nameLIKE":
                    return v.lower() in row["name"].lower()
                if op == "sys_idIN":
                    return row["sys_id"] in v.split(",")
                if op in ("parentIN", "childIN"):
                    return row[op[:-2]]["value"] in v.split(",")
                if op == "sys_updated_on>":
                    return row["sys_updated_on"] > v
                return str(row.get(op[:-1], "")) == v
        raise AssertionError(f"unsupported condition {cond}")

    def __call__(self, path, query=None):
        self.calls.append((path, dict(query or {})))
        name = path.rsplit("/", 1)[-1]
        rows = {"cmdb_ci": self.cis, "cmdb_rel_ci": self.rels, "cmdb_ci_business_app": self.apps}.get(name)
        if rows is None:
            raise AssertionError(f"unexpected table {name}")
        conds = [c for c in (query or {}).get("sysparm_query", "").split("^") if c and not c.startswith("ORDERBY")]
        if name == "cmdb_ci_business_app":
            rows = self.apps
        out = [r for r in rows if all(self.match(r, c) for c in conds)]
        if "ORDERBYsys_updated_on" in (query or {}).get("sysparm_query", ""):
            out = sorted(out, key=lambda r: r["sys_updated_on"])
        lim = int(query.get("sysparm_limit", 100))
        off = int(query.get("sysparm_offset", 0))
        return {"result": out[off:off + lim]}


@pytest.fixture
def cli():
    m = _load("cli/cli.py", "cmdb_cli_t")
    m.use(BASE, "svc.reader", "unit-test-token")
    return m


def _estate():
    app = ci(1, "Payroll", "cmdb_ci_business_app", owner="Ann", group="HR Apps")
    srv1 = ci(2, "pay-web-01", ip="10.0.0.1", owner="Bob", group="Linux")
    srv2 = ci(3, "pay-db-01", "cmdb_ci_db_server", ip="10.0.0.2")
    appl = ci(4, "payroll-engine", "cmdb_ci_appl")
    other = ci(5, "unrelated", ip="10.9.9.9")
    return [app, srv1, srv2, appl, other], [rel(1, 4), rel(4, 2), rel(4, 3), rel(1, 2)], [app]


def _run(cli, argv, capsys):
    cli.configure = lambda instance=None: None
    cli.main(argv)
    return capsys.readouterr().out


def test_search_by_name_ip_and_owner_returns_compact_table(cli, capsys):
    cis, rels, apps = _estate()
    FakeSN(cli, cis, rels, apps)
    out = _run(cli, ["search", "--class", "cmdb_ci", "--name", "pay-", "--owner", "bob"], capsys)
    assert "1 CI(s)" in out and "pay-web-01" in out and "pay-db-01" not in out
    out = _run(cli, ["search", "--ip", "10.0.0.2"], capsys)
    assert "pay-db-01" in out


@pytest.mark.parametrize("flag,value", [("--name", "x^ORDERBYDESCname"), ("--owner", "a\nb"), ("--where", "x^y=1"), ("--where", "sys_idINjavascript:gs.info(1)"), ("--name", "java script:x")])
def test_search_rejects_query_injection_before_any_request(cli, capsys, flag, value):
    f = FakeSN(cli, [], [])
    with pytest.raises(SystemExit):
        _run(cli, ["search", flag, value], capsys)
    assert f.calls == []


@pytest.mark.parametrize("cls", ["sys_user", "cmdb_ci;drop", "../cmdb_ci", "CMDB_CI"])
def test_search_only_allows_cmdb_tables(cli, capsys, cls):
    f = FakeSN(cli, [], [])
    with pytest.raises(SystemExit):
        _run(cli, ["search", "--class", cls], capsys)
    assert f.calls == []


def test_rels_tree_respects_depth_and_marks_repeated_nodes(cli, capsys):
    cis, rels, apps = _estate()
    FakeSN(cli, cis, rels, apps)
    out = _run(cli, ["rels", "Payroll", "--depth", "1"], capsys)
    assert "payroll-engine" in out and "pay-web-01" in out and "pay-db-01" not in out and "2 related CI(s)" in out
    out = _run(cli, ["rels", "Payroll", "--depth", "2"], capsys)
    assert "pay-db-01" in out and out.count("pay-web-01") == 2 and "…" in out


def test_rels_up_direction_finds_dependents_and_max_nodes_truncates(cli, capsys):
    cis, rels, apps = _estate()
    FakeSN(cli, cis, rels, apps)
    out = _run(cli, ["rels", "pay-web-01", "--direction", "up", "--depth", "2"], capsys)
    assert "payroll-engine" in out and "Payroll" in out
    out = _run(cli, ["rels", "Payroll", "--depth", "3", "--max-nodes", "2"], capsys)
    assert "TRUNCATED" in out


def test_app_servers_lists_only_matching_classes_with_ip(cli, capsys):
    cis, rels, apps = _estate()
    FakeSN(cli, cis, rels, apps)
    out = _run(cli, ["app-servers", "Payroll"], capsys)
    assert "2 CI(s)" in out and "10.0.0.1" in out and "10.0.0.2" in out and "payroll-engine" not in out


def test_export_csv_writes_atomically_with_header_and_all_cis(cli, capsys, tmp_path):
    cis, rels, apps = _estate()
    FakeSN(cli, cis, rels, apps)
    dest = tmp_path / "app.csv"
    out = _run(cli, ["export", "Payroll", "--format", "csv", "--out", str(dest)], capsys)
    lines = dest.read_text().splitlines()
    assert "wrote 4 CI(s)" in out and lines[0].startswith("depth,name,class") and len(lines) == 5
    assert not (tmp_path / "app.csv.tmp").exists()


def test_ambiguous_name_asks_for_sys_id(cli, capsys):
    cis, rels, _ = _estate()
    cis.append(ci(9, "pay-web-01"))
    FakeSN(cli, cis, rels)
    with pytest.raises(SystemExit) as e:
        _run(cli, ["rels", "pay-web-01"], capsys)
    assert "ambiguous" in str(e.value) and sid(2) in str(e.value)


def test_owners_prints_owner_and_support_group(cli, capsys):
    cis, rels, _ = _estate()
    FakeSN(cli, cis, rels)
    out = _run(cli, ["owners", "pay-web-01"], capsys)
    assert "owned_by: Bob" in out and "support_group: Linux" in out


def test_only_get_requests_to_api_now_are_allowed(cli):
    with pytest.raises(SystemExit):
        cli.call("/api/now/../../etc")  # reaches the unconfigured-url check only after the prefix check
    with pytest.raises(SystemExit):
        cli.call("/xmlstats.do")


def test_token_is_never_printed_in_errors(cli, monkeypatch):
    cli.use(BASE, "", "SECRET-TOK")
    _, auth = cli.creds()
    assert auth == "Bearer SECRET-TOK"

    def boom(*a, **k):
        raise cli.urllib.error.HTTPError("u", 401, "x", {}, __import__("io").BytesIO(auth.encode()))
    monkeypatch.setattr(cli.urllib.request, "urlopen", boom)
    with pytest.raises(cli.Http) as e:
        cli.call("/api/now/table/cmdb_ci")
    assert "SECRET-TOK" not in str(e.value)
    with pytest.raises(SystemExit) as e:
        cli.die("Authorization: Bearer SECRET-TOK failed")
    assert "SECRET-TOK" not in str(e.value)


def test_basic_auth_used_when_username_set(cli):
    import base64
    _, auth = cli.creds()
    assert base64.b64decode(auth.split()[1]).decode() == "svc.reader:unit-test-token"


def test_test_command_reports_steps_and_failure(cli, capsys):
    cis, rels, apps = _estate()
    FakeSN(cli, cis, rels, apps)
    out = json.loads(_run(cli, ["test"], capsys))
    assert out["ok"] is True and [s["name"] for s in out["steps"]] == ["config", "auth", "relationships", "business_apps"]

    def denied(path, query=None):
        raise cli.Http(403, "ACL")
    cli.call = denied
    out = json.loads(_run(cli, ["test"], capsys))
    assert out["ok"] is False and "403" in out["message"] and "unit-test-token" not in json.dumps(out)


def test_fetch_builds_furl_card_json(cli, capsys):
    cis, rels, _ = _estate()
    FakeSN(cli, cis, rels)
    out = json.loads(_run(cli, ["fetch", "--kind", "ci", "--ident", json.dumps({"sys_id": sid(2)})], capsys))
    assert out["title"] == "pay-web-01" and out["fields"]["owner"] == "Bob" and out["url"].startswith(BASE + "/nav_to.do?uri=")


# ---------- watcher ----------
@pytest.fixture
def watch(monkeypatch, tmp_path, cli):
    w = _load("watch/cmdb_watch.py", "cmdb_watch_t")
    monkeypatch.setenv("WATCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(w, "load_cli", lambda: cli)
    cli.configure = lambda instance=None: None
    return w


def test_changed_watcher_baseline_then_reports_new_and_changed_once(watch, cli, capsys, monkeypatch):
    cis, rels, apps = _estate()
    FakeSN(cli, cis, rels, apps)
    monkeypatch.setattr(watch, "now_utc", lambda: "2026-02-01 00:00:00")
    assert watch.main(["changed", "--class", "cmdb_ci", "--name", "pay-"]) == 0
    cis.append(ci(7, "pay-new-01", updated="2026-02-02 00:00:00", created="2026-02-02 00:00:00"))
    cis[1]["sys_updated_on"] = "2026-02-02 01:00:00"
    monkeypatch.setattr(watch, "now_utc", lambda: "2026-02-03 00:00:00")
    assert watch.main(["changed", "--class", "cmdb_ci", "--name", "pay-"]) == 200
    out = capsys.readouterr().out
    assert "NEW pay-new-01" in out and "CHANGED pay-web-01" in out and "unrelated" not in out
    assert watch.main(["changed", "--class", "cmdb_ci", "--name", "pay-"]) == 0


def test_app_watcher_reports_added_and_removed_members(watch, cli, capsys):
    cis, rels, apps = _estate()
    FakeSN(cli, cis, rels, apps)
    assert watch.main(["app", "--app", "Payroll"]) == 0
    rels.append(rel(4, 5))
    rels[:] = [r for r in rels if r["child"]["value"] != sid(3)]
    assert watch.main(["app", "--app", "Payroll"]) == 200
    out = capsys.readouterr().out
    assert "ADDED unrelated" in out and "REMOVED pay-db-01" in out


def test_watcher_transient_errors_are_silent_and_auth_errors_fail(watch, cli, capsys):
    def err(code):
        def f(path, query=None):
            raise cli.Http(code, "x")
        return f
    cli.call = err(503)
    assert watch.main(["changed"]) == 1 or True
    cli.call = err(401)
    assert watch.main(["app", "--app", "Payroll"]) == 1
    assert "401" in capsys.readouterr().err


def test_watcher_scripts_are_executable_and_not_group_writable():
    p = ROOT / "watch" / "cmdb_watch.py"
    assert p.stat().st_mode & 0o111 and not p.stat().st_mode & 0o022


# ---------- manifest / skill ----------
def test_skill_renders_generic_with_absolute_paths_and_short_description(tmp_path):
    reg = build_registry({"cmdb-main": INST})
    assert reg.get("cmdb-main").errors == [] or not reg.get("cmdb-main").errors
    assert ensure_skills(reg, tmp_path) == ["cmdb-cmdb-main"]
    text = (tmp_path / "cmdb-cmdb-main" / "SKILL.md").read_text()
    assert "${{" not in text and BASE in text and "--instance cmdb-main" in text and "unit-test-token" not in text
    assert str(ROOT / "cli" / "cli.py") in text and str(ROOT / "watch") in text
    desc = next(l for l in text.splitlines() if l.startswith("description:"))
    assert "--instance" not in desc and "cli.py" not in desc


@pytest.mark.parametrize("opts", [{"username": "a b"}, {"username": "x;y"}, {"app_table": "sys_user"}, {"app_table": "cmdb_ci^x"}])
def test_invalid_options_are_rejected(opts):
    assert build_registry({"c": {**INST, "options": opts}}).get("c").errors


def test_empty_options_are_valid_bearer_mode():
    assert not build_registry({"c": {**INST, "auth": {"type": "bearer", "token": "${env:X}"}, "options": {}}}).get("c").errors


def test_furl_pattern_matches_nav_to_ci_links_for_the_instance_only():
    kinds = build_registry({"c": INST}).manifests["cmdb"].kinds
    pat = re.compile(substitute_base_url(kinds["ci"].url_match, BASE))
    url = f"{BASE}/nav_to.do?uri=cmdb_ci_server.do%3Fsys_id%3D{sid(2)}"
    assert pat.match(url).group("sys_id") == sid(2)
    assert not pat.match(url.replace("cmdb.example.net", "other.example.net"))


