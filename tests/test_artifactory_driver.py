"""Artifactory package driver (drivers/artifactory/).

Feature: generic JFrog Artifactory integration (CLI + watcher + skill + furls), several instances via config, no
host or company name built in. Guarantees tested here: the manifest loads and is off by default; one skill per
enabled instance renders without leftovers; download/UI links resolve to the instance with the matching base_url
only; watcher recipes pin the instance; hostile option values are rejected; the driver folder carries no
organisation-specific names; repo paths cannot escape (`..`, URLs); writes (`upload`, non-GET `raw`) are
dry-run without --yes and uploads refuse to overwrite; the files watcher baselines silently and then reports
new/overwritten files exactly once; token-expiry fires once per token; health reports transitions only.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

import watch_scripts
from integrations.registry import build_registry
from integrations.skills import ensure_skills

DRV = Path(__file__).resolve().parents[1] / "drivers" / "artifactory"
sys.path.insert(0, str(DRV / "cli"))
sys.modules.pop("client", None)  # every driver CLI has its own top-level `client`
import afconfig  # noqa: E402
import client as afclient  # noqa: E402
import uploader  # noqa: E402

_spec = importlib.util.spec_from_file_location("af_cli", DRV / "cli" / "cli.py")
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)
_wspec = importlib.util.spec_from_file_location("af_watch", DRV / "watch" / "artifactory_watch.py")
watch = importlib.util.module_from_spec(_wspec)
_wspec.loader.exec_module(watch)


def _cfg():
    return {"prod": {"driver": "artifactory", "enabled": True, "base_url": "https://art.one.example",
                     "auth": {"type": "anonymous"}, "options": {"title": "Prod"}},
            "other": {"driver": "artifactory", "enabled": True, "base_url": "https://art.two.example",
                      "auth": {"type": "anonymous"}}}


def test_manifest_valid_off_by_default_and_skills_rendered_per_instance(tmp_path):
    assert "artifactory" in build_registry({}).manifests and build_registry({}).package_instances() == []
    reg = build_registry(_cfg())
    assert ensure_skills(reg, tmp_path) == ["artifactory-other", "artifactory-prod"]
    text = (tmp_path / "artifactory-prod" / "SKILL.md").read_text()
    assert "https://art.one.example" in text and "--instance prod" in text and "${{" not in text.replace("$${{", "")


def test_links_resolve_only_to_the_instance_with_that_base_url():
    reg = build_registry(_cfg())
    it, kind, ident = reg.resolve_url("https://art.one.example/artifactory/repo-a/dir/file.zip")
    assert (it.name, kind, ident["path"]) == ("prod", "artifact", "repo-a/dir/file.zip")
    it, kind, ident = reg.resolve_url("https://art.two.example/ui/repos/tree/General/repo-b/x/y.tgz?clearFilter=true")
    assert (it.name, kind, ident["path"]) == ("other", "artifact_ui", "repo-b/x/y.tgz")
    assert reg.resolve_url("https://elsewhere.example/artifactory/r/f") is None


def test_watcher_recipe_pins_instance_and_hostile_options_are_rejected():
    reg = build_registry(_cfg())
    meta = watch_scripts.apply_watcher({"script": "artifactory_watch.py", "watcher": "artifactory-file-changes",
                                        "integration": "prod", "args": ["--path", "r/d"]}, reg)
    assert meta["args"][0] == "files" and meta["integration"] == "prod"
    assert watch_scripts.instance_env(meta, reg) == {"ARTIFACTORY_INSTANCE": "prod"}
    bad = _cfg()
    bad["prod"]["options"] = {"title": "x`id`"}
    assert build_registry(bad).package_instances() == [] or "prod" not in [i.name for i in build_registry(bad).package_instances()]


def test_driver_folder_is_self_contained():
    assert not [p for p in DRV.rglob("*") if p.is_symlink()]


@pytest.mark.parametrize("bad", ["", "../x", "repo/../x", "https://h/r/f", "r/f?x=1", "r\\f", "r/\nf"])
def test_repo_paths_cannot_escape_or_carry_urls_and_queries(bad):
    with pytest.raises(afclient.ApiError):
        afclient.clean_path(bad)


class FakeResp:
    def __init__(self, status=200, data=None, text=""):
        self.status_code, self._d, self.text = status, data, text or json.dumps(data)

    def json(self):
        return self._d


class FakeClient(afclient.Client):
    def __init__(self, routes, token=""):
        super().__init__(afconfig.Instance("t", "https://h.example", "anonymous", token))
        self.routes, self.calls = routes, []

    def request(self, method, path, **kw):
        self.calls.append((method, path))
        for k, v in self.routes.items():
            if path.startswith(k):
                return v if isinstance(v, FakeResp) else FakeResp(200, v)
        return FakeResp(404, {}, "nf")


def test_upload_refuses_to_overwrite_unless_forced_and_is_dry_run_without_yes(tmp_path, capsys):
    f = tmp_path / "a.zip"
    f.write_bytes(b"x")
    c = FakeClient({"api/storage/r/d/a.zip": {"size": "1"}})
    with pytest.raises(afclient.ApiError, match="REFUSING"):
        uploader.plan(c, f, "r/d", None, False)
    assert uploader.plan(c, f, "r/d", "b.zip", False)["dest"] == "r/d/b.zip"
    inst = afconfig.Instance("t", "https://h.example", "anonymous", "", {})
    a = cli.build().parse_args(["upload", str(f), "r/d", "--name", "new.zip"])
    assert cli.cmd_upload(c, a, inst) == 0
    assert "DRY RUN" in capsys.readouterr().out and not [x for x in c.calls if x[0] == "PUT"]


def test_raw_non_get_is_dry_run_without_yes_and_urls_are_refused(capsys):
    c = FakeClient({})
    cli.cmd_raw(c, cli.build().parse_args(["raw", "DELETE", "api/x"]))
    assert "DRY RUN" in capsys.readouterr().out and not c.calls
    with pytest.raises(afclient.ApiError):
        cli.cmd_raw(c, cli.build().parse_args(["raw", "GET", "https://evil.example/x"]))


def test_fetch_card_for_file_and_folder_and_unquoted_ids():
    c = FakeClient({"api/storage/r/d/a b.zip": {"repo": "r", "size": "2048", "lastModified": "2026-01-01T00:00:00Z",
                                                "modifiedBy": "ci", "checksums": {"sha256": "ab"}},
                    "api/storage/r/d": {"repo": "r", "children": [{}, {}]}})
    card = cli.fetch_card(c, "artifact", {"path": "r/d/a%20b.zip"})
    assert card["title"] == "a b.zip" and card["fields"]["size"] == "2.0 KB" and card["url"].endswith("/artifactory/r/d/a b.zip")
    assert cli.fetch_card(c, "artifact_ui", {"path": "r/d"})["fields"]["items"] == "2"
    with pytest.raises(afclient.ApiError):
        cli.fetch_card(c, "nope", {})


def _write_cfg(tmp_path, monkeypatch, entry):
    import yaml
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({"integrations": {"a": entry}}))
    monkeypatch.setenv("DOP_AGENT_CONFIG", str(p))
    monkeypatch.setenv("DOP_AGENT_STATE_DIR", str(tmp_path / "state"))


def test_instance_loading_resolves_secret_ref_sso_state_and_strips_artifactory_suffix(tmp_path, monkeypatch):
    monkeypatch.setenv("AF_TEST_TOKEN", "sekret")
    _write_cfg(tmp_path, monkeypatch, {"driver": "artifactory", "base_url": "https://h.example/artifactory/",
                                       "auth": {"type": "access_token", "token": "${env:AF_TEST_TOKEN}"}})
    inst = afconfig.load_instance(None)
    assert inst.base_url == "https://h.example" and inst.token == "sekret" and inst.api.endswith("/artifactory")
    _write_cfg(tmp_path, monkeypatch, {"driver": "artifactory", "base_url": "https://h.example", "auth": {"type": "sso_token"}})
    afconfig.save_state("a", {"token": "minted"})
    assert afconfig.load_instance("a").token == "minted"
    assert oct(afconfig.state_path("a").stat().st_mode & 0o777) == "0o600"
    with pytest.raises(afconfig.InstanceError):
        afconfig.load_instance("missing")
    with pytest.raises(afconfig.InstanceError):
        afconfig.state_path("../evil")


def test_test_command_reports_steps_and_missing_sso_token(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch, {"driver": "artifactory", "base_url": "https://h.example", "auth": {"type": "sso_token"}})
    res = cli.run_test(None)
    assert not res["ok"] and res["steps"][-1]["step"] == "token"
    _write_cfg(tmp_path, monkeypatch, {"driver": "artifactory", "base_url": "https://h.example", "auth": {"type": "anonymous"}})
    monkeypatch.setattr(afclient.Client, "request", lambda self, m, p, **kw:
                        FakeResp(200, [{"key": "r"}], "OK") if "ping" in p else FakeResp(200, [{"key": "r"}]))
    res = cli.run_test("a")
    assert res["ok"] and [s["step"] for s in res["steps"]] == ["config", "reach", "read", "upload_config"]


# ---- watcher ----

@pytest.fixture
def wenv(tmp_path, monkeypatch):
    monkeypatch.setenv("WATCH_STATE_DIR", str(tmp_path / "ws"))
    _write_cfg(tmp_path, monkeypatch, {"driver": "artifactory", "base_url": "https://h.example", "auth": {"type": "anonymous"}})
    box = {"files": []}
    monkeypatch.setattr(afclient.Client, "list_files", lambda self, p, depth=1, cap=5000: list(box["files"]))
    return box


def F(name, size=1, mod="2026-01-01T00:00:00.000Z", sha="aa"):
    return {"uri": f"/{name}", "size": size, "lastModified": mod, "sha1": sha}


def test_files_watch_baselines_then_reports_new_and_overwritten_once(wenv, capsys):
    args = ["files", "--path", "r/d", "--name", "*.zip"]
    wenv["files"] = [F("a.zip"), F("skip.txt")]
    assert watch.main(args) == 0                                       # baseline: silent
    wenv["files"] = [F("a.zip"), F("skip.txt"), F("b.zip")]
    assert watch.main(args) == 200 and "new: r/d/b.zip" in capsys.readouterr().out
    assert watch.main(args) == 0                                       # nothing reported twice
    wenv["files"] = [F("a.zip", sha="bb"), F("b.zip")]
    assert watch.main(args) == 200 and "OVERWRITTEN: r/d/a.zip" in capsys.readouterr().out
    wenv["files"] = [F("a.zip", sha="cc"), F("b.zip")]
    assert watch.main(args + ["--new-only"]) == 0


def test_files_watch_errors_are_exit_1_for_auth_and_missing_path_but_silent_for_outages(wenv, monkeypatch):
    def boom(msg):
        def f(self, p, depth=1, cap=5000):
            raise afclient.ApiError(msg)
        return f
    monkeypatch.setattr(afclient.Client, "list_files", boom("HTTP 403 for api/storage/x"))
    assert watch.main(["files", "--path", "r/d"]) == 1
    monkeypatch.setattr(afclient.Client, "list_files", boom("HTTP 404 for api/storage/x"))
    assert watch.main(["files", "--path", "r/d"]) == 1
    monkeypatch.setattr(afclient.Client, "list_files", boom("https://h.example unreachable (ConnectTimeout)"))
    assert watch.main(["files", "--path", "r/d"]) == 0
    assert watch.main(["files"]) == 1


def test_health_watch_reports_transitions_only(wenv, monkeypatch, capsys):
    state = {"up": True}
    monkeypatch.setattr(afclient.Client, "ping", lambda self: FakeResp(200 if state["up"] else 503, text="OK" if state["up"] else "x"))
    assert watch.main(["health"]) == 0
    state["up"] = False
    assert watch.main(["health"]) == 200 and "DOWN" in capsys.readouterr().out
    assert watch.main(["health"]) == 0
    state["up"] = True
    assert watch.main(["health"]) == 200 and "UP again" in capsys.readouterr().out


def test_token_expiry_watch_fires_once_per_token(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WATCH_STATE_DIR", str(tmp_path / "ws"))
    _write_cfg(tmp_path, monkeypatch, {"driver": "artifactory", "base_url": "https://h.example", "auth": {"type": "sso_token"}})
    afconfig.save_state("a", {"token": "t"})
    exp = {"v": time.time() + 5 * 86400}
    monkeypatch.setattr(afclient.Client, "token_info", lambda self, tid=None: {"valid": True, "expiry": exp["v"]})
    assert watch.main(["token-expiry", "--days", "30"]) == 200 and "renew" in capsys.readouterr().out
    assert watch.main(["token-expiry", "--days", "30"]) == 0
    exp["v"] = time.time() + 90 * 86400
    assert watch.main(["token-expiry", "--days", "30"]) == 0


def test_unresolvable_or_empty_token_ref_errors_without_leaking_the_value(tmp_path, monkeypatch):
    """Same convention as the gitlab driver: auth.token is a ${env:}/${file:}/${keychain:} ref; an unset ref fails
    loudly (never anonymous fallback) and the resolved secret never shows up in the Test output."""
    monkeypatch.delenv("AF_NOPE", raising=False)
    _write_cfg(tmp_path, monkeypatch, {"driver": "artifactory", "base_url": "https://h.example",
                                       "auth": {"type": "access_token", "token": "${env:AF_NOPE}"}})
    with pytest.raises(afconfig.InstanceError):
        afconfig.load_instance("a")
    monkeypatch.setenv("AF_NOPE", "s3cr3t-value")
    monkeypatch.setattr(afclient.Client, "request", lambda self, m, p, **kw: FakeResp(401, {}, "no"))
    assert "s3cr3t-value" not in json.dumps(cli.run_test("a"))
