"""Kargo package driver: multi-instance skill, furls, token-safe `kargo` wrapper, confirm-first writes, watcher.

Feature: Kargo is a package driver whose CLI wraps the `kargo` binary (tested here with a fake binary, no network).
Guarantees: every instance gets its own skill/host (no CLI hints in the description); the token reaches the
child ONLY via KARGO_API_TOKEN (never argv, never printed) and the user's own `kargo login` config is not used;
names that could act as flags are rejected; promote/abort/approve/verify print a dry-run preview until --yes;
`test` reports per-step results; the watcher records a silent baseline, then reports each new freight /
finished promotion / health change / failed verification exactly once.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

from integrations.registry import build_registry
from integrations.skills import ensure_skills
import watch_scripts

DRV = Path(__file__).resolve().parents[1] / "drivers" / "kargo"
sys.path.insert(0, str(DRV / "cli"))
import kargoconfig as kc  # noqa: E402
import kargoview as kv  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


kw = _load("kargo_watch_mod", DRV / "watch" / "kargo_watch.py")
cli = _load("kargo_cli_mod", DRV / "cli" / "cli.py")

TOKEN = "s3cr3t-token-value"
FAKE = r'''#!/usr/bin/env python3
import json, os, sys
d = os.path.dirname(os.path.abspath(__file__))
with open(d + "/calls.log", "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "token": os.environ.get("KARGO_API_TOKEN"),
                        "addr": os.environ.get("KARGO_API_ADDRESS"), "xdg": os.environ.get("XDG_CONFIG_HOME")}) + "\n")
if os.path.exists(d + "/fail"):
    sys.stderr.write(open(d + "/fail").read()); sys.exit(1)
a = sys.argv[1:]
if a[0] == "version":
    print("Client Version: v1.12.6\nServer Version: v1.12.6")
elif a[0] == "get":
    p = d + "/" + a[1] + ".json"
    print(open(p).read() if os.path.exists(p) else '{"items": []}')
else:
    print("done " + " ".join(a))
'''


@pytest.fixture
def env(tmp_path, monkeypatch):
    fake = tmp_path / "kargo"
    fake.write_text(FAKE)
    fake.chmod(0o755)
    (tmp_path / "state").mkdir()
    cfgp = tmp_path / "config.yaml"
    cfgp.write_text(yaml.safe_dump({"integrations": {
        "one": {"driver": "kargo", "enabled": True, "base_url": "https://kargo.one.example",
                "auth": {"type": "bearer_token", "token": "${env:KARGO_TEST_TOKEN}"},
                "options": {"default_project": "shop"}},
        "off": {"driver": "kargo", "enabled": False, "base_url": "https://kargo.off.example",
                "auth": {"type": "bearer_token", "token": "x"}}}}))
    monkeypatch.setenv("DOP_AGENT_CONFIG", str(cfgp))
    monkeypatch.setenv("KARGO_TEST_TOKEN", TOKEN)
    monkeypatch.setenv("KARGO_BIN", str(fake))
    monkeypatch.setenv("WATCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("KARGO_INSTANCE", raising=False)
    return tmp_path


def put(env, kind, items):
    (env / f"{kind}.json").write_text(json.dumps({"items": items}))


def calls(env):
    return [json.loads(x) for x in (env / "calls.log").read_text().splitlines()]


def run(capsys, argv):
    rc = cli.main(argv)
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def stage(name, health="Healthy", phase="Steady", vphase="Successful", vid="v1", freight="abc"):
    return {"metadata": {"name": name}, "spec": {"requestedFreight": [{"origin": {"name": "wh"}}]},
            "status": {"phase": phase, "health": {"status": health},
                       "freightHistory": [{"items": {"Warehouse/wh": {"name": freight * 10, "alias": freight}},
                                           "verificationHistory": [{"id": vid, "phase": vphase, "message": "boom",
                                                                     "analysisRun": {"name": "ar-1", "phase": vphase}}]}]}}


def promo(name, phase, stage_="dev", ts="2026-01-01T00:00:00Z"):
    return {"metadata": {"name": name, "creationTimestamp": ts}, "spec": {"stage": stage_, "freight": "f1"},
            "status": {"phase": phase, "message": "msg"}}


def freight(name, alias="", ts="2026-01-01T00:00:00Z"):
    return {"metadata": {"name": name, "creationTimestamp": ts}, "alias": alias, "origin": {"name": "wh"},
            "images": [{"repoURL": "img", "tag": "1.0"}], "status": {"verifiedIn": {"dev": {}}}}


# ---- registry: skill, options, furls ----

def _cfg():
    def inst(host, **opts):
        return {"driver": "kargo", "enabled": True, "base_url": host,
                "auth": {"type": "bearer_token", "token": "x"}, "options": opts}
    return {"one": inst("https://kargo.one.example", default_project="shop"), "two": inst("https://kargo.two.example", title="Second")}


def test_each_instance_gets_its_own_skill_and_description_has_no_cli_hints(tmp_path):
    reg = build_registry(_cfg())
    assert not [i.errors for i in reg.integrations.values() if i.errors]
    assert ensure_skills(reg, tmp_path) == ["kargo-one", "kargo-two"]
    one = (tmp_path / "kargo-one" / "SKILL.md").read_text()
    assert "https://kargo.one.example" in one and "--instance one" in one and "kargo.two.example" not in one
    assert "${{" not in one.replace("$${{", "") and str(DRV / "cli" / "cli.py") in one
    desc = next(x for x in one.splitlines() if x.startswith("description:"))
    assert "--instance" not in desc and "cli.py" not in desc and len(desc) < 260


@pytest.mark.parametrize("path,kind,ident", [
    ("/project/shop/stage/prod", "stage", {"project": "shop", "stage": "prod"}),
    ("/project/shop/freight/a1b2c3?x=1", "freight", {"project": "shop", "freight": "a1b2c3"}),
    ("/project/shop/promotion/prod.01abc.ff00", "promotion", {"project": "shop", "promotion": "prod.01abc.ff00"})])
def test_kargo_links_route_to_the_instance_with_the_matching_host(path, kind, ident):
    reg = build_registry(_cfg())
    it, k, i = reg.resolve_url("https://kargo.two.example" + path)
    assert (it.name, k, i) == ("two", kind, ident)
    assert reg.resolve_url("https://other.example" + path) is None


def test_option_patterns_reject_injection_in_default_project():
    bad = _cfg()
    bad["two"]["options"] = {"default_project": "x; rm -rf /"}
    assert [e for i in build_registry(bad).integrations.values() for e in i.errors]


def test_watcher_recipe_pins_instance_and_disabled_instance_fails():
    reg = build_registry(_cfg())
    meta = watch_scripts.apply_watcher({"script": "kargo_watch.py", "watcher": "kargo-promotion-result",
                                        "integration": "two"}, reg)
    assert meta["args"][0] == "promotion-result"
    assert watch_scripts.instance_env({"integration": "two"}, reg)["KARGO_INSTANCE"] == "two"
    reg.integrations["two"].enabled = False
    with pytest.raises(ValueError):
        watch_scripts.instance_env({"integration": "two"}, reg)


# ---- token safety / argument safety ----

def test_token_goes_only_to_child_env_never_argv_and_user_kargo_config_is_isolated(env, capsys):
    put(env, "projects", [{"metadata": {"name": "shop"}}])
    rc, out, err = run(capsys, ["--instance", "one", "projects"])
    c = calls(env)[0]
    assert rc == 0 and TOKEN not in out + err and TOKEN not in json.dumps(c["argv"])
    assert c["token"] == TOKEN and c["addr"] == "https://kargo.one.example"
    assert c["xdg"] and "dop-kargo-" in c["xdg"] and not Path(c["xdg"]).exists()


@pytest.mark.parametrize("bad", ["a b", "x;ls", "", "a`id`"])
def test_names_that_could_act_as_flags_or_shell_are_rejected(env, capsys, bad):
    rc, out, err = run(capsys, ["--instance", "one", "stage-info", bad])
    assert rc == 1 and "invalid" in err
    assert not (env / "calls.log").exists()


@pytest.mark.parametrize("bad", ["--abort", "-p", "-x"])
def test_check_name_rejects_leading_dash(bad):
    with pytest.raises(kc.KargoError):
        kc.check_name(bad)


def test_kargo_errors_are_redacted(env, capsys):
    (env / "fail").write_text(f"unauthorized: token={TOKEN} rejected")
    rc, out, err = run(capsys, ["--instance", "one", "projects"])
    assert rc == 1 and TOKEN not in err and "unauthorized" in err


def test_missing_disabled_or_ambiguous_instance_never_falls_back(env, capsys):
    for inst in ("nope", "off"):
        rc, _, err = run(capsys, ["--instance", inst, "projects"])
        assert rc == 1 and "not found/enabled" in err
    assert not (env / "calls.log").exists()


def test_project_is_required_unless_default_project_is_configured(env, capsys, monkeypatch):
    put(env, "stages", [stage("dev")])
    rc, out, _ = run(capsys, ["--instance", "one", "stages"])
    assert rc == 0 and "--project=shop" in calls(env)[0]["argv"]
    rc, out, _ = run(capsys, ["--instance", "one", "stages", "-p", "other"])
    assert "--project=other" in calls(env)[-1]["argv"]


# ---- reads ----

def test_stages_summarize_health_freight_and_verification(env, capsys):
    put(env, "stages", [stage("prod", health="Unhealthy", vphase="Failed")])
    rc, out, _ = run(capsys, ["--instance", "one", "stages"])
    s = json.loads(out)[0]
    assert s["health"] == "Unhealthy" and s["freight"] == ["abc"] and s["verification"]["phase"] == "Failed"
    assert s["verification"]["analysis_run"] == "ar-1"


def test_promotions_are_newest_first_and_filterable_by_phase(env, capsys):
    put(env, "promotions", [promo("old", "Succeeded", ts="2026-01-01T00:00:00Z"),
                            promo("new", "Failed", ts="2026-02-01T00:00:00Z")])
    _, out, _ = run(capsys, ["--instance", "one", "promotions"])
    assert [p["name"] for p in json.loads(out)] == ["new", "old"]
    _, out, _ = run(capsys, ["--instance", "one", "promotions", "--phase", "failed"])
    assert [p["name"] for p in json.loads(out)] == ["new"]


def test_get_json_parser_accepts_list_single_and_concatenated_objects():
    assert len(kc.parse_items('{"items":[{"a":1},{"b":2}]}')) == 2
    assert len(kc.parse_items('{"a":1}\n{"b":2}')) == 2


# ---- writes are confirm-first ----

def test_promote_prints_a_dry_run_and_runs_nothing_without_yes(env, capsys):
    rc, out, _ = run(capsys, ["--instance", "one", "promote", "--alias", "wonky", "--stage", "prod"])
    d = json.loads(out)
    assert rc == 0 and d["dry_run"] is True and "--stage=prod" in d["would_run"]
    assert not (env / "calls.log").exists()
    rc, out, _ = run(capsys, ["--instance", "one", "promote", "--alias", "wonky", "--stage", "prod", "--yes"])
    assert json.loads(out)["ok"] is True
    assert calls(env)[0]["argv"][:2] == ["promote", "--project=shop"] and "--freight-alias=wonky" in calls(env)[0]["argv"]


@pytest.mark.parametrize("argv,msg", [
    (["promote", "--stage", "prod"], "exactly one of --freight"),
    (["promote", "--freight", "f", "--alias", "a", "--stage", "prod"], "exactly one of --freight"),
    (["promote", "--freight", "f"], "exactly one of --stage"),
    (["approve", "--stage", "prod"], "exactly one of --freight")])
def test_promote_and_approve_validate_their_selectors(env, capsys, argv, msg):
    rc, _, err = run(capsys, ["--instance", "one", *argv, "--yes"])
    assert rc == 1 and msg in err and not (env / "calls.log").exists()


@pytest.mark.parametrize("argv,expect", [
    (["promote-abort", "prod.01abc.ff"], ["promote", "--project=shop", "--name=prod.01abc.ff", "--abort"]),
    (["approve", "--freight", "f1", "--stage", "prod"], ["approve", "--project=shop", "--freight=f1", "--stage=prod"]),
    (["verify", "prod", "--abort"], ["verify", "stage", "--project=shop", "prod", "--abort"]),
    (["refresh", "warehouse", "wh"], ["refresh", "warehouse", "--project=shop", "wh"])])
def test_each_write_maps_to_the_expected_kargo_command_after_yes(env, capsys, argv, expect):
    rc, out, _ = run(capsys, ["--instance", "one", *argv, "--yes"])
    assert rc == 0 and calls(env)[0]["argv"] == expect


# ---- fetch / test ----

def test_fetch_builds_link_cards_for_stage_freight_promotion(env, capsys):
    put(env, "stages", [stage("prod", health="Healthy")])
    put(env, "promotions", [promo("prod.1.f", "Succeeded", "prod")])
    put(env, "freight", [freight("abcdef123456", alias="wonky")])
    for kind, ident, title, url in [
            ("stage", {"project": "shop", "stage": "prod"}, "Stage prod", "/project/shop/stage/prod"),
            ("promotion", {"project": "shop", "promotion": "prod.1.f"}, "prod.1.f", "/project/shop/promotion/prod.1.f"),
            ("freight", {"project": "shop", "freight": "wonky"}, "wonky", "/project/shop/freight/abcdef123456")]:
        rc, out, _ = run(capsys, ["--instance", "one", "fetch", "--kind", kind, "--ident", json.dumps(ident)])
        d = json.loads(out)
        assert rc == 0 and d["title"] == title and d["url"] == "https://kargo.one.example" + url


def test_test_command_reports_steps_and_never_the_token(env, capsys):
    put(env, "projects", [{"metadata": {"name": "shop"}}])
    rc, out, _ = run(capsys, ["--instance", "one", "test"])
    d = json.loads(out)
    assert d["ok"] is True and [s["step"] for s in d["steps"]] == ["config", "binary", "server", "auth"]
    assert TOKEN not in out and "v1.12.6" in out


def test_test_command_reports_auth_failure_and_missing_binary(env, capsys, monkeypatch):
    (env / "fail").write_text("unauthorized")
    d = json.loads(run(capsys, ["--instance", "one", "test"])[1])
    assert d["ok"] is False and d["steps"][-1]["step"] == "server"
    monkeypatch.setenv("KARGO_BIN", str(env / "missing"))
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setattr(kc.Path, "home", lambda: env / "nohome")
    monkeypatch.setattr(kc.shutil, "which", lambda _n: None)
    monkeypatch.setattr(kc.os.path, "isfile", lambda _p: False)
    d = json.loads(run(capsys, ["--instance", "one", "test"])[1])
    assert d["ok"] is False and d["steps"][-1]["step"] == "binary" and "not found" in d["message"]


# ---- watcher ----

def watch(env, capsys, *argv):
    rc = kw.main([*argv, "--instance", "one"])
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def test_new_freight_baseline_then_each_new_freight_once(env, capsys):
    put(env, "freight", [freight("f1")])
    assert watch(env, capsys, "new-freight")[0] == 0
    put(env, "freight", [freight("f1"), freight("f2", alias="shiny", ts="2026-02-01T00:00:00Z")])
    rc, out, _ = watch(env, capsys, "new-freight")
    assert rc == 200 and "shiny" in out and "/project/shop/freight/f2" in out and "f1" not in out
    assert watch(env, capsys, "new-freight")[0] == 0


def test_promotion_result_reports_each_finished_promotion_once_and_only_failed_filters(env, capsys):
    put(env, "promotions", [promo("p1", "Succeeded"), promo("p2", "Running")])
    assert watch(env, capsys, "promotion-result")[0] == 0
    put(env, "promotions", [promo("p1", "Succeeded"), promo("p2", "Succeeded"), promo("p3", "Failed")])
    rc, out, _ = watch(env, capsys, "promotion-result")
    assert rc == 200 and "p2" in out and "p3" in out and "p1" not in out
    assert watch(env, capsys, "promotion-result")[0] == 0
    put(env, "promotions", [promo("p3", "Failed"), promo("p4", "Succeeded"), promo("p5", "Errored")])
    rc, out, _ = watch(env, capsys, "promotion-result", "--only-failed")
    assert rc == 200 and "p5" in out and "p4" not in out


def test_stage_health_reports_changes_only(env, capsys):
    put(env, "stages", [stage("dev"), stage("prod")])
    assert watch(env, capsys, "stage-health")[0] == 0
    put(env, "stages", [stage("dev"), stage("prod", health="Unhealthy")])
    rc, out, _ = watch(env, capsys, "stage-health")
    assert rc == 200 and "prod health Healthy → Unhealthy" in out and "dev" not in out
    assert watch(env, capsys, "stage-health")[0] == 0


def test_failed_verification_reported_once_per_verification(env, capsys):
    put(env, "stages", [stage("dev", vphase="Failed", vid="old")])
    assert watch(env, capsys, "verification-failed")[0] == 0
    put(env, "stages", [stage("dev", vphase="Failed", vid="new")])
    rc, out, _ = watch(env, capsys, "verification-failed")
    assert rc == 200 and "Failed on shop/dev" in out and "ar-1" in out
    assert watch(env, capsys, "verification-failed")[0] == 0


def test_watcher_scans_all_projects_when_no_scope_and_is_silent_on_transient_errors(env, capsys, monkeypatch):
    cfg = yaml.safe_load((env / "config.yaml").read_text())
    cfg["integrations"]["one"]["options"] = {}
    (env / "config.yaml").write_text(yaml.safe_dump(cfg))
    put(env, "projects", [{"metadata": {"name": "a"}}, {"metadata": {"name": "b"}}])
    put(env, "freight", [freight("f1")])
    assert watch(env, capsys, "new-freight")[0] == 0
    assert sum(1 for c in calls(env) if c["argv"][:2] == ["get", "freight"]) == 2
    (env / "fail").write_text("dial tcp: connection refused")
    rc, out, err = watch(env, capsys, "new-freight")
    assert rc == 0 and not out and "transient" in err
    (env / "fail").write_text("unauthorized")
    rc, out, err = watch(env, capsys, "new-freight")
    assert rc == 1 and "failed" in err
