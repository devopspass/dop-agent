"""FURLS (link-hover status cards) on the integration registry.

/api/unfurl resolves a URL of ANY configured instance (incl. a second gitlab base_url) through
registry.fetch_url; /api/integrations/ui exposes the secret-free projection the browser compiles
its link regexes / url templates from. Zero config => default jira+gitlab+teams instances.
"""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import message_store  # noqa: E402
import web_server  # noqa: E402
from drivers.base import NotFound, Resource  # noqa: E402
from integrations import runtime  # noqa: E402
from integrations.registry import build_registry  # noqa: E402
from integration_defaults import default_test_integrations as default_integrations  # noqa: E402


def _client(tmp_path, monkeypatch):
    monkeypatch.setattr(message_store, "DB_PATH", tmp_path / "bot_store.db", raising=False)
    monkeypatch.setattr(web_server, "WEB_UI_PASSWORD", "pw")
    monkeypatch.setattr(web_server, "_unfurl_cache", {})
    c = TestClient(web_server.app)
    assert c.post("/api/login", json={"password": "pw"}).status_code == 200
    return c


def _two_gitlabs():
    ints = default_integrations()
    ints["gitlab2"] = {"driver": "gitlab", "base_url": "https://gl2.example.com",
                       "auth": {"type": "private_token", "token": "t"}}
    return ints


@pytest.fixture
def reg(monkeypatch):
    r = build_registry(_two_gitlabs())
    monkeypatch.setattr(runtime, "_registry", r)
    monkeypatch.setattr(runtime, "_hooked", True)
    yield r
    runtime.reset()


def _fake_fetch(calls):
    def fetch(it, kind, ident):
        calls.append((it.name, kind, ident))
        return Resource(integration=it.name, driver=it.driver_id, kind=kind, ident=ident, title="T",
                        url="http://u", status="opened", status_emoji="🟢", fields={"state": "opened"},
                        legacy={"type": "gitlab_mr", "iid": ident.get("id")})
    return fetch


def test_default_integrations_cover_historical_hosts():
    d = default_integrations()
    assert d["gitlab"]["base_url"] == "https://gitlab.example.com"
    assert d["jira"]["base_url"] == "https://jira.example.net"
    assert "teams" not in d  # package drivers are opt-in
    r = build_registry(d)
    assert r.resolve_url("https://jira.example.net/browse/PRJ-1")[1:] == ("ticket", {"key": "PRJ-1"})
    assert r.resolve_url("https://gitlab.example.com/a/b/-/pipelines/9")[1] == "pipeline"


def test_unfurl_resolves_url_of_second_gitlab_instance(tmp_path, monkeypatch, reg):
    """A URL under any configured instance's base_url furls; the payload keeps the generic card keys."""
    calls = []
    monkeypatch.setattr(reg, "fetch", _fake_fetch(calls))
    c = _client(tmp_path, monkeypatch)
    body = c.get("/api/unfurl", params={"url": "https://gl2.example.com/g/p/-/merge_requests/7"}).json()
    assert calls == [("gitlab2", "merge_request", {"project": "g/p", "id": "7"})]
    assert body["type"] == "gitlab_mr" and body["iid"] == "7"
    assert body["fields"] == {"state": "opened"} and body["status_emoji"] == "🟢"


def test_unfurl_unknown_url_and_fetch_failure_give_null_type(tmp_path, monkeypatch, reg):
    def boom(*a):
        raise NotFound("404")
    monkeypatch.setattr(reg, "fetch", boom)
    c = _client(tmp_path, monkeypatch)
    assert c.get("/api/unfurl", params={"url": "https://example.com/x"}).json() == {"type": None}
    assert c.get("/api/unfurl", params={"url": "https://gl2.example.com/g/p/-/jobs/1"}).json() == {"type": None}


def test_unfurl_caches_and_takes_ttl_from_config(tmp_path, monkeypatch, reg):
    calls = []
    monkeypatch.setattr(reg, "fetch", _fake_fetch(calls))
    c = _client(tmp_path, monkeypatch)
    url = "https://gl2.example.com/g/p/-/merge_requests/7"
    c.get("/api/unfurl", params={"url": url})
    c.get("/api/unfurl", params={"url": url})
    assert len(calls) == 1
    monkeypatch.setattr(web_server.furl_api, "ttl", lambda: 0.0)
    c.get("/api/unfurl", params={"url": url})
    assert len(calls) == 2


def test_unfurl_and_ui_projection_require_auth(tmp_path, monkeypatch):
    c = TestClient(web_server.app)
    assert c.get("/api/unfurl", params={"url": "x"}).status_code == 401
    assert c.get("/api/integrations/ui").status_code == 401


def test_integrations_ui_endpoint_returns_secret_free_projection(tmp_path, monkeypatch, reg):
    c = _client(tmp_path, monkeypatch)
    body = c.get("/api/integrations/ui").json()
    assert body == reg.ui_projection()
    assert {i["name"] for i in body["integrations"]} == {"jira", "gitlab", "gitlab2"}
    assert "token" not in c.get("/api/integrations/ui").text


def test_watch_preview_has_no_card_without_a_registry_watch_type(monkeypatch, reg):
    calls = []
    monkeypatch.setattr(reg, "fetch", _fake_fetch(calls))
    for w in ({"id": 1, "type": "mr", "key": "g/p#3"}, {"id": 2, "type": "time", "key": "in:5m"},
              {"id": 3, "type": "script", "key": "gitlab-mr"}):
        assert web_server._fetch_watch_preview_sync(w) == {"type": None}
    assert calls == []
