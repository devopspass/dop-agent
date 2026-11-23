"""Package drivers (Teams): manifest, skill rendering, tenant routing, watcher recipes.

Feature: a driver folder with CLI + watcher scripts + skill template, off by default, one rendered skill per
enabled instance (`ms-teams-<instance>`), stale generated skills removed, watch_script recipes validated.
"""
from __future__ import annotations

import pytest

from drivers.templating import TemplateError, render
from integrations.registry import build_registry
from integrations.skills import ensure_skills
import watch_scripts

TID1, TID2 = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"


def _cfg(**enabled):
    def inst(acct, tid=None, on=True):
        o = {"cdp_url": "http://localhost:9222", "account": acct}
        if tid:
            o["tenant_id"] = tid
        return {"driver": "ms_teams", "enabled": on, "auth": {"type": "cdp_edge"}, "options": o}
    return {"aws": inst("me@example.com", TID1, enabled.get("aws", True)),
            "gcp": inst("me@example.org", TID2, enabled.get("gcp", True))}


def test_template_renders_placeholders_and_rejects_unknown_values():
    assert render("a ${{x.y}} b $${{x.y}}", {"x": {"y": "1"}}) == "a 1 b ${{x.y}}"
    with pytest.raises(TemplateError):
        render("${{x.missing}}", {"x": {}})


def test_package_driver_is_off_by_default_and_manifest_valid():
    reg = build_registry({})
    assert reg.package_instances() == []
    assert "ms_teams" in reg.manifests and reg.manifests["ms_teams"].is_package


def test_skills_rendered_per_enabled_instance_and_stale_ones_removed(tmp_path):
    reg = build_registry(_cfg())
    assert ensure_skills(reg, tmp_path) == ["ms-teams-aws", "ms-teams-gcp"]
    text = (tmp_path / "ms-teams-aws" / "SKILL.md").read_text()
    assert "me@example.com" in text and "${{" not in text.replace("$${{", "")
    (tmp_path / "user-skill").mkdir()  # not generated: must survive
    assert ensure_skills(build_registry(_cfg(gcp=False)), tmp_path) == ["ms-teams-aws"]
    assert not (tmp_path / "ms-teams-gcp").exists() and (tmp_path / "user-skill").exists()


def test_link_goes_to_instance_with_matching_tenant_and_nowhere_when_ambiguous():
    reg = build_registry(_cfg())
    url = "https://teams.microsoft.com/l/chat/19:abc@thread.v2/0?tenantId=" + TID2
    hit = reg.resolve_url(url)
    assert hit and hit[0].name == "gcp"
    assert reg.resolve_url("https://teams.microsoft.com/l/chat/19:abc@thread.v2/0") is None
    one = build_registry({"aws": _cfg()["aws"]})
    assert one.resolve_url("https://teams.microsoft.com/l/chat/19:abc@thread.v2/0")[0].name == "aws"


def test_watcher_recipe_prefixes_mode_args_and_pins_instance_env():
    reg = build_registry(_cfg(gcp=False))
    meta = watch_scripts.apply_watcher({"script": "teams_watch.py", "watcher": "teams-mentions"}, reg)
    assert meta["integration"] == "aws" and meta["args"] and not meta["args"][0].startswith("-")
    env = watch_scripts.instance_env(meta, reg)
    assert env["TEAMS_ACCOUNT"] == "me@example.com"


def test_watcher_recipe_errors_are_actionable():
    reg = build_registry(_cfg())
    with pytest.raises(ValueError):  # two instances, none named
        watch_scripts.apply_watcher({"script": "teams_watch.py", "watcher": "teams-mentions"}, reg)
    with pytest.raises(ValueError):
        watch_scripts.apply_watcher({"script": "other.py", "watcher": "teams-mentions", "integration": "aws"}, reg)


def _teams(name="aws", **opts):
    o = {"cdp_url": "http://localhost:9222", "account": "me@example.com", **opts}
    return {name: {"driver": "ms_teams", "auth": {"type": "cdp_edge"}, "options": o}}


def test_watcher_recipe_flags_the_user_passes_win_and_values_are_not_duplicated():
    """Flags the user passes override the recipe's defaults; recipe values are not appended as stray positionals."""
    reg = build_registry(_teams())
    meta = {"script": "teams_watch.py", "watcher": "teams-files", "integration": "aws", "args": ["--kind", "recording"]}
    assert watch_scripts.apply_watcher(meta, reg)["args"] == ["messages", "--kind", "recording", "--files"]
    meta["args"] = []
    assert watch_scripts.apply_watcher(meta, reg)["args"] == ["messages", "--files", "--kind", "any"]


def test_option_values_that_could_inject_into_skill_or_command_line_are_rejected():
    """Titles with quotes/newlines and cdp_urls with backticks are rejected so they cannot corrupt the skill frontmatter or command line."""
    for opts in ({"title": 'A"b'}, {"title": "a\nname: evil"}, {"cdp_url": "http://h --x `id`"},
                 {"account": "a b@x.com"}, {"tenant_id": "not-a-guid"}):
        assert not build_registry(_teams(**opts)).integrations["aws"].valid, opts
    assert build_registry(_teams(title="Work", tenant_id=TID1)).integrations["aws"].valid


def test_instance_names_with_the_same_skill_suffix_are_not_both_enabled():
    """Instance names that map to the same skill suffix (`teams_aws`/`teams-aws`) are not both enabled, so they cannot overwrite each other's skill."""
    reg = build_registry({**_teams("teams_aws"), **_teams("teams-aws", account="b@x.com")})
    assert [i.name for i in reg.package_instances()] == ["teams_aws"]
    assert any("collides" in e for e in reg.errors)


def test_pinned_watch_fails_instead_of_running_without_its_instance_env():
    """A watch pinned to a missing/disabled instance fails rather than running against the default account."""
    reg = build_registry(_teams())
    assert watch_scripts.instance_env({"integration": "aws"}, reg)["TEAMS_ACCOUNT"] == "me@example.com"
    with pytest.raises(ValueError):
        watch_scripts.instance_env({"integration": "gone"}, reg)
    assert watch_scripts.instance_env({}, reg) == {}


def test_concurrent_skill_refresh_does_not_lose_skills(tmp_path):
    """Concurrent skill refreshes use separate temp files and all skills survive."""
    import concurrent.futures as cf
    reg = build_registry(_cfg())
    with cf.ThreadPoolExecutor(8) as ex:
        results = list(ex.map(lambda _: ensure_skills(reg, tmp_path), range(24)))
    assert all(r == ["ms-teams-aws", "ms-teams-gcp"] for r in results)


def test_link_with_a_different_tenant_is_not_routed_to_the_only_instance():
    reg = build_registry(_teams(tenant_id=TID1))
    base = "https://teams.microsoft.com/l/chat/19:abc@thread.v2/0"
    assert reg.resolve_url(base + "?tenantId=" + TID2) is None
    assert reg.resolve_url(base + "?tenantId=" + TID1)[0].name == "aws"
    assert reg.resolve_url(base)[0].name == "aws"


def test_cli_and_py_scripts_run_under_the_bots_own_interpreter(tmp_path):
    """Feature: package CLIs and .py watcher scripts use sys.executable (it has the deps), also in rendered skills."""
    import sys
    from drivers.package import PackageDriver
    reg = build_registry(_teams())
    assert PackageDriver(reg.integrations["aws"]).cli_argv("test")[0] == sys.executable
    ensure_skills(reg, tmp_path)
    assert sys.executable + " " in (tmp_path / "ms-teams-aws" / "SKILL.md").read_text()
    assert watch_scripts.script_argv(tmp_path / "x.py", ["a"]) == [sys.executable, str(tmp_path / "x.py"), "a"]


def test_user_installed_package_driver_watch_scripts_resolve_but_still_need_a_dry_run(tmp_path, monkeypatch):
    """Feature: ~/.dop-agent/drivers/<id>/watch/ scripts are found, yet are not exempt from the plugin dry-run rule."""
    w = tmp_path / "mydrv" / "watch"
    w.mkdir(parents=True)
    f = w / "plug_watch.py"
    f.write_text("#!/usr/bin/env python3\n")
    f.chmod(0o755)
    monkeypatch.setattr(watch_scripts, "PLUGIN_DRIVERS_DIR", tmp_path)
    assert watch_scripts.resolve_script("plug_watch.py") == f.resolve() or watch_scripts.resolve_script("plug_watch.py") == f
    with pytest.raises(ValueError, match="dry-run"):
        watch_scripts.require_dry_run("plug_watch.py")


def test_teams_cli_configure_runs_and_resets_cached_tokens():
    """configure() runs and resets cached tokens, so CLI calls and the Settings Test work."""
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("teams_auth", Path(__file__).parent.parent / "drivers/ms_teams/cli/auth.py")
    auth = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(auth)
    auth._tokens["x"] = "t"
    auth.configure("localhost:9333", "A@B.com")
    assert auth._cdp_url == "http://localhost:9333" and auth._account == "a@b.com" and not auth._tokens


def _outlook_cfg():
    return {"main": {"driver": "ms_outlook", "enabled": True, "auth": {"type": "cdp_edge"},
                     "options": {"cdp_url": "http://localhost:9222", "account": "me@example.com"}}}


def test_outlook_driver_renders_skill_pins_instance_and_matches_mail_links(tmp_path):
    """ms_outlook: skill ms-outlook-main, recipe pins OUTLOOK_* env, mail/event links resolve to the instance."""
    reg = build_registry(_outlook_cfg())
    assert ensure_skills(reg, tmp_path) == ["ms-outlook-main"]
    text = (tmp_path / "ms-outlook-main" / "SKILL.md").read_text()
    assert "me@example.com" in text and "outlook-new" in text and "${{" not in text.replace("$${{", "")
    meta = watch_scripts.apply_watcher({"script": "outlook_watch.py", "watcher": "outlook-new"}, reg)
    assert meta["integration"] == "main" and meta["args"][0] == "mail"
    env = watch_scripts.instance_env(meta, reg)
    assert env["OUTLOOK_ACCOUNT"] == "me@example.com" and env["OUTLOOK_CDP_URL"].startswith("http://localhost")
    for url in ("https://outlook.office.com/mail/inbox/id/AAMk%3D",
                "https://outlook.office.com/calendar/item/AAMk%3D"):
        assert reg.resolve_url(url)[0].name == "main"


def test_outlook_fetch_decodes_url_ids_and_builds_cards(monkeypatch):
    """furl.fetch unquotes the id captured from a link and returns a normalized message/event card."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "drivers" / "ms_outlook" / "cli"))
    import api, furl
    seen = []

    def fake(path, *a, **k):
        seen.append(path)
        if "/messages/" in path:
            return {"subject": "Hi", "from": {"emailAddress": {"name": "Al", "address": "a@x.com"}}, "webLink": "https://w"}
        return {"subject": "Sync", "start": {"dateTime": "2026-12-01T09:00"}, "organizer": {"emailAddress": {"name": "Bo"}}}

    monkeypatch.setattr(api, "request", fake)
    msg = furl.fetch("message", {"id": "AA%3D%3D"})
    assert msg["title"] == "Hi" and msg["fields"]["from"] and "AA==" in seen[0].replace("%3D", "=")
    assert furl.fetch("event", {"id": "EE"})["fields"]["organizer"] == "Bo"
    with pytest.raises(RuntimeError):
        furl.fetch("nope", {})
