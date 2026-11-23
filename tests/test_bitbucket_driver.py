"""Bitbucket package driver (Cloud + Server/Data Center): skill, link routing, CLI, draft-first writes, watcher.

Feature: Bitbucket is a package driver (CLI + skill + watcher recipes) with NO built-in host. Cloud and Server
instances coexist, each with its own skill/host/token. Everything here runs against an in-memory fake backend.
Guarantees: flavor/API root derive from base_url; PR URLs route to the instance whose host matches; both flavors
yield the same normalized CLI output; writes only print a DRAFT unless --yes; the token never appears in output
or argv; `api` cannot be pointed at another host; the watcher records a silent baseline, then reports each new
review request / comment / finished build exactly once.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

import watch_scripts
from integrations.registry import build_registry
from integrations.skills import ensure_skills

DRV = Path(__file__).resolve().parents[1] / "drivers" / "bitbucket"
sys.path.insert(0, str(DRV / "cli"))
import bbapi  # noqa: E402
import bbconfig  # noqa: E402

TOKEN = "s3cr3t-token-value"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cli = _load("bitbucket_cli_mod", DRV / "cli" / "cli.py")
bw = _load("bitbucket_watch_mod", DRV / "watch" / "bitbucket_watch.py")


class Resp:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status
        self.ok = status < 400
        self.text = data if isinstance(data, str) else json.dumps(data)
        self.content = self.text.encode()

    def json(self):
        if isinstance(self.data, str):
            raise ValueError
        return self.data


class FakeSession:
    """Routes by URL suffix; `routes` maps (METHOD, path-suffix) -> payload; records every call."""

    def __init__(self, routes):
        self.routes, self.calls, self.headers = routes, [], {}

    def _find(self, method, url):
        for (m, suffix), data in self.routes.items():
            if m == method and url.endswith(suffix):
                return Resp(data() if callable(data) else data)
        return Resp({"error": {"message": "nope"}}, 404)

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append((method, url, params, json))
        return self._find(method, url)

    def get(self, url, params=None, headers=None, timeout=None):
        return self.request("GET", url, params, None, headers, timeout)


def _cloud_pr(pid=7, state="OPEN"):
    return {"id": pid, "title": "Fix it", "state": state, "author": {"display_name": "Ann"},
            "source": {"branch": {"name": "feat"}, "commit": {"hash": "abcdef123456"}},
            "destination": {"branch": {"name": "main"}, "repository": {"full_name": "ws/repo"}},
            "links": {"html": {"href": f"https://bitbucket.org/ws/repo/pull-requests/{pid}"}},
            "participants": [{"role": "REVIEWER", "approved": True, "user": {"display_name": "Bob"}}], "updated_on": "2026-01-01T00:00:00Z"}


def _server_pr(pid=9, state="OPEN"):
    return {"id": pid, "version": 3, "title": "Server fix", "state": state, "author": {"user": {"displayName": "Ann"}},
            "fromRef": {"displayId": "feat", "latestCommit": "fedcba654321"},
            "toRef": {"displayId": "master", "repository": {"slug": "repo", "project": {"key": "PRJ"}}},
            "links": {"self": [{"href": f"https://bb.example.com/projects/PRJ/repos/repo/pull-requests/{pid}"}]},
            "reviewers": [{"user": {"displayName": "Bob"}, "status": "NEEDS_WORK"}], "updatedDate": 1767225600000}


def cloud_client(extra=None):
    routes = {
        ("GET", "/user"): {"uuid": "{u-1}", "display_name": "Me Myself", "nickname": "me"},
        ("GET", "/repositories/ws/repo/pullrequests/7"): _cloud_pr(),
        ("GET", "/pullrequests/7/comments"): {"values": [
            {"id": 1, "user": {"display_name": "Bob"}, "content": {"raw": "general note"}, "created_on": "2026-01-01T01:00:00Z"},
            {"id": 2, "user": {"display_name": "Bob"}, "content": {"raw": "inline"}, "created_on": "2026-01-01T02:00:00Z",
             "inline": {"path": "a.py", "to": 4}},
            {"id": 3, "user": {"display_name": "Me Myself"}, "content": {"raw": "mine"}, "created_on": "2026-01-01T03:00:00Z",
             "parent": {"id": 2}}]},
        ("GET", "/pullrequests/7/statuses"): {"values": [{"name": "build", "state": "FAILED", "url": "http://ci/1"}]},
        ("GET", "/repositories/ws/repo/pullrequests"): {"values": [_cloud_pr()]},
        ("GET", "/repositories/ws/repo/pipelines/"): {"values": [
            {"uuid": "{p-1}", "build_number": 5, "state": {"name": "COMPLETED", "result": {"name": "FAILED"}},
             "target": {"ref_name": "main", "commit": {"hash": "cafebabe0000"}}, "created_on": "2026-01-01T00:00:00Z",
             "duration_in_seconds": 61, "creator": {"display_name": "Ann"}}]},
        ("GET", "/repositories/ws"): {"values": [{"full_name": "ws/repo"}]},
    }
    routes.update(extra or {})
    c = bbapi.Client("https://bitbucket.org", "https://api.bitbucket.org/2.0", "cloud", TOKEN, session=FakeSession(routes))
    c.workspace = "ws"
    return c


def server_client(extra=None):
    routes = {
        ("GET", "/plugins/servlet/applinks/whoami"): "me",
        ("GET", "/users/me"): {"slug": "me", "displayName": "Me Myself"},
        ("GET", "/projects/PRJ/repos/repo/pull-requests/9"): _server_pr(),
        ("GET", "/pull-requests/9/activities"): {"isLastPage": True, "values": [
            {"action": "COMMENTED", "commentAnchor": {"path": "b.py", "line": 2},
             "comment": {"id": 11, "text": "needs work", "author": {"displayName": "Bob"}, "createdDate": 1767225600000,
                         "threadResolved": False, "comments": [{"id": 12, "text": "ok", "author": {"displayName": "Ann"},
                                                                 "createdDate": 1767229200000}]}},
            {"action": "APPROVED"}]},
        ("GET", "/rest/build-status/1.0/commits/fedcba654321"): {"isLastPage": True, "values": [{"name": "ci", "state": "SUCCESSFUL", "url": "u"}]},
        ("GET", "/dashboard/pull-requests"): {"isLastPage": True, "values": [_server_pr()]},
        ("GET", "/branches/default"): {"displayId": "master"},
        ("GET", "/projects/PRJ/repos/repo/commits"): {"isLastPage": True, "values": [{"id": "fedcba654321", "message": "m", "author": {"name": "a"}}]},
    }
    routes.update(extra or {})
    return bbapi.Client("https://bb.example.com", "https://bb.example.com/rest/api/1.0", "server", TOKEN, session=FakeSession(routes))


def run(client, argv, capsys):
    rc = cli.main(argv, client=client)
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


# ------------------------------------------------------------------ config, skill, links
def _cfg():
    def inst(host, **opts):
        return {"driver": "bitbucket", "enabled": True, "base_url": host, "auth": {"type": "private_token", "token": "x"}, "options": opts}
    return {"cloud": inst("https://bitbucket.org", workspace="ws"), "dc": inst("https://bb.example.com", title="Corp DC")}


def test_flavor_and_api_root_derive_from_base_url_unless_overridden():
    assert bbconfig.flavor("https://bitbucket.org", {}) == "cloud"
    assert bbconfig.api_url("https://bitbucket.org", {}) == "https://api.bitbucket.org/2.0"
    assert bbconfig.flavor("https://bb.example.com", {}) == "server"
    assert bbconfig.api_url("https://bb.example.com/", {}) == "https://bb.example.com/rest/api/1.0"
    assert bbconfig.flavor("https://git.example.com", {"flavor": "cloud"}) == "cloud"
    assert bbconfig.api_url("https://bb.example.com", {"api_url": "https://x.example.com/api/"}) == "https://x.example.com/api"


def test_each_instance_gets_its_own_skill_and_links_route_by_host(tmp_path):
    reg = build_registry(_cfg())
    assert ensure_skills(reg, tmp_path) == ["bitbucket-cloud", "bitbucket-dc"]
    text = (tmp_path / "bitbucket-dc" / "SKILL.md").read_text()
    assert "https://bb.example.com" in text and "--instance dc" in text and "Corp DC" in text
    assert "${{" not in text.replace("$${{", "")
    desc = text.split("description:")[1].split("\n")[0]
    assert "--instance" not in desc and "cli.py" not in desc
    it, kind, ident = reg.resolve_url("https://bitbucket.org/ws/repo/pull-requests/12/diff")
    assert (it.name, kind, ident) == ("cloud", "pull_request", {"repo": "ws/repo", "id": "12"})
    it, kind, ident = reg.resolve_url("https://bb.example.com/projects/PRJ/repos/repo/pull-requests/3/overview")
    assert (it.name, kind, ident) == ("dc", "pull_request_server", {"project": "PRJ", "slug": "repo", "id": "3"})
    it, kind, ident = reg.resolve_url("https://bitbucket.org/ws/repo/pipelines/results/44")
    assert (kind, ident["id"]) == ("pipeline", "44")
    assert reg.resolve_url("https://elsewhere.example.com/ws/repo/pull-requests/1") is None


def test_watcher_recipe_prefixes_mode_and_pins_instance_env():
    reg = build_registry({"dc": _cfg()["dc"]})
    meta = watch_scripts.apply_watcher({"script": "bitbucket_watch.py", "watcher": "bitbucket-pr", "args": ["--ref", "PRJ/repo#9"]}, reg)
    assert meta["integration"] == "dc" and meta["args"][:3] == ["pr", "--ref", "PRJ/repo#9"]


def test_bad_option_values_do_not_reach_a_rendered_skill(tmp_path):
    cfg = _cfg()
    cfg["dc"]["options"]["title"] = 'x"; rm -rf `y`'
    reg = build_registry(cfg)
    ensure_skills(reg, tmp_path)
    assert "rm -rf" not in "".join(p.read_text() for p in tmp_path.rglob("SKILL.md"))


# ------------------------------------------------------------------ CLI, both flavors
def test_pr_status_is_normalized_for_cloud_and_server(capsys):
    rc, out, _ = run(cloud_client(), ["pr-status", "ws/repo#7"], capsys)
    assert rc == 0 and "ws/repo#7 🟢open @Ann feat -> main" in out and "Bob=approved" in out and "checks: failed" in out
    assert "FAILED build" in out and "1 inline thread" in out
    rc, out, _ = run(server_client(), ["pr-status", "PRJ/repo#9"], capsys)
    assert rc == 0 and "PRJ/repo#9 🟢open @Ann feat -> master" in out and "Bob=changes_requested" in out and "checks: success" in out


def test_pr_ref_accepts_urls_for_both_flavors():
    assert cli.parse_ref("https://bitbucket.org/ws/repo/pull-requests/12/diff") == ("ws/repo", "12")
    assert cli.parse_ref("https://bb.example.com/projects/PRJ/repos/repo/pull-requests/3") == ("PRJ/repo", "3")
    assert cli.parse_ref("ws/repo#5") == ("ws/repo", "5")
    with pytest.raises(bbapi.BbError):
        cli.parse_ref("nonsense")


def test_threads_show_replies_and_server_open_state(capsys):
    _, out, _ = run(server_client(), ["pr-threads", "PRJ/repo#9", "--unresolved"], capsys)
    assert "[11] b.py:2 open @Bob: needs work" in out and "↳ [12] @Ann: ok" in out
    _, out, _ = run(cloud_client(), ["pr-threads", "ws/repo#7"], capsys)
    assert "[2] a.py:4" in out and "↳ [3] @Me Myself: mine" in out


def test_pipeline_commands_are_cloud_only_with_clear_hint(capsys):
    _, out, _ = run(cloud_client(), ["pipelines", "ws/repo"], capsys)
    assert "#5 ❌failed main cafebabe" in out
    rc, _, err = run(server_client(), ["pipelines", "PRJ/repo"], capsys)
    assert rc == 1 and "only on Bitbucket Cloud" in err and "build statuses" in err


def test_pipeline_number_falls_back_to_listing_when_direct_lookup_404s():
    p = cloud_client().pipeline("ws/repo", "5")
    assert p["uuid"] == "{p-1}" and p["status"] == "failed"


# ------------------------------------------------------------------ writes are draft-first
def test_writes_without_yes_print_a_draft_and_send_nothing(capsys):
    c = cloud_client()
    for argv in (["pr-comment", "ws/repo#7", "--body", "LGTM"], ["pr-approve", "ws/repo#7"], ["pr-merge", "ws/repo#7"],
                 ["pr-create", "ws/repo", "--source", "feat", "--dest", "main", "--title", "T"],
                 ["api", "POST", "/repositories/ws/repo/pullrequests/7/decline", "--data", "{}"]):
        rc, out, _ = run(c, argv, capsys)
        assert rc == 0 and out.startswith("DRAFT"), argv
    assert all(m == "GET" for m, *_ in c.session.calls)


def test_confirmed_writes_use_flavor_specific_requests(capsys):
    c = cloud_client({("POST", "/pullrequests/7/comments"): {"id": 99}, ("POST", "/pullrequests/7/approve"): {}})
    _, out, _ = run(c, ["pr-comment", "ws/repo#7", "--body", "LGTM", "--reply", "2", "--yes"], capsys)
    assert "comment id 99" in out
    post = [x for x in c.session.calls if x[0] == "POST"][0]
    assert post[3] == {"content": {"raw": "LGTM"}, "parent": {"id": 2}}
    s = server_client({("POST", "/pull-requests/9/merge"): {"state": "MERGED"}})
    _, out, _ = run(s, ["pr-merge", "PRJ/repo#9", "--yes"], capsys)
    assert "merged PRJ/repo#9" in out
    merge = [x for x in s.session.calls if x[0] == "POST"][0]
    assert merge[2] == {"version": 3}
    rc, _, err = run(s, ["pr-merge", "PRJ/repo#9", "--strategy", "squash", "--yes"], capsys)
    assert rc == 1 and "Cloud-only" in err


def test_api_refuses_absolute_urls_so_the_token_cannot_reach_another_host(capsys):
    c = cloud_client()
    rc, _, err = run(c, ["api", "GET", "https://evil.example.com/steal"], capsys)
    assert rc == 1 and "relative REST path" in err and c.session.calls == []


def test_secrets_are_masked_in_errors_and_logs():
    assert "hunter2" not in cli.redact("password=hunter2 token: hunter2")
    lines = cli.clean_log("\x1b[31mbuild failed\x1b[0m token=abc123\nok")
    assert lines == ["build failed token=***", "ok"]


def test_fetch_returns_card_json_for_pr_and_server_pr(capsys):
    _, out, _ = run(cloud_client(), ["fetch", "--kind", "pull_request", "--ident", '{"repo":"ws/repo","id":"7"}'], capsys)
    card = json.loads(out)
    assert card["status"] == "open" and card["fields"]["approvals"] == "1/1" and card["fields"]["checks"] == "failed"
    _, out, _ = run(server_client(), ["fetch", "--kind", "pull_request_server", "--ident", '{"project":"PRJ","slug":"repo","id":"9"}'], capsys)
    assert json.loads(out)["fields"]["branch"] == "feat -> master"


def test_test_command_reports_steps_without_token(monkeypatch, capsys):
    monkeypatch.setattr(bbconfig, "load_instance", lambda name=None: ("dc", "https://bb.example.com", TOKEN, "", {}))
    fake = FakeSession({("GET", "/application-properties"): {"version": "9"}, ("GET", "/plugins/servlet/applinks/whoami"): "me",
                        ("GET", "/users/me"): {"slug": "me", "displayName": "Me Myself"}})
    monkeypatch.setattr(bbapi.requests, "Session", lambda: fake)
    rc, out, _ = run(None, ["--instance", "dc", "test"], capsys)
    res = json.loads(out)
    assert rc == 0 and res["ok"] and [s["step"] for s in res["steps"]] == ["requests", "config", "reach", "auth"]
    assert TOKEN not in out


def test_test_command_explains_rejected_token(monkeypatch, capsys):
    monkeypatch.setattr(bbconfig, "load_instance", lambda name=None: ("c", "https://bitbucket.org", TOKEN, "", {}))
    fake = FakeSession({})
    fake.routes[("GET", "/user")] = {"error": {"message": "x"}}
    monkeypatch.setattr(fake, "get", lambda url, **kw: Resp({}, 401))
    monkeypatch.setattr(bbapi.requests, "Session", lambda: fake)
    _, out, _ = run(None, ["test"], capsys)
    res = json.loads(out)
    assert not res["ok"] and "auth.username" in res["message"] and TOKEN not in out


def test_missing_instance_fails_instead_of_using_another(tmp_path, monkeypatch):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("integrations:\n  a: {driver: bitbucket, enabled: true, base_url: https://bitbucket.org, auth: {token: x}}\n"
                   "  b: {driver: bitbucket, enabled: false, base_url: https://bitbucket.org, auth: {token: x}}\n")
    monkeypatch.setenv("DOP_AGENT_CONFIG", str(cfg))
    monkeypatch.delenv("BITBUCKET_INSTANCE", raising=False)
    with pytest.raises(bbconfig.InstanceError, match="not found/enabled"):
        bbconfig.load_instance("b")
    assert bbconfig.load_instance()[0] == "a"


# ------------------------------------------------------------------ watcher
def _watch(monkeypatch, tmp_path, client, argv):
    monkeypatch.setenv("WATCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(bw, "_client", lambda inst: ("t", client, bbapi))
    return bw.main(argv)


def test_review_requested_baselines_then_reports_only_new_prs(monkeypatch, tmp_path, capsys):
    prs = [_server_pr(9)]
    c = server_client({("GET", "/dashboard/pull-requests"): lambda: {"isLastPage": True, "values": list(prs)}})
    assert _watch(monkeypatch, tmp_path, c, ["review-requested"]) == 0
    assert capsys.readouterr().out == ""
    assert _watch(monkeypatch, tmp_path, c, ["review-requested"]) == 0
    prs.append(_server_pr(10))
    assert _watch(monkeypatch, tmp_path, c, ["review-requested"]) == 200
    out = capsys.readouterr().out
    assert "PRJ/repo#10" in out and "#9 " not in out
    assert _watch(monkeypatch, tmp_path, c, ["review-requested"]) == 0


def test_pr_watch_reports_new_foreign_comment_and_merge_once(monkeypatch, tmp_path, capsys):
    comments = [{"id": 1, "user": {"display_name": "Bob"}, "content": {"raw": "old"}, "created_on": "2026-01-01T01:00:00Z"}]
    pr = {"v": _cloud_pr()}
    c = cloud_client({("GET", "/pullrequests/7/comments"): lambda: {"values": list(comments)},
                      ("GET", "/repositories/ws/repo/pullrequests/7"): lambda: pr["v"],
                      ("GET", "/pullrequests/7/statuses"): {"values": []}})
    assert _watch(monkeypatch, tmp_path, c, ["pr", "--ref", "ws/repo#7"]) == 0
    comments += [{"id": 2, "user": {"display_name": "Bob"}, "content": {"raw": "please fix token=abc"}, "created_on": "2026-01-02T00:00:00Z"},
                 {"id": 3, "user": {"display_name": "Me Myself"}, "content": {"raw": "mine"}, "created_on": "2026-01-02T01:00:00Z"}]
    pr["v"] = _cloud_pr(state="MERGED")
    assert _watch(monkeypatch, tmp_path, c, ["pr", "--ref", "ws/repo#7"]) == 200
    out = capsys.readouterr().out
    assert "is merged" in out and "by Bob: please fix token=***" in out and "mine" not in out and "can be removed" in out
    assert _watch(monkeypatch, tmp_path, c, ["pr", "--ref", "ws/repo#7"]) == 0


def test_pipeline_watch_fires_once_when_cloud_build_finishes(monkeypatch, tmp_path, capsys):
    state = {"v": "IN_PROGRESS"}

    def listing():
        st = {"name": "COMPLETED", "result": {"name": "SUCCESSFUL"}} if state["v"] == "DONE" else {"name": "IN_PROGRESS"}
        return {"values": [{"uuid": "{p-2}", "build_number": 6, "state": st, "target": {"ref_name": "feat", "commit": {"hash": "1" * 12}}}]}
    c = cloud_client({("GET", "/repositories/ws/repo/pipelines/"): listing})
    args = ["pipeline", "--ref", "ws/repo@feat"]
    assert _watch(monkeypatch, tmp_path, c, args) == 0
    state["v"] = "DONE"
    assert _watch(monkeypatch, tmp_path, c, args) == 200
    out = capsys.readouterr().out
    assert "pipeline #6 on feat" in out and "success" in out
    assert _watch(monkeypatch, tmp_path, c, args) == 0


def test_watcher_treats_rate_limits_as_transient_and_other_errors_as_failures(monkeypatch, tmp_path, capsys):
    c = server_client({("GET", "/dashboard/pull-requests"): {"errors": [{"message": "slow down"}]}})
    c.session.routes[("GET", "/dashboard/pull-requests")] = None
    c.session._find = lambda m, u: Resp({"errors": [{"message": "slow down"}]}, 429) if "dashboard" in u else FakeSession._find(c.session, m, u)
    assert _watch(monkeypatch, tmp_path, c, ["review-requested"]) == 0
    c.session._find = lambda m, u: Resp({"errors": [{"message": "denied"}]}, 401) if "dashboard" in u else FakeSession._find(c.session, m, u)
    assert _watch(monkeypatch, tmp_path, c, ["review-requested"]) == 1
    assert "denied" in capsys.readouterr().err


def test_paging_links_to_a_foreign_host_are_refused_and_option_like_ids_rejected(capsys):
    c = cloud_client({("GET", "/repositories/ws/repo/pipelines/"): {"values": [], "next": "https://evil.example.com/page2"}})
    with pytest.raises(bbapi.BbError, match="foreign host"):
        c.pages("/repositories/ws/repo/pipelines/", {}, limit=500)
    for bad in ("--help", "a/../b"):
        with pytest.raises(bbapi.BbError):
            cli.safe_id(bad, "ref")


def test_pr_watch_works_with_tokens_that_have_no_user(monkeypatch, tmp_path):
    c = cloud_client({("GET", "/user"): {"error": {"message": "no user"}}})
    c.session._find = (lambda orig: lambda m, u: Resp({"error": {"message": "x"}}, 403) if u.endswith("/user") else orig(m, u))(c.session._find)
    assert _watch(monkeypatch, tmp_path, c, ["pr", "--ref", "ws/repo#7"]) == 0
