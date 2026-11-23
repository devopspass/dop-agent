"""Power BI package driver (drivers/powerbi): manifest/skill wiring, CLI behaviour and refresh watcher.

Feature: read Power BI (workspaces, reports, datasets, refresh history, DAX, owners) through a CDP browser token, plus
token-free `script` watchers for dataset refresh failed/completed. Everything runs against a mocked `api.request`;
no credentials or network are used. Guarantees: skill renders per instance without leftover templates and with a short
generic description; recipes pin POWERBI_* env; links resolve to the instance; refresh is a dry run unless --yes;
DAX output is row/cell bounded; GUIDs are validated (no path injection); tokens are redacted; the watcher baselines on
the first run, reports each finished refresh once, ignores running ones, and treats auth failures as errors.
"""
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

from integrations.registry import build_registry
from integrations.skills import ensure_skills
import watch_scripts

DRV = Path(__file__).resolve().parents[1] / "drivers" / "powerbi"


def _load_cli():
    """Import the driver's api/cli under private names: other drivers ship their own top-level `api` module."""
    saved = {k: sys.modules.pop(k, None) for k in ("api", "cli")}
    sys.path.insert(0, str(DRV / "cli"))
    try:
        import api as a
        import cli as c
    finally:
        sys.path.remove(str(DRV / "cli"))
        for k in ("api", "cli"):
            sys.modules.pop(k, None)
            if saved[k] is not None:
                sys.modules[k] = saved[k]
    sys.modules["powerbi_api"], sys.modules["powerbi_cli"] = a, c
    return a, c


api, cli = _load_cli()

spec = importlib.util.spec_from_file_location("powerbi_watch", DRV / "watch" / "powerbi_watch.py")
pw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pw)

WS = "11111111-1111-1111-1111-111111111111"
DS = "22222222-2222-2222-2222-222222222222"


def _cfg():
    return {"bi": {"driver": "powerbi", "enabled": True, "auth": {"type": "cdp_edge"},
                   "options": {"cdp_url": "http://localhost:9222", "account": "me@x.com", "tenant_id": "33333333-3333-3333-3333-333333333333"}}}


def test_driver_renders_short_generic_skill_pins_env_and_matches_links(tmp_path):
    reg = build_registry(_cfg())
    assert ensure_skills(reg, tmp_path) == ["powerbi-bi"]
    text = (tmp_path / "powerbi-bi" / "SKILL.md").read_text()
    desc = [ln for ln in text.splitlines() if ln.startswith("description:")][0]
    assert "cli.py" not in desc and "--instance" not in desc and "${{" not in text.replace("$${{", "")
    meta = watch_scripts.apply_watcher({"script": "powerbi_watch.py", "watcher": "powerbi-refresh-failed"}, reg)
    assert meta["integration"] == "bi" and meta["args"][0] == "refresh-failed"
    assert watch_scripts.instance_env(meta, reg)["POWERBI_ACCOUNT"] == "me@x.com"
    for kind in ("reports", "datasets", "dashboards"):
        assert reg.resolve_url(f"https://app.powerbi.com/groups/{WS}/{kind}/{DS}/ReportSection")[0].name == "bi"


def test_option_with_shell_metacharacters_is_rejected():
    for key, val in (("account", "a b@x.com"), ("title", 'A"b'), ("cdp_url", "http://h `id`"), ("tenant_id", "nope")):
        bad = _cfg()
        bad["bi"]["options"][key] = val
        assert not build_registry(bad).integrations["bi"].valid, key


def test_guid_validation_blocks_path_injection_and_redaction_hides_tokens():
    with pytest.raises(api.PowerBIError):
        api.guid("../../admin/groups")
    assert api.ws_prefix("me") == "" and api.ws_prefix(WS) == f"/groups/{WS}"
    assert "eyJabc.def.ghi123456" not in api.redact("Authorization: Bearer eyJabc.def.ghi123456")


@pytest.fixture
def calls(monkeypatch):
    log = []
    monkeypatch.setattr(api, "request", lambda path, method="get", body=None, **k: log.append((method, path, body)) or {"value": []})
    return log


def run_cli(argv, capsys):
    args = cli.build().parse_args(argv)
    args.fn(args)
    return capsys.readouterr()


def test_refresh_is_dry_run_without_yes_and_posts_only_with_yes(calls, capsys):
    out = run_cli(["refresh", DS, "--workspace", WS], capsys).out
    assert "DRY RUN" in out and not calls
    run_cli(["refresh", DS, "--workspace", WS, "--yes"], capsys)
    assert calls == [("post", f"/groups/{WS}/datasets/{DS}/refreshes", {"notifyOption": "NoNotification"})]


def test_dax_output_is_bounded_and_column_names_are_compact(monkeypatch, capsys):
    rows = [{"Sales[Region]": "R" * 200, "[Total]": i} for i in range(80)]
    monkeypatch.setattr(api, "request", lambda *a, **k: {"results": [{"tables": [{"rows": rows}]}]})
    r = run_cli(["dax", DS, "--query", "EVALUATE Sales", "--max-rows", "5", "--max-cell", "10"], capsys)
    lines = r.out.strip().splitlines()
    assert lines[0] == "Region\tTotal" and len(lines) == 6 and lines[1].startswith("R" * 10 + "…")
    assert "75 more rows" in r.err


def test_dax_rejects_empty_and_oversized_queries(capsys):
    for argv in (["dax", DS], ["dax", DS, "--query", "x" * 5000]):
        with pytest.raises(SystemExit):
            run_cli(argv, capsys)


def test_refresh_history_shows_status_duration_and_error_code(monkeypatch, capsys):
    row = {"status": "Failed", "refreshType": "Scheduled", "startTime": "2026-11-20T10:00:00Z", "endTime": "2026-11-20T10:01:30Z",
           "serviceExceptionJson": json.dumps({"errorCode": "ModelRefreshFailed", "errorDescription": "boom"})}
    monkeypatch.setattr(api, "request", lambda *a, **k: {"value": [row]})
    out = run_cli(["refresh-history", DS], capsys).out
    assert "Failed" in out and "90s" in out and "ModelRefreshFailed boom" in out


def test_access_dataset_prints_owner_and_users(monkeypatch, capsys):
    def fake(path, *a, **k):
        if path.endswith("/users"):
            return {"value": [{"displayName": "Al", "emailAddress": "al@x.com", "principalType": "User", "datasetUserAccessRight": "Read"}]}
        return {"configuredBy": "owner@x.com"}
    monkeypatch.setattr(api, "request", fake)
    out = run_cli(["access", "dataset", DS, "--workspace", WS], capsys).out
    assert "owner@x.com" in out and "al@x.com" in out and "Read" in out


def test_fetch_dataset_card_has_last_refresh_status(monkeypatch):
    def fake(path, *a, **k):
        if "refreshes" in path:
            return {"value": [{"status": "Completed", "endTime": "t"}]}
        return {"name": "Sales", "configuredBy": "o@x.com"}
    monkeypatch.setattr(api, "request", fake)
    card = cli._card("dataset", {"group": WS, "id": DS})
    assert card["title"] == "Sales" and card["status"] == "Completed" and card["fields"]["owner"] == "o@x.com"


def test_test_command_reports_failing_step_as_data_when_cdp_is_down(monkeypatch, capsys):
    auth = api._auth()
    monkeypatch.setattr(auth, "_cdp_json", lambda p: (_ for _ in ()).throw(RuntimeError("CDP endpoint down")))
    cli.cmd_test(None)
    res = json.loads(capsys.readouterr().out)
    assert res["ok"] is False and any(s["step"] == "cdp" and not s["ok"] for s in res["steps"])


def run(monkeypatch, tmp_path, runs, mode="refresh-failed", extra=()):
    monkeypatch.setenv("WATCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(pw, "pbi_get", lambda path, account=None: {"value": runs[0]})
    return pw.main([mode, "--dataset", f"{WS}:{DS}", *extra])


def r(i, status, **kw):
    return {"requestId": f"r{i}", "status": status, "refreshType": "Scheduled", "endTime": f"2026-11-20T1{i}:00:00Z", **kw}


def test_watcher_baselines_then_reports_each_new_failure_once(monkeypatch, tmp_path, capsys):
    runs = [[r(1, "Failed")]]
    assert run(monkeypatch, tmp_path, runs) == 0 and capsys.readouterr().out == ""
    runs[0] = [r(2, "Failed", serviceExceptionJson=json.dumps({"errorCode": "E1", "errorDescription": "bad"})), r(1, "Failed")]
    assert run(monkeypatch, tmp_path, runs) == 200
    out = capsys.readouterr().out
    assert "E1 bad" in out and out.count("refresh Failed") == 1
    assert run(monkeypatch, tmp_path, runs) == 0


def test_watcher_running_refresh_fires_only_after_it_finishes(monkeypatch, tmp_path, capsys):
    runs = [[]]
    run(monkeypatch, tmp_path, runs, "refresh-completed")
    runs[0] = [r(1, "Unknown")]
    assert run(monkeypatch, tmp_path, runs, "refresh-completed") == 0
    runs[0] = [r(1, "Completed")]
    assert run(monkeypatch, tmp_path, runs, "refresh-completed") == 200
    assert "Completed" in capsys.readouterr().out


def test_watcher_auth_failure_exits_1_and_rate_limit_is_silent(monkeypatch, tmp_path):
    monkeypatch.setenv("WATCH_STATE_DIR", str(tmp_path))

    def boom(exc):
        def f(path, account=None):
            raise exc
        return f
    monkeypatch.setattr(pw, "pbi_get", boom(pw.AuthError("signed out")))
    assert pw.main(["refresh-failed", "--dataset", f"{WS}:{DS}"]) == 1
    monkeypatch.setattr(pw, "pbi_get", boom(pw.Transient("429")))
    assert pw.main(["refresh-failed", "--dataset", f"{WS}:{DS}"]) == 0


def test_watcher_rejects_non_guid_dataset_spec(monkeypatch, tmp_path):
    monkeypatch.setenv("WATCH_STATE_DIR", str(tmp_path))
    assert pw.main(["refresh-failed", "--dataset", "../x:y"]) == 1
