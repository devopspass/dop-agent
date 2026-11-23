"""Jenkins package driver: instance config, skill rendering, CLI reads/writes, log masking, watcher script.

Feature: Jenkins is a package driver (CLI + skill + watcher recipes) with NO built-in host. Guarantees:
each instance gets its own skill/host; the CLI resolves host+user+token from the named instance (token never in argv,
output or errors); job paths cannot escape `/job/<x>/`; console logs are ANSI-stripped, annotation-free and masked;
writes (trigger/abort/rebuild) are dry-run unless `--yes`; the build watcher records a silent baseline and then
reports each newly finished build once; the queue watcher reports each stuck item once.
All HTTP is faked; no real Jenkins or credentials are used.
"""
import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

from integrations.registry import build_registry
from integrations.skills import ensure_skills
import watch_scripts

DRV = Path(__file__).resolve().parents[1] / "drivers" / "jenkins"
sys.path.insert(0, str(DRV / "cli"))
import jkclient  # noqa: E402
import jkconfig  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


jw = _load("jenkins_watch", DRV / "watch" / "jenkins_watch.py")
cli = _load("jenkins_cli_mod", DRV / "cli" / "cli.py")


def _cfg():
    def inst(host, **opts):
        return {"driver": "jenkins", "enabled": True, "base_url": host,
                "auth": {"type": "api_token", "token": "x"}, "options": {"user": "bob", **opts}}
    return {"one": inst("https://ci.one.example"), "two": inst("https://ci.two.example", title="Second")}


def test_each_instance_gets_its_own_skill_without_cli_hints_in_description(tmp_path):
    reg = build_registry(_cfg())
    assert ensure_skills(reg, tmp_path) == ["jenkins-one", "jenkins-two"]
    one = (tmp_path / "jenkins-one" / "SKILL.md").read_text()
    assert "https://ci.one.example" in one and "--instance one" in one and "ci.two.example" not in one
    assert "${{" not in one
    desc = next(ln for ln in one.splitlines() if ln.startswith("description:"))
    assert "--instance" not in desc and "cli.py" not in desc


def test_links_route_to_matching_host_and_build_vs_job_kinds():
    reg = build_registry(_cfg())
    inst, kind, ident = reg.resolve_url("https://ci.two.example/job/team/job/app/42/console")
    assert inst.name == "two" and kind == "build" and ident == {"path": "job/team/job/app", "id": "42"}
    assert reg.resolve_url("https://ci.two.example/job/team/job/app/")[1] == "job"
    assert reg.resolve_url("https://other.example/job/a/") is None


def test_watcher_recipe_pins_instance_and_exports_it():
    reg = build_registry(_cfg())
    meta = watch_scripts.apply_watcher({"script": "jenkins_watch.py", "watcher": "jenkins-build",
                                        "integration": "two"}, reg)
    assert meta["args"][0] == "build"
    assert watch_scripts.instance_env(meta, reg)["JENKINS_INSTANCE"] == "two"


def test_user_option_rejects_injection_characters():
    cfg = _cfg()
    cfg["one"]["options"]["user"] = 'bob"; rm -rf /'
    summary = {i["name"]: i for i in build_registry(cfg).integrations_summary()}
    assert not summary["one"]["valid"]


@pytest.fixture
def conf(tmp_path, monkeypatch):
    p = tmp_path / "config.yaml"
    p.write_text("integrations:\n"
                 "  a: {driver: jenkins, base_url: 'https://a.example/', options: {user: bob}, auth: {type: api_token, token: '${env:JK_A}'}}\n"
                 "  b: {driver: jenkins, base_url: 'https://b.example', options: {user: amy}, auth: {type: api_token, token: tok-b}}\n"
                 "  nouser: {driver: jenkins, base_url: 'https://c.example', auth: {token: t}}\n"
                 "  off: {driver: jenkins, enabled: false, base_url: 'https://d.example', options: {user: x}, auth: {token: t}}\n")
    monkeypatch.setenv("DOP_AGENT_CONFIG", str(p))
    monkeypatch.setenv("JK_A", "tok-secret-a")
    monkeypatch.delenv("JENKINS_INSTANCE", raising=False)
    return p


def test_config_resolves_user_and_token_of_named_instance_only(conf):
    assert jkconfig.load_instance("a")[:4] == ("a", "https://a.example", "bob", "tok-secret-a")
    with pytest.raises(jkconfig.InstanceError, match="not found/enabled"):
        jkconfig.load_instance("off")
    with pytest.raises(jkconfig.InstanceError, match="options.user"):
        jkconfig.load_instance("nouser")
    with pytest.raises(jkconfig.InstanceError, match="a, b"):
        jkconfig.load_instance(None)


def test_connection_test_reports_steps_and_never_leaks_the_token(conf, monkeypatch, capsys):
    class R:
        ok, status_code, headers = True, 200, {"X-Jenkins": "2.440"}
        def json(self): return {"fullName": "Bob"}
    import requests
    seen = {}
    monkeypatch.setattr(requests, "get", lambda *a, **k: seen.update(auth=k.get("auth")) or R())
    assert cli.main(["--instance", "a", "test"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and [s["step"] for s in out["steps"]] == ["config", "reach", "auth"]
    assert "tok-secret-a" not in json.dumps(out) and seen["auth"] == ("bob", "tok-secret-a")
    assert cli.main(["--instance", "zzz", "test"]) == 0
    assert not json.loads(capsys.readouterr().out)["ok"]


def test_job_paths_are_validated_and_quoted():
    assert jkclient.job_url_path("team/app one") == "/job/team/job/app%20one"
    assert jkclient.norm_path("https://ci/job/a/job/b/12/console".replace("https://ci", ""), "") == "a/b"
    assert jkclient.job_url_path("team/feature%2Fx") == "/job/team/job/feature%2Fx"
    for bad in ("../etc", "a/../b", "a?x=1", "a#b", "a\\b"):
        with pytest.raises(jkclient.JenkinsError):
            jkclient.job_url_path(bad)
    assert jkclient.build_ref("failed") == "lastFailedBuild" and jkclient.build_ref(7) == "7"
    with pytest.raises(jkclient.JenkinsError):
        jkclient.build_ref("../x")


class FakeResp:
    def __init__(self, data=None, status=200, text="", lines=None, headers=None):
        self._d, self.status_code, self.text = data, status, text
        self.ok = status < 400
        self.headers = headers or {}
        self._lines = lines

    def json(self): return self._d
    def iter_content(self, chunk_size=0, decode_unicode=False):
        yield "".join(l + "\n" for l in self._lines).encode()
    def close(self): pass


class FakeSession:
    def __init__(self, routes):
        self.routes, self.auth, self.calls = routes, None, []

    def _hit(self, method, url, **k):
        self.calls.append((method, url, k.get("data")))
        for frag, resp in self.routes.items():
            if frag in url:
                return resp() if callable(resp) else resp
        return FakeResp(status=404, text="nope")

    def get(self, url, **k): return self._hit("GET", url, **k)
    def post(self, url, **k): return self._hit("POST", url, **k)


def client(routes):
    s = FakeSession(routes)
    return jkclient.Client("https://ci.example", "bob", "tok-secret", session=s), s


BUILD = {"number": 7, "url": "https://ci.example/job/a/7/", "result": "FAILURE", "building": False, "duration": 133000,
         "timestamp": 1_700_000_000_000, "builtOn": "agent-1", "actions": [
             {"parameters": [{"name": "BRANCH", "value": "main"}, {"name": "DEPLOY_TOKEN", "value": "s3cr3t"}]},
             {"causes": [{"shortDescription": "Started by user bob"}]}],
         "changeSets": [{"items": [{"commitId": "abcdef123456", "msg": "fix it", "author": {"fullName": "Amy"}}]}]}


def test_build_info_shows_params_changes_and_hides_secret_parameter_values(capsys):
    c, _ = client({"/job/a/7/api/json": FakeResp(BUILD)})
    assert cli.main(["--instance", "x", "build-info", "a", "7"], client=c) == 0
    out = capsys.readouterr().out
    assert "FAILURE" in out and "2m13s" in out and "BRANCH=main" in out and "DEPLOY_TOKEN=***" in out
    assert "s3cr3t" not in out and "abcdef12 Amy: fix it" in out and "Started by user bob" in out


def test_log_tail_strips_ansi_and_annotations_masks_secrets_and_greps(capsys):
    lines = ["start", "\x1b[8mha:////4abc==\x1b[0m\x1b[31mERROR boom\x1b[0m", "password=hunter2 used",
             "Authorization: Bearer abcdefghij123", "ok line"]
    c, _ = client({"/lastBuild/consoleText": FakeResp(lines=lines)})
    assert cli.main(["--instance", "x", "log-tail", "a", "--lines", "10"], client=c) == 0
    out = capsys.readouterr().out
    assert "ERROR boom" in out and "\x1b" not in out and "ha:////" not in out
    assert "hunter2" not in out and "abcdefghij123" not in out
    assert cli.main(["--instance", "x", "log-tail", "a", "--grep", "error"], client=c) == 0
    assert capsys.readouterr().out.strip().splitlines()[1:] == ["ERROR boom"]
    assert cli.main(["--instance", "x", "log-tail", "a", "--lines", "1"], client=c) == 0
    assert capsys.readouterr().out.strip().splitlines()[1:] == ["ok line"]


def test_writes_are_dry_run_without_yes_and_post_with_yes(capsys):
    c, s = client({"/job/a/build": FakeResp(status=201, headers={"Location": "https://ci.example/queue/item/9/"}),
                   "/job/a/buildWithParameters": FakeResp(status=201, headers={"Location": "q/9"}),
                   "/job/a/7/stop": FakeResp(status=200),
                   "/job/a/7/api/json": FakeResp(BUILD)})
    for argv in (["trigger", "a", "--param", "X=1"], ["abort", "a", "7"], ["rebuild", "a", "7"]):
        assert cli.main(["--instance", "x"] + argv, client=c) == 0
        assert "DRY-RUN" in capsys.readouterr().out
    assert not [x for x in s.calls if x[0] == "POST"]
    assert cli.main(["--instance", "x", "trigger", "a", "--param", "X=1", "--yes"], client=c) == 0
    assert cli.main(["--instance", "x", "abort", "a", "7", "--yes"], client=c) == 0
    assert cli.main(["--instance", "x", "rebuild", "a", "7", "--yes"], client=c) == 0
    posts = [x for x in s.calls if x[0] == "POST" and "crumb" not in x[1]]
    assert [p[1].split("example")[1] for p in posts] == ["/job/a/buildWithParameters", "/job/a/7/stop",
                                                         "/job/a/buildWithParameters"]
    assert posts[0][2] == {"X": "1"} and posts[2][2] == {"BRANCH": "main", "DEPLOY_TOKEN": "s3cr3t"}
    assert "s3cr3t" not in capsys.readouterr().out


def test_error_text_never_contains_the_token(capsys):
    c, _ = client({"/job/a/7/api/json": FakeResp(status=403, text="token=tok-secret denied")})
    assert cli.main(["--instance", "x", "build-info", "a", "7"], client=c) == 1
    assert "tok-secret" not in capsys.readouterr().err


def _tree(builds):
    """root folder 'team' with jobs a, b and subfolder 'sub' with job c; builds = {fullName: number}."""
    def job(full, n, res="SUCCESS"):
        d = {"name": full.split("/")[-1], "fullName": full, "_class": "hudson.model.FreeStyleProject", "color": "blue"}
        if n:
            d["lastCompletedBuild"] = {"number": n, "result": res, "duration": 61000, "url": f"https://ci.example/{full}/{n}/"}
        return d
    return {"/job/team/job/sub/api/json": FakeResp({"jobs": [job("team/sub/c", builds.get("team/sub/c"), "FAILURE")]}),
            "/job/team/api/json": FakeResp({"_class": "com.cloudbees.hudson.plugins.folder.Folder", "jobs": [
                job("team/a", builds.get("team/a")), job("team/b", builds.get("team/b")),
                {"name": "sub", "fullName": "team/sub", "_class": "com.cloudbees.hudson.plugins.folder.Folder"}]})}


def test_build_watcher_baselines_then_reports_each_finished_build_once(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WATCH_STATE_DIR", str(tmp_path))
    argv = ["build", "--instance", "x", "--target", "team"]
    c, _ = client(_tree({"team/a": 1, "team/b": 5, "team/sub/c": 2}))
    assert jw.main(argv, client=c) == 0 and capsys.readouterr().out == ""
    assert jw.main(argv, client=c) == 0
    c, _ = client(_tree({"team/a": 2, "team/b": 5, "team/sub/c": 3}))
    assert jw.main(argv, client=c) == 200
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 2 and "team/a #2 SUCCESS" in out[0] and "team/sub/c #3 FAILURE" in out[1]
    assert jw.main(argv, client=c) == 0
    c, _ = client(_tree({"team/a": 3, "team/b": 5, "team/sub/c": 3}))
    assert jw.main(argv + ["--only-failed"], client=c) == 0


def test_build_watcher_on_a_single_job_and_transient_errors_are_silent(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WATCH_STATE_DIR", str(tmp_path))
    job = lambda n: {"/job/team/job/a/api/json": FakeResp({"fullName": "team/a", "_class": "hudson.model.FreeStyleProject",
                                                           "lastCompletedBuild": {"number": n, "result": "FAILURE", "duration": 1000, "url": "u"}})}
    argv = ["build", "--instance", "x", "--target", "team/a"]
    assert jw.main(argv, client=client(job(1))[0]) == 0
    assert jw.main(argv, client=client(job(2))[0]) == 200 and "FAILURE" in capsys.readouterr().out
    assert jw.main(argv, client=client({"/job/team/job/a/api/json": FakeResp(status=503, text="busy")})[0]) == 0
    assert jw.main(["build", "--instance", "x"], client=client({})[0]) == 1


def test_queue_stuck_watcher_reports_old_items_once(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WATCH_STATE_DIR", str(tmp_path))
    now = time.time() * 1000
    items = {"/queue/api/json": FakeResp({"items": [
        {"id": 1, "why": "Waiting for next available executor", "inQueueSince": now - 3 * 3600_000, "task": {"name": "old"}},
        {"id": 2, "why": "x", "inQueueSince": now - 60_000, "task": {"name": "fresh"}}]})}
    argv = ["queue-stuck", "--instance", "x", "--min-minutes", "30"]
    c, _ = client(items)
    assert jw.main(argv, client=c) == 200
    out = capsys.readouterr().out
    assert "#1 old waiting 180m" in out and "fresh" not in out
    assert jw.main(argv, client=c) == 0


def test_watcher_scripts_are_executable_and_not_group_writable():
    p = DRV / "watch" / "jenkins_watch.py"
    assert p.stat().st_mode & 0o111 and not p.stat().st_mode & 0o022


def test_fetch_returns_card_json_for_build_and_job_links(capsys):
    c, _ = client({"/job/team/job/a/7/api/json": FakeResp(BUILD),
                   "/job/team/job/a/api/json": FakeResp({"fullName": "team/a", "url": "https://ci.example/job/team/job/a/",
                                                         "color": "red", "lastBuild": {"number": 7, "result": "FAILURE"}})})
    assert cli.main(["--instance", "x", "fetch", "--kind", "build", "--ident", '{"path": "job/team/job/a", "id": "7"}'], client=c) == 0
    card = json.loads(capsys.readouterr().out)
    assert card["status"] == "FAILURE" and card["fields"]["duration"] == "2m13s" and card["title"] == "team/a #7"
    assert cli.main(["--instance", "x", "fetch", "--kind", "job", "--ident", '{"path": "job/team/job/a"}'], client=c) == 0
    assert json.loads(capsys.readouterr().out)["fields"]["last_build"] == "#7"
