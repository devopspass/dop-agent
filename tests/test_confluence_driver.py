"""Confluence package driver (drivers/confluence): CLI, watcher script, skill rendering, link routing.

Feature: one `confluence-<instance>` skill per enabled instance (site/account/token file come from config, nothing
site-specific is built in), compact CLI over the Confluence Cloud REST API (v2 pages + v1 search), read-only watcher
script with a silent baseline, page links unfurl via `fetch`. Strict route-ordered urllib fake: an unexpected request
fails the test, and no test ever reaches a network.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import pytest

import watch_scripts
from integrations.registry import build_registry
from integrations.skills import ensure_skills

DRV = Path(__file__).resolve().parent.parent / "drivers" / "confluence"
TOKEN = "SECRET-TOKEN-123"
SITE = "https://x.example.net"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


c = _load("confluence_cli_t", DRV / "cli" / "cli.py")
w = _load("confluence_watch_t", DRV / "watch" / "confluence_watch.py")


class Resp:
    def __init__(self, data):
        self.b = json.dumps(data).encode()

    def read(self):
        return self.b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def fake_net(routes, reqs):
    queue = list(routes)

    def fake(req, timeout=0):
        path = req.full_url.split("example.net")[1]
        reqs.append((req.get_method(), path, json.loads(req.data) if req.data else None, req.headers))
        m, sub, resp = queue.pop(0)
        assert m == req.get_method() and sub in path, f"unexpected {req.get_method()} {path}, wanted {m} {sub}"
        if isinstance(resp, Exception):
            raise resp
        return Resp(resp)
    fake.queue = queue
    return fake


ENV = {"CONFLUENCE_INSTANCE": "main", "DOP_AGENT_CONFIG": "", "CF_TEST_TOKEN": TOKEN}


@pytest.fixture(autouse=True)
def _config(tmp_path, monkeypatch):
    """Instance `main` from a config.yaml whose token is an ${env:CF_TEST_TOKEN} ref (gitlab-driver convention)."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"integrations:\n  main:\n    driver: confluence\n    enabled: true\n    base_url: {SITE}\n"
                   "    auth: {type: private_token, token: '${env:CF_TEST_TOKEN}'}\n    options: {email: a@b.c}\n")
    ENV["DOP_AGENT_CONFIG"] = str(cfg)
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)


def run(argv, routes):
    reqs, buf = [], io.StringIO()
    net = fake_net(routes, reqs)
    with mock.patch.dict(os.environ, ENV), mock.patch("urllib.request.urlopen", net), redirect_stdout(buf):
        c.main(argv)
    assert not net.queue, f"unused routes {net.queue}"
    return buf.getvalue(), reqs


PAGE = {"id": "123", "title": "Runbook", "status": "current", "spaceId": "9", "parentId": "1",
        "version": {"number": 4, "createdAt": "2026-10-01T10:00:00Z", "authorId": "u1"},
        "body": {"storage": {"value": "<h1>Hi</h1><p>one <strong>two</strong></p><ul><li>a</li><li>b</li></ul>"}},
        "_links": {"webui": "/spaces/DOC/pages/123/Runbook"}}


def test_page_prints_header_and_plain_text_body_and_sends_basic_auth_without_leaking_the_token():
    out, reqs = run(["page", "123"], [("GET", "/wiki/api/v2/pages/123", PAGE)])
    assert "version: 4" in out and "one two" in out and "- a" in out and "<p>" not in out
    assert f"{SITE}/wiki/spaces/DOC/pages/123/Runbook" in out and TOKEN not in out
    assert reqs[0][3]["Authorization"].startswith("Basic ")


def test_page_id_must_be_numeric_so_paths_cannot_be_injected():
    with pytest.raises(SystemExit, match="numeric"):
        run(["page", "1/../../x"], [])


def test_search_one_line_per_hit_with_links():
    r = {"results": [{"title": "Runbook", "url": "/spaces/DOC/pages/123/R", "lastModified": "2026-10-02T00:00:00Z",
                      "content": {"id": "123", "type": "page"}, "resultGlobalContainer": {"title": "Docs"}}], "totalSize": 1}
    out, reqs = run(["search", "title ~ runbook", "--links"], [("GET", "/wiki/rest/api/search", r)])
    assert f"{SITE}/wiki/spaces/DOC/pages/123/R [page] Runbook | Docs | 2026-10-02" in out and "1 of 1" in out


def test_update_bumps_version_and_dry_run_sends_nothing():
    out, reqs = run(["update", "123", "--body", "new <b>text</b>", "--dry-run"], [("GET", "/pages/123", PAGE)])
    assert out.startswith("DRY-RUN PUT") and len(reqs) == 1 and '"number": 5' in out
    out, reqs = run(["update", "123", "--body", "- x", "--message", "m"], [("GET", "/pages/123", PAGE), ("PUT", "/pages/123", {"version": {"number": 5}})])
    put = reqs[1][2]
    assert put["version"] == {"number": 5, "message": "m"} and put["title"] == "Runbook" and "<li>x</li>" in put["body"]["value"]
    assert out.strip() == "OK 123 version 5"


def test_create_resolves_space_key_to_id_and_escapes_html_in_plain_bodies():
    routes = [("GET", "/wiki/api/v2/spaces", {"results": [{"id": "77", "key": "DOC"}]}),
              ("POST", "/wiki/api/v2/pages", {"id": "500", "_links": {"webui": "/spaces/DOC/pages/500/T"}})]
    out, reqs = run(["create", "--space", "DOC", "--title", "T", "--body", "<script>x</script>\n\n```\na]]>b\n```", "--parent", "1"], routes)
    d = reqs[1][2]
    assert d["spaceId"] == "77" and d["parentId"] == "1" and "&lt;script&gt;" in d["body"]["value"] and "<script>" not in d["body"]["value"]
    assert "]]]]><![CDATA[>" in d["body"]["value"] and out.startswith("OK 500")


def test_fetch_returns_card_json_for_both_link_kinds():
    routes = [("GET", "/wiki/api/v2/pages/123", PAGE), ("GET", "/wiki/api/v2/spaces/9", {"key": "DOC"}),
              ("GET", "/wiki/rest/api/user", {"displayName": "Al"})]
    out, _ = run(["fetch", "--kind", "page", "--ident", '{"id":"123"}'], routes)
    card = json.loads(out)
    assert card["title"] == "Runbook" and card["status"] == "current" and card["fields"] == {
        "space": "DOC", "version": "v4", "updated": "2026-10-01", "author": "Al"}
    with pytest.raises(SystemExit, match="no kind"):
        run(["fetch", "--kind", "x", "--ident", "{}"], [])


def test_test_command_reports_steps_and_stops_at_first_failure():
    out, _ = run(["test"], [("GET", "/wiki/rest/api/user/current", {"displayName": "Al"}),
                            ("GET", "/wiki/api/v2/spaces", {"results": [{"key": "DOC"}]})])
    r = json.loads(out)
    assert r["ok"] and [s["name"] for s in r["steps"]] == ["config", "auth", "spaces"] and "Al" in r["message"]
    bad = __import__("urllib.error").error.HTTPError(SITE, 401, "no", {}, io.BytesIO(b"denied"))
    out, _ = run(["test"], [("GET", "/wiki/rest/api/user/current", bad)])
    r = json.loads(out)
    assert not r["ok"] and r["steps"][-1]["name"] == "auth" and "401" in r["message"] and len(r["steps"]) == 2


def test_missing_instance_or_unresolvable_token_is_a_clear_error_not_a_traceback():
    with mock.patch.dict(os.environ, {**ENV, "CONFLUENCE_INSTANCE": "nope"}), pytest.raises(SystemExit, match="not found"):
        c.configure()
    with mock.patch.dict(os.environ, {k: v for k, v in ENV.items() if k != "CF_TEST_TOKEN"}), pytest.raises(SystemExit, match="auth.token"):
        os.environ.pop("CF_TEST_TOKEN", None)
        c.configure()


def test_token_is_resolved_from_the_config_ref_and_never_appears_in_argv_or_env_of_the_recipe():
    c.configure("main")
    assert c.CONF["token"] == TOKEN and c.CONF["email"] == "a@b.c" and c.CONF["base_url"] == SITE


# ---------- watcher ----------
@pytest.fixture
def wenv(tmp_path, monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("WATCH_STATE_DIR", str(tmp_path / "state"))
    return tmp_path


def wrun(argv, routes, monkeypatch):
    reqs = []
    net = fake_net(routes, reqs)
    monkeypatch.setattr("sys.argv", ["w", *argv])
    monkeypatch.setattr("urllib.request.urlopen", net)
    monkeypatch.setattr(w, "load_cli", lambda: _load("confluence_cli_w", DRV / "cli" / "cli.py"))
    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit) as e:
        w.main()
    assert not net.queue
    return e.value.code, buf.getvalue(), reqs


def item(i, v, title="T"):
    return {"id": i, "title": title, "version": {"number": v, "when": "2026-10-01T10:00:00Z", "by": {"displayName": "Al"}},
            "space": {"key": "DOC"}, "_links": {"webui": f"/spaces/DOC/pages/{i}/T"}}


def test_cql_watch_first_run_is_a_silent_baseline_then_reports_new_and_updated_only(wenv, monkeypatch):
    rc, out, reqs = wrun(["cql", "space = DOC"], [("GET", "/content/search", {"results": [item("1", 1)]})], monkeypatch)
    assert rc == 0 and out == "" and "order+by+lastmodified+desc" in reqs[0][1]
    rc, out, _ = wrun(["cql", "space = DOC"], [("GET", "/content/search", {"results": [item("1", 1)]})], monkeypatch)
    assert rc == 0 and out == ""
    rc, out, _ = wrun(["cql", "space = DOC"], [("GET", "/content/search", {"results": [item("2", 1, "Fresh"), item("1", 2)]})], monkeypatch)
    assert rc == 200 and "2 [new] Fresh" in out and "1 [updated]" in out and f"{SITE}/wiki/spaces/DOC/pages/2/T" in out
    rc, out, _ = wrun(["cql", "space = DOC", "--new-only"], [("GET", "/content/search", {"results": [item("2", 1), item("1", 3)]})], monkeypatch)
    assert rc == 0


def test_space_mode_builds_a_quoted_cql_and_rejects_cql_smuggled_in_the_key(wenv, monkeypatch):
    rc, _, reqs = wrun(["space", "DOC"], [("GET", "/content/search", {"results": []})], monkeypatch)
    assert rc == 0 and "space+%3D+%22DOC%22" in reqs[0][1]
    rc, _, _ = wrun(["space", 'DOC" OR type = page'], [], monkeypatch)
    assert rc == 1


def test_page_watch_reports_new_version_and_new_comments(wenv, monkeypatch):
    def routes(ver, cids):
        p = {**PAGE, "version": {**PAGE["version"], "number": ver}}
        cs = {"results": [{"id": i, "version": {"authorId": "u2"}, "body": {"storage": {"value": f"<p>c{i}</p>"}}} for i in cids]}
        return [("GET", "/pages/123", p), ("GET", "/footer-comments", cs)]
    assert wrun(["page", "123"], routes(4, ["c1"]), monkeypatch)[0] == 0
    assert wrun(["page", "123"], routes(4, ["c1"]), monkeypatch)[0] == 0
    rc, out, _ = wrun(["page", "123"], routes(5, ["c2", "c1"]), monkeypatch)
    assert rc == 200 and "v4 -> v5" in out and "new comment #c2" in out and "#c1" not in out


def test_transient_errors_are_silent_and_auth_errors_fail(wenv, monkeypatch):
    err = __import__("urllib.error").error.HTTPError
    assert wrun(["cql", "x"], [("GET", "/content/search", err(SITE, 503, "x", {}, io.BytesIO(b"")))], monkeypatch)[0] == 0
    assert wrun(["cql", "x"], [("GET", "/content/search", err(SITE, 401, "x", {}, io.BytesIO(b"")))], monkeypatch)[0] == 1


# ---------- registry / skill / links ----------
def _cfg():
    def inst(host, on=True):
        return {"driver": "confluence", "enabled": on, "base_url": host, "auth": {"type": "private_token", "token": "${env:CF_TEST_TOKEN}"},
                "options": {"email": "me@example.com"}}
    return {"one": inst("https://one.example.net"), "two": inst("https://two.example.net")}


def test_each_instance_gets_its_own_skill_pinned_to_its_site_and_nothing_is_left_unrendered(tmp_path):
    reg = build_registry(_cfg())
    assert sorted(ensure_skills(reg, tmp_path)) == ["confluence-one", "confluence-two"]
    text = (tmp_path / "confluence-two" / "SKILL.md").read_text()
    assert "https://two.example.net" in text and "one.example.net" not in text and "${{" not in text.replace("$${{", "")
    assert TOKEN not in text and "CF_TEST_TOKEN" not in text and "--instance two" in text


def test_page_links_route_to_the_instance_with_the_matching_site_and_unknown_sites_stay_plain():
    reg = build_registry(_cfg())
    inst, kind, ident = reg.resolve_url("https://two.example.net/wiki/spaces/DOC/pages/123/Run+book?x=1#h")
    assert inst.name == "two" and ident["id"] == "123"
    assert reg.resolve_url("https://three.example.net/wiki/spaces/DOC/pages/123") is None


def test_watcher_recipe_pins_instance_and_exports_site_to_the_script():
    reg = build_registry(_cfg())
    meta = watch_scripts.apply_watcher({"script": "confluence_watch.py", "watcher": "confluence-mentions", "integration": "two"}, reg)
    assert meta["args"][0] == "cql" and "mention = currentUser()" in meta["args"][1]
    env = watch_scripts.instance_env(meta, reg)
    assert env == {"CONFLUENCE_INSTANCE": "two"} or env["CONFLUENCE_INSTANCE"] == "two"
    assert TOKEN not in json.dumps(env) and "token" not in " ".join(env).lower()
    with pytest.raises(Exception):
        watch_scripts.instance_env({**meta, "integration": "gone"}, reg)


def test_driver_folder_is_generic_and_scripts_are_executable():
    assert os.access(DRV / "watch" / "confluence_watch.py", os.X_OK)
