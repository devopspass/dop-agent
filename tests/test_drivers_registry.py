"""Drivers catalog + integrations registry.

Covers: manifest validation errors, {base_url} substitution and Python->JS regex conversion (checked
with node), URL/watch-key resolution for the real shapes used by watchers.py/web_server.py, multiple
instances of one driver, secret-free ui_projection, and fetch/poll with mocked clients.
"""
from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

from drivers.base import ManifestError, UnsupportedOperation
from drivers.manifest import discover_manifests, parse_manifest, substitute_base_url, to_js_regex
from integrations import build_registry

TOKEN = "glpat-SUPER-SECRET-123"
JTOKEN = "jira-SECRET-456"
JTOKEN_FILE = Path(tempfile.mkdtemp()) / "jira-token"
JTOKEN_FILE.write_text(JTOKEN + "\n")


def cfg(**over):
    c = {
        "gitlab": {"driver": "gitlab", "base_url": "https://gitlab.example.com",
                   "auth": {"type": "private_token", "token": TOKEN}},
        "jira": {"driver": "jira", "base_url": "https://jira.example.net", "auth": {"type": "token_file"},
                 "options": {"email": "a@b.c", "token_file": str(JTOKEN_FILE)}},
    }
    c.update(over)
    return c


@pytest.fixture
def reg():
    return build_registry(cfg(), extra_driver_dirs=[])


def base_manifest(**kw):
    m = {"id": "x", "implementation": "m:C", "settings": {"base_url": {"required": True}},
         "kinds": {"k": {"url_match": "^{base_url}/(?P<id>\\d+)$", "url_template": "{base_url}/{id}"}}}
    m.update(kw)
    return m


# ---- manifest validation ----

@pytest.mark.parametrize("mutate,msg", [
    (lambda m: m.update(id="Bad Id"), "'id'"),
    (lambda m: m.update(implementation="nocolon"), "implementation"),
    (lambda m: m["kinds"]["k"].update(url_match="^{base_url}/\\d+$"), "named group"),
    (lambda m: m["kinds"]["k"].update(url_template="{base_url}/{nope}"), "not captured"),
    (lambda m: m["kinds"]["k"].update(url_match="^{base_url}/(?P<id>(\\d+$"), "invalid regex"),
    (lambda m: m["kinds"]["k"].update(status_set="missing"), "status_set"),
    (lambda m: m.update(options={"o": {"type": "weird"}}), "type"),
    (lambda m: m.update(options={"o": {"type": "int", "default": "abc"}}), "default"),
    (lambda m: m.update(kinds={}), "at least one kind"),
    (lambda m: m["kinds"]["k"].update(watch={"type": "t", "key_match": "^(?P<a>.+$", }), "invalid regex"),
    (lambda m: m["kinds"]["k"].update(watch={"type": "t", "key_match": "^.+$", "poll_interval_s": 0}), "poll_interval_s"),
    (lambda m: m.update(settings={}), "settings.base_url"),
])
def test_manifest_validation_rejects_bad_manifests(mutate, msg):
    m = base_manifest()
    mutate(m)
    with pytest.raises(ManifestError, match=msg):
        parse_manifest(m)


def test_manifest_duplicate_watch_type_rejected():
    m = base_manifest()
    m["kinds"]["k2"] = {"watch": {"type": "t", "key_match": "^.+$"}}
    m["kinds"]["k"]["watch"] = {"type": "t", "key_match": "^.+$"}
    with pytest.raises(ManifestError, match="already used"):
        parse_manifest(m)


def test_builtin_manifests_valid_and_discovered():
    errs = []
    found = discover_manifests([], errs)
    assert errs == [] and {"gitlab", "jira", "ms_teams"} <= set(found)
    assert found["gitlab"].auth_types["private_token"].fields["token"].secret is True


def test_plugin_dir_invalid_manifest_reported_not_raised(tmp_path):
    d = tmp_path / "bad"
    d.mkdir()
    (d / "driver.yaml").write_text("id: bad\n")
    errs = []
    found = discover_manifests([tmp_path], errs)
    assert "bad" not in found and len(errs) == 1


# ---- regexes ----

def test_base_url_substitution_escapes_and_trailing_slash():
    out = substitute_base_url("^{base_url}/x$", "https://gitlab.example.com/")
    assert out == r"^https:\/\/gitlab\.example\.com/x$"


def test_js_regex_conversion_named_groups():
    assert to_js_regex(r"^(?P<project>.+)#(?P<id>\d+)$") == r"^(?<project>.+)#(?<id>\d+)$"
    with pytest.raises(ManifestError):
        to_js_regex("(?i)abc")


@pytest.mark.skipif(shutil.which("node") is None, reason="node missing")
def test_converted_regexes_work_in_node(reg):
    proj = reg.ui_projection()
    mr = next(i for i in proj["integrations"] if i["name"] == "gitlab")["kinds"]["merge_request"]["url_match"]
    script = ("const r=new RegExp(process.argv[1]);const m=r.exec(process.argv[2]);"
              "console.log(JSON.stringify(m&&m.groups))")
    out = subprocess.run(["node", "-e", script, mr, "https://gitlab.example.com/g/sub/p/-/merge_requests/42"],
                         capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == {"project": "g/sub/p", "id": "42"}


# ---- resolution ----

@pytest.mark.parametrize("url,kind,ident", [
    ("https://gitlab.example.com/lds/a/b/-/merge_requests/12", "merge_request", {"project": "lds/a/b", "id": "12"}),
    ("https://gitlab.example.com/lds/a/-/merge_requests/12/", "merge_request", {"project": "lds/a", "id": "12"}),
    ("https://gitlab.example.com/lds/a/-/merge_requests/12#note_5", "merge_request", {"project": "lds/a", "id": "12"}),
    ("https://gitlab.example.com/lds/a/-/pipelines/99", "pipeline", {"project": "lds/a", "id": "99"}),
    ("https://gitlab.example.com/lds/a/-/jobs/7", "job", {"project": "lds/a", "id": "7"}),
    ("https://jira.example.net/browse/PRJ-123", "ticket", {"key": "PRJ-123"}),
])
def test_resolve_url_real_shapes(reg, url, kind, ident):
    it, k, i = reg.resolve_url(url)
    assert (k, i) == (kind, ident)


@pytest.mark.parametrize("url", ["https://example.com/x", "https://gitlab.example.com/a/-/issues/1",
                                 "https://gitlab.example.com.evil.com/a/-/jobs/1", "", "https://jira.example.net/browse/proj-1"])
def test_resolve_url_non_matching(reg, url):
    assert reg.resolve_url(url) is None


@pytest.mark.parametrize("type_,key", [("mr", "lds/a/b#123"), ("pipeline", "lds/a#456"), ("pipeline", "lds/a@feature/PRJ-1-x"),
                                       ("job", "lds/a#9"), ("mr", "lds/a!12"), ("ticket", "PRJ-1"), ("bogus", "x#1"), ("mr", "")])
def test_package_drivers_have_no_native_watch_types(reg, type_, key):
    assert reg.resolve_watch(type_, key) is None
    assert reg.link_for(type_, key) is None
    assert reg.watch_types() == set() and reg.watch_type_meta(type_) is None


def test_two_gitlab_instances_different_base_urls():
    c = cfg(gl2={"driver": "gitlab", "base_url": "https://gitlab.other.io",
                 "auth": {"type": "private_token", "token": "t2"}})
    r = build_registry(c, extra_driver_dirs=[])
    assert r.resolve_url("https://gitlab.other.io/a/-/jobs/3")[0].name == "gl2"
    assert r.resolve_url("https://gitlab.example.com/a/-/jobs/3")[0].name == "gitlab"
    assert r.fetch_url("https://nowhere/x") is None


def test_instance_overrides_and_disabled():
    c = cfg()
    c["gitlab"]["kinds"] = {"job": {"enabled": False}}
    r = build_registry(c, extra_driver_dirs=[])
    assert r.resolve_url("https://gitlab.example.com/a/-/jobs/3") is None
    assert r.resolve_url("https://gitlab.example.com/a/-/pipelines/3") is not None
    c["jira"]["enabled"] = False
    assert build_registry(c, extra_driver_dirs=[]).resolve_url("https://jira.example.net/browse/PRJ-1") is None


def test_invalid_instances_reported_and_excluded():
    c = cfg(a={"driver": "nope"}, b={"driver": "gitlab", "base_url": "https://g", "auth": {"type": "private_token"}},
            c={"driver": "gitlab", "base_url": "https://g", "auth": {"type": "private_token", "token": "x"},
               "options": {"outage_notice_s": "abc", "zzz": 1}, "kinds": {"nokind": {}}})
    r = build_registry(c, extra_driver_dirs=[])
    text = "\n".join(r.errors)
    assert "unknown driver" in text and "auth.token is required" in text
    assert "outage_notice_s" in text and "unknown option 'zzz'" in text and "unknown kind 'nokind'" in text
    assert not r.get("b").valid and "gitlab" in {i.name for i in r._active()}
    assert not r.test_connection("b")["ok"]


# ---- UI projection / secrets ----

def test_ui_projection_and_summaries_never_contain_secrets(reg):
    blobs = [json.dumps(x) for x in (reg.ui_projection(), reg.catalog(), reg.integrations_summary())]
    blobs += [repr(i) for i in reg.integrations.values()]
    for b in blobs:
        assert TOKEN not in b and JTOKEN not in b and "a@b.c" not in b
    s = reg.integrations_summary()
    assert next(i for i in s if i["name"] == "gitlab")["configured_secrets"] == ["token"]
    proj = reg.ui_projection()
    assert proj["integrations"][0]["kinds"] and proj["watch_types"] == {}


# ---- fetch via CLI ----

def test_package_fetch_normalizes_cli_json_and_has_no_poll(reg, monkeypatch):
    d = reg.get("gitlab").driver()
    out = {"title": "T", "url": "u", "status": "opened", "status_emoji": "🟢", "fields": {"author": "bob"}}
    monkeypatch.setattr(d, "run_cli", lambda *a, **kw: subprocess.CompletedProcess(a, 0, json.dumps(out), ""))
    r = reg.fetch("gitlab", "merge_request", {"project": "g/p", "id": "1"})
    assert (r.title, r.status, r.status_emoji, r.fields["author"]) == ("T", "opened", "🟢", "bob")
    monkeypatch.setattr(d, "run_cli", lambda *a, **kw: subprocess.CompletedProcess(a, 1, "", "boom"))
    with pytest.raises(UnsupportedOperation, match="boom"):
        reg.fetch("gitlab", "job", {"project": "g/p", "id": "3"})
    with pytest.raises(UnsupportedOperation, match="script watches"):
        reg.poll("gitlab", "merge_request", {"project": "g/p", "id": "1"}, {})


def test_example_config_integrations_load_cleanly():
    from pathlib import Path
    c = yaml.safe_load((Path(__file__).resolve().parent.parent / "config.example.yaml").read_text())["integrations"]
    c = copy.deepcopy(c)
    c["jira-main"]["options"].update(email="u@example.com", token_file="/tmp/t")
    c["gitlab-main"]["auth"]["token"] = "t"
    assert build_registry(c, extra_driver_dirs=[]).errors == []
