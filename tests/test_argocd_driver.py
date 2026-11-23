"""Argo CD package driver: instance config, skill rendering, injection-safe CLI, confirm-first writes, scoping,
credential-free metadata, per-step Test and the watcher script (all against a mocked `argocd` binary).

Feature: Argo CD is a package driver wrapping the user's `argocd` CLI login session (no token stored by dop-agent).
Guarantees: each instance gets its own skill with its own server; names/revisions starting with '-' are rejected
(no option injection); sync/refresh/rollback only print a preview unless `--yes`; the `project` option blocks apps
of other projects; clusters/repos output is whitelisted (no credentials); `test` reports per-step results; the
watchers record a silent baseline and then report each OutOfSync / health change / finished sync once.
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from integrations.registry import build_registry
from integrations.skills import ensure_skills
import watch_scripts

DRV = Path(__file__).resolve().parents[1] / "drivers" / "argocd"
sys.path.insert(0, str(DRV / "cli"))
import argocdclient  # noqa: E402
import argocdconfig  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cli = _load("argocd_cli_mod", DRV / "cli" / "cli.py")
aw = _load("argocd_watch_mod", DRV / "watch" / "argocd_watch.py")


def _cfg():
    def inst(host, on=True, **opts):
        return {"driver": "argocd", "enabled": on, "base_url": host, "auth": {"type": "argocd_cli"}, "options": opts}
    return {"one": inst("https://argo.one.example"), "two": inst("https://argo.two.example", title="Second", project="team-a")}


def app(name, project="team-a", health="Healthy", sync="Synced", op=None, finished="", resources=None):
    st = {"health": {"status": health}, "sync": {"status": sync, "revision": "abcdef1234567"},
          "resources": resources or [], "history": [{"id": 3, "revision": "aaaaaaaaaaaa", "deployedAt": "2026-01-01"}]}
    if op:
        st["operationState"] = {"phase": op, "finishedAt": finished, "message": "boom"}
    return {"metadata": {"name": name, "namespace": "argocd"},
            "spec": {"project": project, "destination": {"name": "prod", "namespace": "ns"}, "source": {"repoURL": "https://git/x", "targetRevision": "main"}},
            "status": st}


class FakeArgo:
    """Stands in for subprocess.run: records argv, answers by sub-command."""
    def __init__(self, apps, rc_map=None):
        self.apps, self.calls, self.rc_map = {a["metadata"]["name"]: a for a in apps}, [], rc_map or {}

    def __call__(self, argv, capture_output=True, text=True, timeout=0):
        self.calls.append(argv)
        sub = " ".join(argv[1:3])
        rc = self.rc_map.get(sub, 0)
        out = ""
        if sub == "app list":
            out = json.dumps(list(self.apps.values()))
        elif sub == "app get":
            ref = argv[3].rpartition("/")[2]
            out = json.dumps(self.apps[ref]) if ref in self.apps else ""
            rc = rc or (0 if ref in self.apps else 20)
        elif sub == "cluster list":
            out = json.dumps([{"name": "prod", "server": "https://k8s", "serverVersion": "1.29", "connectionState": {"status": "Successful"},
                               "config": {"bearerToken": "SECRET-TOKEN-XYZ"}}])
        elif sub == "repo list":
            out = json.dumps([{"repo": "https://git/x", "type": "git", "username": "u", "password": "SECRET-PW", "connectionState": {"status": "Successful"}}])
        elif sub == "app diff":
            out = "- old\n+ new"
        elif sub == "version --client":
            out = json.dumps({"client": {"Version": "v2.11.0"}})
        elif sub == "version --server" or (sub.startswith("version") and "--client" not in argv):
            out = json.dumps({"client": {"Version": "v2.11.0"}, "server": {"Version": "v2.11.1"}})
        elif sub == "account get-user-info":
            out = json.dumps({"loggedIn": True, "username": "alice"})
        return subprocess.CompletedProcess(argv, rc, out, "")

    def mutating(self):
        return [c for c in self.calls if c[1:3] in (["app", "sync"], ["app", "rollback"]) or "--refresh" in c or "--hard-refresh" in c]


def client(apps, base="https://argo.one.example", **opts):
    f = FakeArgo(apps)
    return argocdclient.ArgoClient(base, opts, runner=f), f


def test_each_instance_gets_its_own_skill_with_its_own_server_and_no_leftover_templates(tmp_path):
    reg = build_registry(_cfg())
    assert ensure_skills(reg, tmp_path) == ["argocd-one", "argocd-two"]
    one = (tmp_path / "argocd-one" / "SKILL.md").read_text()
    assert "https://argo.one.example" in one and "--instance one" in one and "argo.two.example" not in one
    assert "${{" not in one.replace("$${{", "")
    head = one.split("---")[1]
    assert "cli.py" not in head and "--instance" not in head


def test_links_route_to_the_instance_with_the_matching_server_and_parse_both_app_url_shapes():
    reg = build_registry(_cfg())
    inst, kind, ident = reg.resolve_url("https://argo.two.example/applications/argocd/my-app?view=tree")
    assert inst.name == "two" and kind == "application" and ident == {"ns": "argocd", "name": "my-app"}
    assert reg.resolve_url("https://argo.two.example/applications/old-app")[2]["name"] == "old-app"
    assert reg.resolve_url("https://argo.two.example/settings/projects/team-a")[1] == "project"
    assert reg.resolve_url("https://other.example/applications/x") is None


def test_watcher_recipe_pins_instance_and_exports_it_to_the_script():
    reg = build_registry(_cfg())
    meta = watch_scripts.apply_watcher({"script": "argocd_watch.py", "watcher": "argocd-health", "integration": "two"}, reg)
    assert meta["args"][0] == "health"
    assert watch_scripts.instance_env(meta, reg)["ARGOCD_INSTANCE"] == "two"


@pytest.fixture
def conf(tmp_path, monkeypatch):
    p = tmp_path / "config.yaml"
    p.write_text("integrations:\n"
                 "  a: {driver: argocd, base_url: 'https://a.example/', auth: {type: argocd_cli}, options: {project: team-a}}\n"
                 "  b: {driver: argocd, base_url: 'https://b.example', auth: {type: argocd_cli}}\n"
                 "  off: {driver: argocd, enabled: false, base_url: 'https://c.example', auth: {type: argocd_cli}}\n")
    monkeypatch.setenv("DOP_AGENT_CONFIG", str(p))
    monkeypatch.delenv("ARGOCD_INSTANCE", raising=False)
    return p


def test_cli_asks_for_instance_when_several_are_enabled_and_never_falls_back_to_disabled_one(conf):
    assert argocdconfig.load_instance("a") == ("a", "https://a.example", {"project": "team-a"})
    with pytest.raises(argocdconfig.InstanceError, match="a, b"):
        argocdconfig.load_instance(None)
    with pytest.raises(argocdconfig.InstanceError, match="not found/enabled"):
        argocdconfig.load_instance("off")


@pytest.mark.parametrize("bad", ["--insecure", "-o", "a b", "a;rm", "$(x)", "", "a\nb", "a\n"])
def test_names_that_could_be_parsed_as_options_or_shell_are_rejected(bad):
    with pytest.raises(argocdclient.ArgoError):
        argocdclient.valid_name(bad)
    with pytest.raises(argocdclient.ArgoError):
        cli.split_ref(bad)


def test_trailing_newline_is_rejected_in_revisions_and_selectors():
    for fn in (argocdclient.valid_rev, argocdclient.valid_selector, argocdclient.valid_ns):
        with pytest.raises(argocdclient.ArgoError):
            fn("ok\n")


def test_empty_argocd_answer_is_a_clean_error_not_a_traceback(capsys):
    c, f = client([])
    f.rc_map["app get"] = 0
    assert cli.main(["app", "ghost"], client=c) == 1
    assert "ERROR" in capsys.readouterr().err


def test_revision_and_selector_cannot_start_with_a_dash():
    with pytest.raises(argocdclient.ArgoError):
        argocdclient.valid_rev("--force")
    with pytest.raises(argocdclient.ArgoError):
        argocdclient.valid_selector("-x=y")
    assert argocdclient.valid_rev("release/1.2~3") and argocdclient.valid_selector("team=a,env in (x, y)")


def test_server_flags_come_from_the_instance_and_argv_is_a_list_without_a_shell():
    c, f = client([app("a")], base="http://argo.local:8080", insecure=True)
    cli.main(["apps"], client=c)
    argv = f.calls[0]
    assert argv[:3] == ["argocd", "app", "list"] and "--server" in argv and "argo.local:8080" in argv
    assert "--grpc-web" in argv and "--plaintext" in argv and "--insecure" in argv


def test_sync_without_yes_only_previews_and_never_executes(capsys):
    c, f = client([app("a")])
    assert cli.main(["sync", "a", "--prune"], client=c) == 0
    out = capsys.readouterr().out
    assert "PREVIEW sync" in out and "NOT EXECUTED" in out and "prune=True" in out
    assert f.mutating() == []
    cli.main(["rollback", "a", "3"], client=c)
    cli.main(["refresh", "a", "--hard"], client=c)
    assert f.mutating() == []


def test_writes_run_only_with_yes_and_rollback_validates_the_history_id(capsys):
    c, f = client([app("a")])
    cli.main(["sync", "a", "--revision", "main", "--yes"], client=c)
    assert any(x[1:3] == ["app", "sync"] and "--revision" in x and "main" in x for x in f.mutating())
    cli.main(["rollback", "a", "3", "--yes"], client=c)
    assert any(x[1:3] == ["app", "rollback"] and x[3:5] == ["a", "3"] for x in f.mutating())
    capsys.readouterr()
    assert cli.main(["rollback", "a", "99", "--yes"], client=c) == 1
    assert "history id 99 not found" in capsys.readouterr().err


def test_option_injection_in_a_command_is_rejected_before_argocd_runs(capsys):
    c, f = client([app("a")])
    assert cli.main(["sync", "--yes", "--", "--grpc-web"], client=c) == 1
    assert "invalid application name" in capsys.readouterr().err
    assert f.mutating() == []


def test_project_scope_blocks_apps_of_other_projects_and_filters_lists(capsys):
    c, f = client([app("mine", "team-a"), app("theirs", "team-b")], project="team-a")
    assert cli.main(["app", "theirs"], client=c) == 1
    assert "outside this instance's scope" in capsys.readouterr().err
    assert cli.main(["sync", "theirs", "--yes"], client=c) == 1
    assert f.mutating() == []
    cli.main(["apps"], client=c)
    assert "-p" in f.calls[-1] and "team-a" in f.calls[-1]
    assert cli.main(["apps", "--project", "team-b"], client=c) == 1


def test_clusters_and_repos_show_metadata_but_never_credentials(capsys):
    c, _ = client([])
    cli.main(["clusters"], client=c)
    cli.main(["repos"], client=c)
    out = capsys.readouterr().out
    assert "prod" in out and "https://git/x" in out
    assert "SECRET" not in out


def test_apps_filters_and_problems_summary(capsys):
    bad = app("bad", health="Degraded", sync="OutOfSync", resources=[{"kind": "Deployment", "name": "web", "namespace": "ns", "health": {"status": "Degraded", "message": "0/2 ready"}, "status": "OutOfSync"}])
    c, _ = client([app("ok"), bad])
    cli.main(["apps", "--unhealthy"], client=c)
    out = capsys.readouterr().out
    assert "1 app(s)" in out and "bad" in out and "ok [" not in out
    cli.main(["problems", "bad"], client=c)
    assert "Deployment/web" in capsys.readouterr().out


def test_diff_maps_argocd_exit_code_one_to_differences(capsys):
    c, f = client([app("a")])
    cli.main(["diff", "a"], client=c)
    assert "no differences" in capsys.readouterr().out
    f.rc_map["app diff"] = 1
    cli.main(["diff", "a"], client=c)
    assert "+ new" in capsys.readouterr().out
    f.rc_map["app diff"] = 20
    assert cli.main(["diff", "a"], client=c) == 1


def test_secrets_in_argocd_output_are_masked():
    assert "abc" not in argocdclient.redact("Authorization: Bearer abc") and "***" in argocdclient.redact("token=abc")


def test_fetch_builds_the_link_card_for_an_application(capsys):
    c, _ = client([app("a", health="Degraded", sync="OutOfSync")])
    cli.main(["fetch", "--kind", "application", "--ident", json.dumps({"name": "a", "ns": "argocd"})], client=c)
    card = json.loads(capsys.readouterr().out)
    assert card["title"] == "a" and card["status"] == "Degraded/OutOfSync" and card["url"].endswith("/applications/argocd/a")
    assert card["fields"]["project"] == "team-a"


def test_connection_test_reports_steps_and_fails_clearly_when_not_logged_in(conf, monkeypatch, capsys):
    f = FakeArgo([app("a")])
    monkeypatch.setattr(subprocess, "run", f)
    assert cli.main(["--instance", "b", "test"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and [s["step"] for s in out["steps"]] == ["config", "binary", "server", "session", "apps"]
    assert cli.main(["--instance", "zzz", "test"]) == 0
    unknown = json.loads(capsys.readouterr().out)
    assert not unknown["ok"] and unknown["steps"][0]["step"] == "config"
    f.rc_map["account get-user-info"] = 1
    cli.main(["--instance", "b", "test"])
    bad = json.loads(capsys.readouterr().out)
    assert not bad["ok"] and "argocd login" in bad["message"] and bad["steps"][-1]["step"] == "session"


def test_missing_binary_is_reported_by_test(conf, monkeypatch, capsys):
    def boom(*a, **k):
        raise FileNotFoundError()
    monkeypatch.setattr(subprocess, "run", boom)
    cli.main(["--instance", "b", "test"])
    out = json.loads(capsys.readouterr().out)
    assert not out["ok"] and out["steps"][-1]["step"] == "binary" and "not found" in out["message"]


# ---------------------------------------------------------------- watcher

def _watch(monkeypatch, tmp_path, mode, apps, extra=(), conf_=None):
    monkeypatch.setenv("WATCH_STATE_DIR", str(tmp_path))
    f = FakeArgo(apps)
    monkeypatch.setattr(subprocess, "run", f)
    return aw.main([mode, "--instance", "b", *extra])


def test_out_of_sync_watcher_baselines_silently_then_reports_each_transition_once(conf, monkeypatch, tmp_path, capsys):
    assert _watch(monkeypatch, tmp_path, "out-of-sync", [app("a", sync="OutOfSync"), app("b")]) == 0
    assert capsys.readouterr().out == ""
    assert _watch(monkeypatch, tmp_path, "out-of-sync", [app("a", sync="OutOfSync"), app("b", sync="OutOfSync")]) == 200
    out = capsys.readouterr().out
    assert "b [team-a] became OutOfSync" in out and "a [" not in out
    assert _watch(monkeypatch, tmp_path, "out-of-sync", [app("a", sync="OutOfSync"), app("b", sync="OutOfSync")]) == 0


def test_health_watcher_reports_changes_and_only_degraded_filters_recoveries(conf, monkeypatch, tmp_path, capsys):
    _watch(monkeypatch, tmp_path, "health", [app("a")])
    assert _watch(monkeypatch, tmp_path, "health", [app("a", health="Degraded")], ["--only-degraded"]) == 200
    assert "Healthy -> Degraded" in capsys.readouterr().out
    assert _watch(monkeypatch, tmp_path, "health", [app("a", health="Healthy")], ["--only-degraded"]) == 0


def test_sync_watcher_reports_a_finished_operation_once_and_includes_failure_message(conf, monkeypatch, tmp_path, capsys):
    _watch(monkeypatch, tmp_path, "sync", [app("a", op="Succeeded", finished="t1")])
    assert capsys.readouterr().out == ""
    assert _watch(monkeypatch, tmp_path, "sync", [app("a", op="Failed", finished="t2")]) == 200
    out = capsys.readouterr().out
    assert "sync Failed" in out and "boom" in out
    assert _watch(monkeypatch, tmp_path, "sync", [app("a", op="Failed", finished="t2")]) == 0


def test_watcher_errors_exit_1_without_leaking_and_unknown_app_is_an_error(conf, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("WATCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(subprocess, "run", FakeArgo([app("a")], {"app list": 1}))
    assert aw.main(["health", "--instance", "b"]) == 1
    monkeypatch.setattr(subprocess, "run", FakeArgo([app("a")]))
    assert aw.main(["health", "--instance", "b", "--app", "nope"]) == 1
    assert aw.main(["health", "--instance", "zzz"]) == 1
