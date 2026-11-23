"""Wiz package driver: manifest/skill rendering, instance pinning, CLI query logic, watcher script.

Feature: the `wiz` driver (CLI + watcher + skill template in drivers/wiz/). One enabled instance per Wiz tenant gets a rendered skill `wiz-<instance>`; watch recipes
pin the instance's env (WIZ_DIR, scope tags, ...). Guarantees tested here: generic scope (tags come from options, no
tenant-specific names), option patterns reject injection, findings filters/dedup/truncation, snapshot diff, the watcher
records a silent baseline then reports only new findings once, count changes, auth failure exits 1 while network
errors are a silent skip, and rescan is a dry run unless --yes.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import urllib.error
from pathlib import Path

import pytest

import watch_scripts
from drivers.base import ManifestError
from integrations.registry import build_registry
from integrations.skills import ensure_skills

DRV = Path(__file__).resolve().parents[1] / "drivers" / "wiz"
sys.path.insert(0, str(DRV / "cli"))
import wiz_api as api  # noqa: E402
import wiz_auth as auth  # noqa: E402
import wiz_furl as furl  # noqa: E402
import wiz_rescan as rescan  # noqa: E402

spec = importlib.util.spec_from_file_location("wiz_watch", DRV / "watch" / "wiz_watch.py")
ww = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ww)


def _cfg(**opts):
    o = {"wiz_dir": "~/.wiz-a", "scope_tags": "application=shop", "title": "Tenant A", **opts}
    return {"wiz-a": {"driver": "wiz", "enabled": True, "auth": {"type": "wizcli"}, "options": o}}


def vm(i, host="h1", sev="HIGH", pkg="openssl", cve="CVE-1", **extra):
    return {"id": i, "name": cve, "severity": sev, "detailedName": pkg, "version": "1", "fixedVersion": "2",
            "portalUrl": f"https://w/{i}", "postureIssuesV2": [], "locationPath": "/opt/app/x.jar",
            "vulnerableAsset": {"__typename": "VulnerableAssetVirtualMachine", "id": f"a-{host}", "name": host,
                                "providerUniqueId": f"arn/{host}"}, **extra}


@pytest.fixture(autouse=True)
def _reset(tmp_path, monkeypatch):
    monkeypatch.setenv("DOP_AGENT_CONFIG", str(tmp_path / "none.yaml"))
    (tmp_path / "none.yaml").write_text("integrations: {}\n")
    monkeypatch.delenv("WIZ_INSTANCE", raising=False)
    api.configure(None, "environment")
    auth.configure(None, None)
    yield
    api.configure(None, "environment")


@pytest.fixture
def gql(monkeypatch):
    """Fake GraphQL backend: serves `nodes` (one page) and records the variables sent."""
    s = {"nodes": [], "calls": [], "total": 0, "hasNext": False}

    def fake(query, variables, **kw):
        s["calls"].append(variables)
        if "vulnerabilityFindings" not in query:
            return {"data": {}}
        first = variables.get("first")
        nodes = s["nodes"][:first] if first == 1 else s["nodes"]
        return {"data": {"vulnerabilityFindings": {"totalCount": s["total"], "nodes": nodes,
                                                   "pageInfo": {"hasNextPage": s["hasNext"], "endCursor": "c"}}}}
    monkeypatch.setattr(auth, "graphql", fake)
    return s


def test_driver_renders_generic_skill_and_pins_instance_env(tmp_path):
    """Skill wiz-<instance> carries the instance options; recipes pin WIZ_* env of that instance only."""
    reg = build_registry(_cfg())
    assert ensure_skills(reg, tmp_path) == ["wiz-wiz-a"]
    text = (tmp_path / "wiz-wiz-a" / "SKILL.md").read_text()
    assert "Tenant A" in text and "application=shop" in text and "wiz-new-findings" in text
    assert "${{" not in text.replace("$${{", "")
    meta = watch_scripts.apply_watcher({"script": "wiz_watch.py", "watcher": "wiz-counts"}, reg)
    assert meta["integration"] == "wiz-a" and meta["args"][0] == "counts"
    env = watch_scripts.instance_env(meta, reg)
    assert env["WIZ_DIR"] == "~/.wiz-a" and env["WIZ_SCOPE_TAGS"] == "application=shop" and env["WIZ_ENV_TAG_KEY"] == "environment"


@pytest.mark.parametrize("opt,val", [("wiz_dir", "x; rm -rf /"), ("scope_tags", "a=b c"), ("scope_tags", "novalue"),
                                     ("data_center", "EU 1"), ("cdp_url", "file:///etc"), ("env_tag_key", "a b")])
def test_bad_option_values_are_rejected_so_skills_and_argv_stay_injection_safe(opt, val):
    reg = build_registry(_cfg(**{opt: val}))
    assert not reg.integrations["wiz-a"].active or reg.integrations["wiz-a"].errors




def test_link_resolves_only_for_single_instance_and_path_style_urls():
    reg = build_registry(_cfg())
    uid = "0b6c1c3e-1111-4222-8333-444455556666"
    assert reg.resolve_url(f"https://app.wiz.io/issues/{uid}")[1] == "issue"
    assert reg.resolve_url(f"https://app.wiz.io/findings/vulnerabilities/{uid}")[1] == "finding"
    two = {**_cfg(), "wiz-b": _cfg(wiz_dir="~/.wiz-b")["wiz-a"]}
    assert reg.resolve_url("https://app.wiz.io/issues#~(issue~'x)") is None
    assert build_registry(two).resolve_url(f"https://app.wiz.io/issues/{uid}") is None


def test_parse_tags_and_filter_combine_scope_env_and_extra_tags_without_null_keys():
    assert api.parse_tags("none") == [] and api.parse_tags("a=b,c=d") == [("a", "b"), ("c", "d")]
    with pytest.raises(ValueError):
        api.parse_tags("oops")
    api.configure("application=shop", "stage")
    f = api.build_filter("dev", [("team", "x")], None, "OPEN")
    assert f["assetTags"]["containsAll"] == [{"key": "application", "value": "shop"}, {"key": "stage", "value": "dev"},
                                             {"key": "team", "value": "x"}]
    assert "severity" not in f and f["status"] == ["OPEN"]
    assert api.build_filter(None, (), ["HIGH"], None) == {
        "assetTags": {"containsAll": [{"key": "application", "value": "shop"}]}, "severity": ["HIGH"]}
    api.configure(None, "stage")
    assert api.build_filter(None, (), ["HIGH"], None) == {"severity": ["HIGH"]}


def test_findings_are_deduped_host_filtered_and_truncation_is_reported(gql, capsys):
    gql["nodes"] = [vm("1"), vm("2", cve="CVE-2"), vm("3", host="other"), {**vm("4"), "vulnerableAsset": {"__typename": "Other"}}]
    gql["nodes"].append(vm("1"))
    rows = api.find_rows(["dev"], None, ["HIGH"])
    assert sorted(r["id"] for r in rows) == ["1", "2", "3"]
    assert [r["host"] for r in api.find_rows(["dev"], "oth", ["HIGH"])] == ["other"]
    gql["hasNext"] = True
    _, _, _, truncated = api.fetch_findings("dev", max_pages=2)
    assert truncated and "TRUNCATED" in capsys.readouterr().err


def test_snapshot_diff_reports_totals_and_per_asset_changes():
    b = {"envs": {"dev": {"total_count": 5, "by_severity": {"HIGH": 5}, "assets": {"h1": {"HIGH": 5}}}}}
    a = {"envs": {"dev": {"total_count": 3, "by_severity": {"HIGH": 2, "CRITICAL": 1}, "assets": {"h2": {"HIGH": 3}}}}}
    out = "\n".join(api.diff_snapshots(b, a, per_asset=True))
    assert "HIGH:-3" in out and "CRITICAL:+1" in out and "dev/h2: NEW" in out and "dev/h1: REMOVED" in out


def test_watcher_baseline_is_silent_then_only_new_findings_fire_once(gql, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WATCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(auth, "refresh_token", lambda *a, **k: None)
    gql["nodes"] = [vm("1")]
    assert ww.main(["findings"]) == 0
    gql["nodes"] = [vm("1"), vm("2", host="h2", cve="CVE-9")]
    assert ww.main(["findings"]) == 200
    out = capsys.readouterr().out
    assert "CVE-9" in out and "CVE-1" not in out
    assert ww.main(["findings"]) == 0


def test_watcher_counts_fire_on_change_and_only_increase_ignores_drops(gql, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WATCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(auth, "refresh_token", lambda *a, **k: None)
    gql["total"] = 4
    assert ww.main(["counts", "--severity", "HIGH"]) == 0
    gql["total"] = 2
    assert ww.main(["counts", "--severity", "HIGH", "--only-increase"]) == 0
    gql["total"] = 7
    assert ww.main(["counts", "--severity", "HIGH", "--only-increase"]) == 200
    assert "2->7" in capsys.readouterr().out


def test_watcher_auth_error_exits_1_but_network_error_is_a_silent_skip(tmp_path, monkeypatch):
    monkeypatch.setenv("WATCH_STATE_DIR", str(tmp_path))

    def boom(*a, **k):
        raise auth.WizAuthError("not logged in")
    monkeypatch.setattr(auth, "refresh_token", boom)
    assert ww.main(["findings"]) == 1

    def net(*a, **k):
        raise urllib.error.URLError("down")
    monkeypatch.setattr(auth, "refresh_token", net)
    assert ww.main(["findings"]) == 0


def test_rescan_is_a_dry_run_unless_yes_and_fetch_rejects_unknown_kind(capsys):
    cspec = importlib.util.spec_from_file_location("wiz_cli_main", DRV / "cli" / "cli.py")
    wiz_cli = importlib.util.module_from_spec(cspec)
    cspec.loader.exec_module(wiz_cli)
    args = wiz_cli.build().parse_args(["rescan", "--asset-id", "u1"])
    wiz_cli.cmd_rescan(args)
    assert "DRY RUN" in capsys.readouterr().out
    with pytest.raises(RuntimeError):
        furl.fetch("nope", {})


def test_scan_activities_summary_marks_unfinished_and_scopes_to_scan_group(monkeypatch):
    nodes = [{"name": "meta", "status": "DONE", "groupId": "g1"}, {"name": "disk", "status": "IN_PROGRESS", "groupId": "g1"},
             {"name": "old", "status": "DONE", "groupId": "g0"}]
    monkeypatch.setattr(rescan, "_page_graphql", lambda *a, **k: {"data": {"systemActivities": {"nodes": nodes}}})
    assert rescan.scan_activities(None, "u", "g1")["done"] is False
    assert rescan.scan_activities(None, "u", "g0")["done"] is True
    monkeypatch.setattr(rescan, "_page_graphql", lambda *a, **k: {"error": "no_response"})
    assert rescan.scan_activities(None, "u")["done"] is None


def test_service_account_secrets_resolve_from_config_refs_and_reach_only_the_wizcli_env(tmp_path, monkeypatch):
    """auth.type service_account keeps `${env:X}` refs in config.yaml (gitlab-driver convention); the CLI/watcher resolve
    them by instance name and pass them only to the wizcli subprocess env - never argv, never output."""
    import subprocess
    cfg = tmp_path / "c.yaml"
    cfg.write_text("integrations:\n  w1:\n    driver: wiz\n    enabled: true\n    auth:\n      type: service_account\n"
                   "      client_id: ${env:T_WIZ_ID}\n      client_secret: ${env:T_WIZ_SECRET}\n")
    monkeypatch.setenv("DOP_AGENT_CONFIG", str(cfg))
    monkeypatch.setenv("T_WIZ_ID", "the-id")
    monkeypatch.setenv("T_WIZ_SECRET", "the-secret")
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(cmd=cmd, env=kw["env"])
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(subprocess, "run", fake_run)
    auth.configure(str(tmp_path / "wd"), None)
    auth.configure_instance("w1")
    auth.refresh_token()
    assert seen["env"]["WIZ_CLIENT_ID"] == "the-id" and seen["env"]["WIZ_CLIENT_SECRET"] == "the-secret"
    assert "the-secret" not in " ".join(seen["cmd"])
    monkeypatch.delenv("T_WIZ_SECRET")
    with pytest.raises(auth.WizAuthError) as e:
        auth.configure_instance("w1")
    assert "the-secret" not in str(e.value)
    with pytest.raises(auth.WizAuthError):
        auth.configure_instance("missing")
