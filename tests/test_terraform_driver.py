"""Terraform (HCP Terraform / TFE) package driver: manifest/skill rendering, CLI read+write safety, watcher script.

Feature: the `terraform` driver (CLI + watcher + skill template in drivers/terraform/). Guarantees tested against a
fake JSON:API backend (no network, no real token): option patterns reject injection, skill renders generically with no
leftover templates, tokens never reach output, variable values and sensitive outputs are never shown, writes
(trigger/apply/discard) are dry runs without --yes and apply is refused for a run that is not confirmable, `test`
reports per-step results, and the watcher records a silent baseline then reports finished runs / approvals / drift once.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

import watch_scripts
from integrations.registry import build_registry
from integrations.skills import ensure_skills

DRV = Path(__file__).resolve().parents[1] / "drivers" / "terraform"
sys.path.insert(0, str(DRV / "cli"))
import tfclient  # noqa: E402
import tfconfig  # noqa: E402

_spec = importlib.util.spec_from_file_location("terraform_cli", DRV / "cli" / "cli.py")
tfcli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tfcli)
_spec = importlib.util.spec_from_file_location("terraform_watch", DRV / "watch" / "terraform_watch.py")
tw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tw)

TOKEN = "tok-SECRET-123"


def _cfg(**opts):
    o = {"organization": "acme", "title": "Acme TF", **opts}
    return {"tf-a": {"driver": "terraform", "enabled": True, "base_url": "https://tfe.example.com",
                     "auth": {"type": "api_token", "token": "${env:TF_TEST_TOKEN}"}, "options": o}}


class FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code, self._b, self.text = status, body if body is not None else {}, json.dumps(body or {})

    def json(self):
        return self._b


class Backend:
    """Fake TFE JSON:API: routes by path; records calls."""

    def __init__(self):
        self.calls = []
        self.run = {"id": "run-abc12345", "type": "runs", "attributes": {
            "status": "planned", "message": "m", "has-changes": True, "created-at": "2026-01-01T00:00:00Z",
            "actions": {"is-confirmable": True, "is-discardable": True, "is-cancelable": False}},
            "relationships": {"plan": {"data": {"id": "plan-1"}}}}
        self.ws = {"id": "ws-1", "type": "workspaces", "attributes": {
            "name": "landing-zone", "assessments-enabled": True, "locked": False, "terraform-version": "1.9.0"},
            "relationships": {"current-run": {"data": {"id": "run-abc12345"}}}}
        self.assessment = {"data": {"attributes": {"drifted": False, "resources-drifted": 0}}}

    def __call__(self, method, url, headers=None, params=None, json=None, timeout=None):
        path = url.split("/api/v2/")[1]
        self.calls.append((method, path, json))
        if method == "POST":
            return FakeResp(200, {"data": {"id": "run-new00001", "attributes": {"status": "pending"}}})
        if path == "account/details":
            return FakeResp(200, {"data": {"attributes": {"username": "bot"}}})
        if path == "organizations/acme/workspaces":
            return FakeResp(200, {"data": [self.ws], "included": [self.run], "meta": {"pagination": {}}})
        if path == "organizations/acme/workspaces/landing-zone":
            return FakeResp(200, {"data": self.ws})
        if path.startswith("runs/") and path.count("/") == 1:
            return FakeResp(200, {"data": self.run, "included": [self.ws]})
        if path == "plans/plan-1":
            return FakeResp(200, {"data": {"attributes": {"resource-additions": 2, "resource-changes": 1,
                                                          "resource-destructions": 0}}})
        if path.endswith("/policy-checks"):
            return FakeResp(200, {"data": []})
        if path.endswith("/vars"):
            return FakeResp(200, {"data": [{"attributes": {"key": "AWS_SECRET", "value": "topsecret",
                                                           "category": "env", "sensitive": True}}]})
        if path.endswith("/current-state-version"):
            return FakeResp(200, {"data": {"id": "sv-1", "attributes": {"serial": 3}}})
        if path == "state-versions/sv-1/outputs":
            return FakeResp(200, {"data": [
                {"attributes": {"name": "vpc_id", "type": "string", "sensitive": False, "value": "vpc-1"}},
                {"attributes": {"name": "db_pass", "type": "string", "sensitive": True, "value": "hunter2"}}]})
        if path.endswith("/current-assessment-result"):
            return FakeResp(200, self.assessment)
        return FakeResp(404, {})


@pytest.fixture
def be(tmp_path, monkeypatch):
    monkeypatch.setenv("TF_TEST_TOKEN", TOKEN)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"integrations": _cfg()}))
    monkeypatch.setenv("DOP_AGENT_CONFIG", str(cfg))
    monkeypatch.setenv("WATCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("TERRAFORM_INSTANCE", raising=False)
    b = Backend()
    monkeypatch.setattr(tfclient.requests, "request", b)
    return b


def cli(*args):
    return tfcli.main(["--instance", "tf-a", *args])


def test_driver_renders_generic_skill_and_pins_instance_env(tmp_path):
    """Skill terraform-<instance> renders with org/host; recipes pin TERRAFORM_INSTANCE; no leftover templates."""
    reg = build_registry(_cfg())
    assert ensure_skills(reg, tmp_path) == ["terraform-tf-a"]
    text = (tmp_path / "terraform-tf-a" / "SKILL.md").read_text()
    assert "acme" in text and "terraform-needs-approval" in text and "https://tfe.example.com" in text
    assert "${{" not in text.replace("$${{", "")
    head = text.split("---")[1]
    assert "cli.py" not in head and "--instance" not in head
    meta = watch_scripts.apply_watcher({"script": "terraform_watch.py", "watcher": "terraform-drift"}, reg)
    assert meta["integration"] == "tf-a" and meta["args"][0] == "drift"
    assert watch_scripts.instance_env(meta, reg)["TERRAFORM_INSTANCE"] == "tf-a"


@pytest.mark.parametrize("val", ["a b", "x;rm", "$(id)", "a/b", ""])
def test_bad_organization_values_are_rejected_so_skills_and_argv_stay_injection_safe(val):
    reg = build_registry(_cfg(organization=val))
    assert not reg.integrations["tf-a"].active or reg.integrations["tf-a"].errors


def test_run_and_workspace_links_resolve_by_base_url():
    reg = build_registry(_cfg())
    assert reg.resolve_url("https://tfe.example.com/app/acme/workspaces/lz/runs/run-abc123")[1] == "run"
    assert reg.resolve_url("https://tfe.example.com/app/acme/workspaces/lz")[1] == "workspace"
    assert reg.resolve_url("https://other.example.com/app/acme/workspaces/lz") is None


def test_missing_token_or_instance_fails_without_fallback(be, monkeypatch):
    monkeypatch.setenv("TF_TEST_TOKEN", "")
    with pytest.raises(tfconfig.InstanceError, match="empty"):
        tfconfig.load_instance("tf-a")
    with pytest.raises(tfconfig.InstanceError, match="not found"):
        tfconfig.load_instance("renamed")


def test_test_command_reports_steps_and_never_prints_token(be, capsys):
    assert cli("test") == 0
    out = capsys.readouterr().out
    res = json.loads(out)
    assert res["ok"] and [s["name"] for s in res["steps"]] == ["config", "auth", "organization"]
    assert TOKEN not in out


def test_test_command_flags_rejected_token(be, monkeypatch, capsys):
    monkeypatch.setattr(tfclient.requests, "request", lambda *a, **k: FakeResp(401, {"errors": []}))
    cli("test")
    res = json.loads(capsys.readouterr().out)
    assert not res["ok"] and "check auth.token" in res["message"]


def test_run_shows_plan_summary_and_vars_never_show_values(be, capsys):
    assert cli("run", "run-abc12345") == 0
    assert "+2 ~1 -0" in capsys.readouterr().out
    assert cli("vars", "--workspace", "landing-zone") == 0
    out = capsys.readouterr().out
    assert "AWS_SECRET" in out and "topsecret" not in out


def test_state_outputs_hide_sensitive_values_and_values_are_opt_in(be, capsys):
    cli("state", "--workspace", "landing-zone")
    out = capsys.readouterr().out
    assert "vpc_id" in out and "vpc-1" not in out and "hunter2" not in out
    cli("state", "--workspace", "landing-zone", "--values")
    out = capsys.readouterr().out
    assert "vpc-1" in out and "hunter2" not in out


def test_writes_are_dry_runs_without_yes_and_apply_needs_a_confirmable_run(be, capsys):
    cli("trigger", "--workspace", "landing-zone")
    cli("apply", "run-abc12345")
    assert "DRY RUN" in capsys.readouterr().out
    assert not [c for c in be.calls if c[0] == "POST"]
    cli("apply", "run-abc12345", "--yes", "--comment", "ok")
    assert [c[1] for c in be.calls if c[0] == "POST"] == ["runs/run-abc12345/actions/apply"]
    be.run["attributes"]["actions"]["is-confirmable"] = False
    assert cli("apply", "run-abc12345", "--yes") == 1


def test_invalid_ids_are_rejected_before_any_request(be):
    assert cli("run", "../x") == 1
    assert cli("runs", "--workspace", "a/b") == 1
    assert not be.calls


def test_fetch_returns_card_for_run(be, capsys):
    cli("fetch", "--kind", "run", "--ident", '{"id": "run-abc12345"}')
    card = json.loads(capsys.readouterr().out)
    assert card["fields"]["plan"] == "+2 ~1 -0" and card["url"].endswith("/runs/run-abc12345")


def test_watcher_baseline_then_reports_finished_run_once(be, capsys):
    assert tw.main(["runs"]) == 0  # baseline: run is "planned", not terminal
    be.run["attributes"]["status"] = "errored"
    assert tw.main(["runs"]) == 200
    assert "landing-zone: run errored" in capsys.readouterr().out
    assert tw.main(["runs"]) == 0


def test_watcher_status_filter_skips_other_final_states(be):
    tw.main(["runs"])
    be.run["attributes"]["status"] = "applied"
    assert tw.main(["runs", "--only-failed"]) == 0


def test_watcher_approval_fires_for_new_confirmable_run_only(be, capsys):
    assert tw.main(["approval"]) == 0  # baseline: already waiting
    be.run["id"] = "run-new99999"
    be.ws["relationships"]["current-run"]["data"]["id"] = "run-new99999"
    assert tw.main(["approval"]) == 200
    assert "needs approval" in capsys.readouterr().out
    assert tw.main(["approval"]) == 0


def test_watcher_drift_fires_on_new_drift_and_count_change(be, capsys):
    assert tw.main(["drift"]) == 0
    be.assessment = {"data": {"attributes": {"drifted": True, "resources-drifted": 2}}}
    assert tw.main(["drift"]) == 200 and "2 resource" in capsys.readouterr().out
    assert tw.main(["drift"]) == 0
    be.assessment = {"data": {"attributes": {"drifted": True, "resources-drifted": 3}}}
    assert tw.main(["drift"]) == 200


def test_watcher_auth_failure_exits_1_but_server_error_is_silent(be, monkeypatch):
    monkeypatch.setattr(tfclient.requests, "request", lambda *a, **k: FakeResp(401, {}))
    assert tw.main(["runs"]) == 1
    monkeypatch.setattr(tfclient.requests, "request", lambda *a, **k: FakeResp(503, {}))
    assert tw.main(["runs"]) == 0


def test_watcher_rejects_unsafe_filter_values(be):
    assert tw.main(["runs", "--search", "a b; rm"]) == 1


