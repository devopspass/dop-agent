"""GitLab package driver: generic multi-instance config, skill rendering, CLI test, watcher script.

Feature: the GitLab integration is a package driver (CLI + skill + watcher recipes) with NO built-in host.
Guarantees: several instances (hosts/tokens) each get their own skill with their own host; the CLI resolves
host+token from the named instance (token never in argv); `test` reports per-step results; the watcher script
records a silent baseline, then reports each new review request / pipeline result / human comment once.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from integrations.registry import build_registry
from integrations.skills import ensure_skills
import watch_scripts

DRV = Path(__file__).resolve().parents[1] / "drivers" / "gitlab"
sys.path.insert(0, str(DRV / "cli"))
import glconfig  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gw = _load("gitlab_watch", DRV / "watch" / "gitlab_watch.py")
cli = _load("gitlab_cli_mod", DRV / "cli" / "cli.py")


def _cfg():
    def inst(host, on=True, **opts):
        return {"driver": "gitlab", "enabled": on, "base_url": host,
                "auth": {"type": "private_token", "token": "x"}, "options": opts}
    return {"one": inst("https://git.one.example"), "two": inst("https://git.two.example", title="Second")}


def test_each_instance_gets_its_own_skill_with_its_own_host_and_no_leftover_templates(tmp_path):
    reg = build_registry(_cfg())
    assert ensure_skills(reg, tmp_path) == ["gitlab-one", "gitlab-two"]
    one = (tmp_path / "gitlab-one" / "SKILL.md").read_text()
    assert "https://git.one.example" in one and "--instance one" in one and "git.two.example" not in one
    assert "${{" not in one.replace("$${{", "")


def test_links_route_to_the_instance_with_the_matching_host():
    reg = build_registry(_cfg())
    assert reg.resolve_url("https://git.two.example/g/p/-/merge_requests/5")[0].name == "two"
    assert reg.resolve_url("https://other.example/g/p/-/merge_requests/5") is None


def test_watcher_recipe_pins_instance_and_exports_it_to_the_script():
    reg = build_registry(_cfg())
    meta = watch_scripts.apply_watcher({"script": "gitlab_watch.py", "watcher": "gitlab-assigned",
                                        "integration": "two"}, reg)
    assert meta["args"][0] == "assigned"
    assert watch_scripts.instance_env(meta, reg)["GITLAB_INSTANCE"] == "two"


def test_no_default_gitlab_host_is_built_into_zero_config_registry():
    from integrations.registry import default_integrations
    assert "gitlab" not in default_integrations()


@pytest.fixture
def conf(tmp_path, monkeypatch):
    p = tmp_path / "config.yaml"
    p.write_text("integrations:\n"
                 "  a: {driver: gitlab, base_url: 'https://a.example/', auth: {type: private_token, token: '${env:GL_A}'}}\n"
                 "  b: {driver: gitlab, base_url: 'https://b.example', auth: {type: private_token, token: tok-b}}\n"
                 "  off: {driver: gitlab, enabled: false, base_url: 'https://c.example', auth: {token: t}}\n")
    monkeypatch.setenv("DOP_AGENT_CONFIG", str(p))
    monkeypatch.setenv("GL_A", "tok-a")
    monkeypatch.delenv("GITLAB_INSTANCE", raising=False)
    return p


def test_cli_resolves_host_and_token_of_the_named_instance(conf):
    assert glconfig.load_instance("a")[:3] == ("a", "https://a.example", "tok-a")
    assert glconfig.load_instance("b")[2] == "tok-b"


def test_cli_asks_for_instance_when_several_are_enabled_and_hides_disabled_ones(conf):
    with pytest.raises(glconfig.InstanceError, match="a, b"):
        glconfig.load_instance(None)
    with pytest.raises(glconfig.InstanceError, match="not found/enabled"):
        glconfig.load_instance("off")


def test_connection_test_reports_steps_and_never_leaks_the_token(conf, monkeypatch, capsys):
    class R:
        ok, status_code = True, 200
        def json(self): return {"username": "me"}
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: R())
    assert cli.main(["--instance", "a", "test"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and [s["step"] for s in out["steps"]] == ["config", "reach", "auth"]
    assert "tok-a" not in json.dumps(out)
    assert cli.main(["--instance", "zzz", "test"]) == 0
    bad = json.loads(capsys.readouterr().out)
    assert not bad["ok"] and bad["steps"][0]["step"] == "config"


class FakeGl:
    def __init__(self):
        self.mrs, self.notes = [], {}

    def _get(self, path, params=None):
        class R:
            def __init__(s, d): s.d = d
            def json(s): return s.d
        if path == "/user":
            return R({"username": "me"})
        if path in getattr(self, "by_path", {}):
            return R(self.by_path[path])
        if path.endswith("/notes"):
            return R(self.notes.get(path, []))
        return R(self.mrs)


def mr(i, title="t", pipeline=None):
    return {"id": i, "iid": i, "project_id": 7, "title": title, "web_url": f"https://h/g/p/-/merge_requests/{i}",
            "references": {"full": f"g/p!{i}"}, "head_pipeline": pipeline}


@pytest.fixture
def gl(tmp_path, monkeypatch):
    monkeypatch.setenv("WATCH_STATE_DIR", str(tmp_path))
    f = FakeGl()
    monkeypatch.setattr(gw, "_client", lambda inst: ("one", f))
    return f


def test_review_requests_baseline_is_silent_then_each_new_mr_is_reported_once(gl, capsys):
    gl.mrs = [mr(1)]
    assert gw.main(["review-requested"]) == 0 and capsys.readouterr().out == ""
    gl.mrs = [mr(2, "Fix x"), mr(1)]
    assert gw.main(["review-requested"]) == 200
    out = capsys.readouterr().out
    assert "g/p!2" in out and "g/p!1" not in out
    assert gw.main(["review-requested"]) == 0


def test_pipeline_watch_fires_only_on_terminal_status_change(gl, capsys):
    gl.mrs = [mr(1, pipeline={"id": 9, "status": "running", "web_url": "u"})]
    assert gw.main(["my-mr-pipelines"]) == 0
    gl.mrs = [mr(1, pipeline={"id": 9, "status": "failed", "web_url": "u"})]
    assert gw.main(["my-mr-pipelines"]) == 200 and "pipeline failed" in capsys.readouterr().out
    assert gw.main(["my-mr-pipelines"]) == 0


def test_note_watch_skips_system_bot_and_own_notes(gl, capsys):
    gl.mrs = [mr(1)]
    path = "/projects/7/merge_requests/1/notes"
    gl.notes[path] = [{"id": 1, "body": "old", "author": {"username": "bob"}}]
    assert gw.main(["my-mr-notes"]) == 0
    gl.notes[path] = [
        {"id": 5, "body": "mine", "author": {"username": "me"}},
        {"id": 4, "body": "bot", "author": {"username": "project_12_bot_ab"}},
        {"id": 3, "body": "sys", "system": True, "author": {"username": "bob"}},
        {"id": 2, "body": "please\nfix this", "author": {"username": "bob"}},
        {"id": 1, "body": "old", "author": {"username": "bob"}}]
    assert gw.main(["my-mr-notes"]) == 200
    out = capsys.readouterr().out
    assert "bob: please fix this" in out and "mine" not in out and "bot" not in out and "sys" not in out


def test_transient_api_errors_are_a_silent_skip_and_other_errors_exit_1(gl, monkeypatch, capsys):
    def boom(*a, **k): raise RuntimeError("GitLab API error 503 for /x")
    monkeypatch.setattr(gl, "_get", boom)
    assert gw.main(["assigned"]) == 0
    monkeypatch.setattr(gl, "_get", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("401 Unauthorized")))
    assert gw.main(["assigned"]) == 1


def test_per_target_watchers_baseline_silently_then_fire_once_on_terminal_result(gl, capsys):
    """No bot-side polling: one MR / pipeline / job is watched by a script watcher (use until_changed)."""
    gl.by_path = {
        "/projects/g%2Fp/merge_requests/1": {"state": "opened", "web_url": "u", "head_pipeline": {"id": 9, "status": "running"}},
        "/projects/g%2Fp/pipelines/9": {"id": 9, "status": "running", "web_url": "pu"},
        "/projects/g%2Fp/jobs/5": {"status": "running", "name": "build", "web_url": "ju"}}
    for mode, tgt in (("mr", "g/p#1"), ("pipeline", "g/p#9"), ("job", "g/p#5")):
        assert gw.main([mode, "--target", tgt]) == 0
    gl.by_path["/projects/g%2Fp/merge_requests/1"] = {"state": "merged", "web_url": "u", "head_pipeline": {"id": 9, "status": "success", "web_url": "pu"}}
    gl.by_path["/projects/g%2Fp/pipelines/9"] = {"id": 9, "status": "failed", "web_url": "pu"}
    gl.by_path["/projects/g%2Fp/jobs/5"] = {"status": "success", "name": "build", "web_url": "ju"}
    for mode, tgt in (("mr", "g/p#1"), ("pipeline", "g/p#9"), ("job", "g/p#5")):
        assert gw.main([mode, "--target", tgt]) == 200
    out = capsys.readouterr().out
    assert "pipeline success on g/p#1" in out and "is merged" in out and "pipeline g/p#9 (#9) failed" in out and "job g/p#5 (build) success" in out
    assert gw.main(["job", "--target", "g/p#5"]) == 0


def test_bad_target_is_an_error_not_a_silent_skip(gl):
    assert gw.main(["mr", "--target", "no-hash"]) == 1
