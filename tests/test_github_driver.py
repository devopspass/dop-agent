"""GitHub package driver: generic multi-instance config, skill rendering, CLI, furl cards, watcher script.

Feature: the GitHub integration is a package driver (CLI + skill + watcher recipes) with NO built-in host;
github.com and GitHub Enterprise instances coexist, each with its own skill/host/token.
Guarantees: API endpoints derive from the web base_url (github.com, *.ghe.com, GHES /api/v3, api_url override);
links go to the instance whose host matches; the CLI parses explicit refs, masks secrets in logs, issues the
expected REST/GraphQL calls and `test` reports per-step results without a token; the watcher records a silent
baseline, then reports each new review request / checks result / human comment / finished run exactly once.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

import watch_scripts
from integrations.registry import build_registry
from integrations.skills import ensure_skills

DRV = Path(__file__).resolve().parents[1] / "drivers" / "github"
sys.path.insert(0, str(DRV / "cli"))
import ghapi  # noqa: E402
import ghconfig  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cli = _load("github_cli_mod", DRV / "cli" / "cli.py")
gw = _load("github_watch", DRV / "watch" / "github_watch.py")


def _cfg():
    def inst(host, **opts):
        return {"driver": "github", "enabled": True, "base_url": host, "auth": {"type": "private_token", "token": "x"}, "options": opts}
    return {"dotcom": inst("https://github.com"), "ghe": inst("https://ghe.example.com", title="Enterprise")}


def test_each_instance_gets_its_own_skill_with_its_own_host_and_links_route_by_host(tmp_path):
    reg = build_registry(_cfg())
    assert ensure_skills(reg, tmp_path) == ["github-dotcom", "github-ghe"]
    text = (tmp_path / "github-ghe" / "SKILL.md").read_text()
    assert "https://ghe.example.com" in text and "--instance ghe" in text and "${{" not in text.replace("$${{", "")
    assert "Enterprise" in text and "github-run" in text
    it, kind, ident = reg.resolve_url("https://ghe.example.com/o/r/pull/12/files")
    assert (it.name, kind, ident) == ("ghe", "pull_request", {"repo": "o/r", "id": "12"})
    it, kind, ident = reg.resolve_url("https://github.com/o/r/actions/runs/99/job/5")
    assert (it.name, kind, ident["id"]) == ("dotcom", "run", "99")
    assert reg.resolve_url("https://elsewhere.example.com/o/r/pull/1") is None


def test_watcher_recipe_prefixes_mode_and_pins_instance_env():
    reg = build_registry({"ghe": _cfg()["ghe"]})
    meta = watch_scripts.apply_watcher({"script": "github_watch.py", "watcher": "github-pr", "args": ["--ref", "o/r#1"]}, reg)
    assert meta["integration"] == "ghe" and meta["args"][:3] == ["pr", "--ref", "o/r#1"]
    assert watch_scripts.instance_env(meta, reg)["GITHUB_INSTANCE"] == "ghe"


@pytest.mark.parametrize("base,opts,rest,gql", [
    ("https://github.com", {}, "https://api.github.com", "https://api.github.com/graphql"),
    ("https://acme.ghe.com", {}, "https://api.acme.ghe.com", "https://api.acme.ghe.com/graphql"),
    ("https://ghe.example.com", {}, "https://ghe.example.com/api/v3", "https://ghe.example.com/api/graphql"),
    ("https://ghe.example.com", {"api_url": "https://api.ghe.example.com"}, "https://api.ghe.example.com", "https://api.ghe.example.com/graphql"),
])
def test_api_endpoints_derive_from_web_base_url(base, opts, rest, gql):
    assert ghconfig.api_urls(base, opts) == (rest, gql)


def test_ref_parsing_accepts_hash_form_and_urls_and_rejects_garbage():
    assert cli.parse_ref("o/r#12") == ("o/r", "12")
    assert cli.parse_ref("https://github.com/o/r/pull/7/files") == ("o/r", "7")
    for bad in ("o/r", "12", "o#1", "../x/y#1"):
        with pytest.raises(ghapi.GhError):
            cli.parse_ref(bad)


def test_log_cleaning_strips_timestamps_ansi_and_masks_secrets():
    raw = "2026-11-20T10:00:00.1234567Z \x1b[31mError: token=abc123 failed\x1b[0m\n\n2026-11-20T10:00:01Z ok"
    assert cli.clean_log(raw) == ["Error: token=*** failed", "ok"]
    lines = [f"l{i}" for i in range(100)] + ["boom error here"] + ["x"] * 5
    assert any("boom" in ln for ln in cli.log_tail(lines, grep="boom"))


class Fake:
    """Records REST/GraphQL calls and serves canned JSON by path."""
    def __init__(self, routes=None):
        self.routes, self.calls = routes or {}, []

    def json(self, path, params=None):
        self.calls.append(("GET", path, params))
        r = self.routes[path]
        return r(params) if callable(r) else r

    def get_all(self, path, params=None, max_pages=5, key=None):
        r = self.json(path, params)
        return r.get(key, []) if key else r

    def request(self, method, path, params=None, body=None):
        self.calls.append((method, path, body))
        return self.routes.get((method, path), {"html_url": "http://x/y", "number": 3, "message": "ok"})

    def graphql(self, query, variables=None):
        self.calls.append(("GQL", query.split("(")[0].strip(), variables))
        return self.routes.get("gql", {})


PR = {"state": "open", "merged": False, "draft": False, "title": "T", "html_url": "http://x/pr/1", "user": {"login": "al"},
      "head": {"ref": "feat", "sha": "abcdef1234", "repo": {"full_name": "o/r"}}, "base": {"ref": "main"},
      "mergeable": True, "mergeable_state": "clean", "additions": 3, "deletions": 1, "changed_files": 2,
      "requested_reviewers": [{"login": "bob"}], "requested_teams": []}


def _pr_routes():
    return {"/repos/o/r/pulls/1": PR,
            "/repos/o/r/pulls/1/reviews": [{"user": {"login": "bob"}, "state": "APPROVED", "id": 1}],
            "/repos/o/r/commits/abcdef1234/check-runs": {"check_runs": [
                {"name": "build", "status": "completed", "conclusion": "failure", "html_url": "http://x/b"},
                {"name": "lint", "status": "completed", "conclusion": "success"}]},
            "/repos/o/r/commits/abcdef1234/status": {"statuses": []},
            "gql": {"repository": {"pullRequest": {"reviewThreads": {"pageInfo": {"hasNextPage": False}, "nodes": [
                {"id": "PRRT_1", "isResolved": False, "isOutdated": False, "path": "a.py", "line": 4,
                 "comments": {"totalCount": 1, "nodes": [{"author": {"login": "bob"}, "body": "fix", "databaseId": 77}]}}]}}}}}


def test_pr_status_summarises_reviews_threads_and_failed_checks(capsys):
    assert cli.main(["pr-status", "o/r#1"], client=Fake(_pr_routes())) == 0
    out = capsys.readouterr().out
    assert "bob=APPROVED" in out and "1 unresolved of 1" in out and "checks: failure" in out and "FAILED build" in out


def test_pr_threads_prints_ids_needed_for_resolve_and_reply(capsys):
    cli.main(["pr-threads", "o/r#1", "--unresolved"], client=Fake(_pr_routes()))
    assert "PRRT_1 OPEN @bob a.py:4 c=77" in capsys.readouterr().out


@pytest.mark.parametrize("argv,call", [
    (["pr-merge", "o/r#1", "--method", "squash"], ("PUT", "/repos/o/r/pulls/1/merge", {"merge_method": "squash"})),
    (["pr-review", "o/r#1", "--event", "approve"], ("POST", "/repos/o/r/pulls/1/reviews", {"event": "APPROVE", "body": ""})),
    (["pr-comment", "o/r#1", "--body", "hi", "--reply", "77"], ("POST", "/repos/o/r/pulls/1/comments/77/replies", {"body": "hi"})),
    (["run-rerun", "o/r", "5", "--failed"], ("POST", "/repos/o/r/actions/runs/5/rerun-failed-jobs", None)),
    (["issue-close", "o/r#2"], ("PATCH", "/repos/o/r/issues/2", {"state": "closed", "state_reason": "completed"})),
])
def test_writers_send_expected_rest_call(argv, call):
    f = Fake()
    assert cli.main(argv, client=f) == 0
    assert call in f.calls


def test_thread_resolve_uses_graphql_mutation_and_validates_id():
    f = Fake()
    cli.main(["thread-resolve", "o/r#1", "PRRT_abc12345"], client=f)
    assert f.calls[0][0] == "GQL" and f.calls[0][2] == {"id": "PRRT_abc12345"}
    assert cli.main(["thread-resolve", "o/r#1", "bad id!"], client=f) == 1


def test_api_non_get_needs_yes_and_missing_required_flags_fail(capsys):
    assert cli.main(["api", "DELETE", "/repos/o/r"], client=Fake()) == 1
    assert cli.main(["pr-create", "o/r", "--head", "b"], client=Fake()) == 1
    assert "needs" in capsys.readouterr().err


def test_fetch_builds_card_for_pr_and_run(capsys):
    f = Fake(_pr_routes())
    f.routes["/repos/o/r/actions/runs/9"] = {"name": "CI", "status": "completed", "conclusion": "success", "head_branch": "main",
                                             "event": "push", "html_url": "http://x/run", "display_title": "Build"}
    cli.main(["fetch", "--kind", "pull_request", "--ident", '{"repo":"o/r","id":"1"}'], client=f)
    card = json.loads(capsys.readouterr().out)
    assert card["status"] == "open" and card["status_emoji"] == "🟢" and card["fields"]["checks"] == "failure"
    cli.main(["fetch", "--kind", "run", "--ident", '{"repo":"o/r","id":"9"}'], client=f)
    assert json.loads(capsys.readouterr().out)["status"] == "success"


def test_test_command_reports_config_failure_without_raising(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DOP_AGENT_CONFIG", str(tmp_path / "none.yaml"))
    assert cli.main(["test"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and out["steps"][-1]["step"] == "config" and out["steps"][-1]["ok"] is False


# ---- watcher script
@pytest.fixture
def watch(tmp_path, monkeypatch):
    monkeypatch.setenv("WATCH_STATE_DIR", str(tmp_path))
    box = {"search": [], "pr": dict(PR), "checks": [], "issue": [], "rc": [], "reviews": [], "run": {}, "runs": []}

    def route(path, params=None):
        if path == "/user":
            return {"login": "me"}
        if path == "/search/issues":
            return {"items": box["search"]}
        if path.endswith("/pulls/1"):
            return box["pr"]
        if path.endswith("/check-runs"):
            return {"check_runs": box["checks"]}
        if path.endswith("/status"):
            return {"statuses": []}
        if path.endswith("/issues/1/comments"):
            return box["issue"]
        if path.endswith("/pulls/1/comments"):
            return box["rc"]
        if path.endswith("/pulls/1/reviews"):
            return box["reviews"]
        if path.endswith("/actions/runs/7"):
            return box["run"]
        if path.endswith("/actions/runs"):
            return {"workflow_runs": box["runs"]}
        raise AssertionError(path)

    fake = Fake()
    fake.json = lambda path, params=None: route(path, params)
    monkeypatch.setattr(gw, "_client", lambda inst: ("t", fake, ghapi))
    return box


def item(i, n=1, repo="o/r"):
    return {"id": i, "number": n, "title": f"PR {n}", "html_url": f"http://x/{n}", "repository_url": f"https://api/repos/{repo}"}


def test_review_requested_baseline_is_silent_then_new_pr_reported_once(watch, capsys):
    watch["search"] = [item(1)]
    assert gw.main(["review-requested"]) == 0 and capsys.readouterr().out == ""
    watch["search"] = [item(2, 2), item(1)]
    assert gw.main(["review-requested"]) == gw.RC_CHANGED
    assert "review requested: o/r#2" in capsys.readouterr().out
    assert gw.main(["review-requested"]) == 0


def test_my_pr_checks_fires_when_terminal_state_changes(watch, capsys):
    watch["search"] = [item(1)]
    watch["checks"] = [{"name": "build", "status": "in_progress"}]
    assert gw.main(["my-pr-checks"]) == 0
    watch["checks"] = [{"name": "build", "status": "completed", "conclusion": "failure"}]
    assert gw.main(["my-pr-checks"]) == gw.RC_CHANGED
    out = capsys.readouterr().out
    assert "checks failure: o/r#1" in out and "[failed: build]" in out


def test_my_pr_notes_reports_others_comments_not_mine_not_bots(watch, capsys):
    watch["search"] = [item(1)]
    watch["issue"] = [{"id": 1, "user": {"login": "bob"}, "body": "old", "html_url": "u"}]
    assert gw.main(["my-pr-notes"]) == 0
    watch["issue"] += [{"id": 2, "user": {"login": "bob"}, "body": "please  fix", "html_url": "u2"},
                       {"id": 3, "user": {"login": "me"}, "body": "mine"},
                       {"id": 4, "user": {"login": "ci[bot]"}, "body": "bot"}]
    watch["reviews"] = [{"id": 5, "user": {"login": "amy"}, "state": "APPROVED", "body": "", "html_url": "r"}]
    assert gw.main(["my-pr-notes"]) == gw.RC_CHANGED
    out = capsys.readouterr().out
    assert "comment on o/r#1 by bob: please fix" in out and "review approved on o/r#1 by amy" in out
    assert "mine" not in out and "bot" not in out


def test_pr_watch_reports_merge_and_tells_to_remove_the_watch(watch, capsys):
    assert gw.main(["pr", "--ref", "o/r#1"]) == 0
    watch["pr"] = dict(PR, merged=True, state="closed")
    assert gw.main(["pr", "--ref", "o/r#1"]) == gw.RC_CHANGED
    out = capsys.readouterr().out
    assert "o/r#1 is merged" in out and "can be removed" in out


def test_run_watch_fires_once_on_completion_and_branch_runs_report_new_completions(watch, capsys):
    watch["run"] = {"id": 7, "status": "in_progress"}
    assert gw.main(["run", "--ref", "o/r#7"]) == 0
    watch["run"] = {"id": 7, "status": "completed", "conclusion": "failure", "run_attempt": 1, "name": "CI", "html_url": "u"}
    assert gw.main(["run", "--ref", "o/r#7"]) == gw.RC_CHANGED
    assert gw.main(["run", "--ref", "o/r#7"]) == 0
    capsys.readouterr()
    watch["runs"] = [{"id": 1, "status": "completed", "conclusion": "success", "run_attempt": 1, "name": "CI"}]
    assert gw.main(["branch-runs", "--repo", "o/r", "--branch", "main"]) == 0
    watch["runs"].insert(0, {"id": 2, "status": "completed", "conclusion": "failure", "run_attempt": 1, "name": "CI"})
    assert gw.main(["branch-runs", "--repo", "o/r", "--branch", "main"]) == gw.RC_CHANGED
    assert "run 2 failure" in capsys.readouterr().out


def test_rate_limit_is_a_silent_skip_but_other_errors_exit_1(watch, monkeypatch, capsys):
    def boom(*a, **k):
        raise ghapi.GhError("GitHub API error 403 GET /x: API rate limit exceeded", 403)
    monkeypatch.setattr(gw, "_client", lambda inst: ("t", type("C", (), {"json": staticmethod(boom)})(), ghapi))
    assert gw.main(["assigned"]) == 0
    def bad(*a, **k):
        raise ghapi.GhError("GitHub API error 401 GET /user: Bad credentials", 401)
    monkeypatch.setattr(gw, "_client", lambda inst: ("t", type("C", (), {"json": staticmethod(bad)})(), ghapi))
    assert gw.main(["assigned"]) == 1


def test_watcher_rejects_bad_repo_and_workflow_values(capsys):
    assert gw.main(["branch-runs", "--repo", "../x"]) == 1
    assert gw.main(["branch-runs", "--repo", "o/r", "--workflow", "../x"]) == 1


def test_settings_test_of_unsaved_draft_instance_finds_it_via_private_temp_config(monkeypatch):
    """Settings -> Test on a not-yet-saved instance works: PackageDriver.test_connection hands the CLI a private
    one-instance config (DOP_AGENT_CONFIG, 0600, removed afterwards) containing the draft, since the CLI
    reads config.yaml and ghconfig.load_instance must find the draft."""
    import os, stat
    from drivers.package import PackageDriver
    from drivers.base import Integration
    from config import Secret
    seen = {}

    class It(Integration):
        pass
    man = type("M", (), {"id": "github"})()
    it = Integration(name="personal", manifest=man, base_url="https://github.com", auth={"token": Secret("t0k")})
    d = PackageDriver.__new__(PackageDriver)
    d.integration = it
    with d._draft_config() as env:
        path = env["DOP_AGENT_CONFIG"]
        seen["mode"] = stat.S_IMODE(os.stat(path).st_mode)
        monkeypatch.setenv("DOP_AGENT_CONFIG", path)
        import importlib, sys
        sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "drivers/github/cli"))
        import ghconfig
        importlib.reload(ghconfig)
        name, base, token, _ = ghconfig.load_instance("personal")
    assert (name, base, token) == ("personal", "https://github.com", "t0k")
    assert seen["mode"] == 0o600 and not os.path.exists(path)


def test_cli_supports_actions_artifacts_caches_variables_and_workflow_state():
    """Actions coverage beyond runs/logs: artifacts, caches, variables (secret NAMES only), workflow enable/disable."""
    import cli as ghcli
    calls = []

    class C:
        def json(self, path, params=None):
            if path.endswith("/artifacts"):
                return {"artifacts": [{"id": 5, "name": "dist", "size_in_bytes": 10, "created_at": "2026-01-01T00:00:00Z"}]}
            if path.endswith("/caches"):
                return {"actions_caches": [{"id": 9, "size_in_bytes": 1, "ref": "refs/heads/m", "key": "k"}]}
            if path.endswith("/variables"):
                return {"variables": [{"name": "A", "value": "1"}]}
            return {"secrets": [{"name": "TOKEN_X", "value": "must-not-appear"}]}

        def request(self, method, path, **kw):
            calls.append((method, path))
    c = C()
    ns = ghcli.build_parser().parse_args
    assert "dist" in ghcli.cmd_run_artifacts(c, ns(["run-artifacts", "o/r", "--run", "7"]))
    assert "refs/heads/m" in ghcli.cmd_caches(c, ns(["caches", "o/r"]))
    out = ghcli.cmd_variables(c, ns(["variables", "o/r"]))
    assert "secret TOKEN_X" in out and "must-not-appear" not in out
    ghcli.cmd_workflow_state(c, ns(["workflow-state", "o/r", "ci.yml", "disable"]))
    assert calls == [("PUT", "/repos/o/r/actions/workflows/ci.yml/disable")]
