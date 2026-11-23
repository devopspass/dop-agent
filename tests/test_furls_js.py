"""webui/furls.js (projection-driven link recognition / url templates / card html), run under node.

Feeds the REAL registry projection (default instances + a second gitlab) to furls.js and checks
that every supported URL shape matches, other hosts do not, watch-key -> url templating
yields the expected links, '@ref' keys give no link, and the generic card renders.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from integrations.registry import build_registry  # noqa: E402
from integration_defaults import default_test_integrations as default_integrations  # noqa: E402

FURLS = Path(__file__).resolve().parents[1] / "webui" / "furls.js"
pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node not installed")

HARNESS = r"""
const F = require(process.argv[1]);
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
F.setProjection(input.projection);
const out = {};
for (const u of input.urls) { const m = F.matchUrl(u); out[u] = m ? [m.it.name, m.kname, m.ident] : null; }
const w = {};
for (const [t, k] of input.watches) w[t + '|' + k] = F.watchLinkUrl(t, k);
const cards = input.cards.map((d) => F.cardHtml(d));
F.setProjection(null);
console.log(JSON.stringify({ out, w, cards, off: F.isLink(input.urls[0]), types: F.watchTypes() }));
"""


def _run(payload):
    ints = default_integrations()
    ints["gitlab2"] = {"driver": "gitlab", "base_url": "https://gl2.example.com",
                       "auth": {"type": "private_token", "token": "t"}}
    payload["projection"] = build_registry(ints).ui_projection()
    p = subprocess.run(["node", "-e", HARNESS, str(FURLS)], input=json.dumps(payload), capture_output=True,
                       text=True, timeout=30)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def test_old_url_shapes_match_and_foreign_hosts_do_not():
    gl, jira = "https://gitlab.example.com", "https://jira.example.net"
    urls = [f"{jira}/browse/PRJ-13862", f"{jira}/browse/PRJ-1/", f"{gl}/a/b/c/-/merge_requests/42",
            f"{gl}/a/b/-/pipelines/9001/", f"{gl}/a/b/-/jobs/12345#L10",
            "https://gl2.example.com/g/p/-/merge_requests/7", "https://other.example.com/g/p/-/jobs/1",
            "https://evil.atlassian.net/browse/PRJ-1"]
    r = _run({"urls": urls, "watches": [], "cards": []})["out"]
    assert r[urls[0]] == ["jira", "ticket", {"key": "PRJ-13862"}]
    assert r[urls[1]][0] == "jira"
    assert r[urls[2]] == ["gitlab", "merge_request", {"project": "a/b/c", "id": "42"}]
    assert r[urls[3]][1] == "pipeline" and r[urls[4]][1] == "job"
    assert r[urls[5]][0] == "gitlab2"
    assert r[urls[6]] is None and r[urls[7]] is None


def test_watch_link_templates_are_empty_for_package_drivers():
    watches = [["ticket", "PRJ-1"], ["mr", "g/p#5"], ["pipeline", "g/p#9"], ["job", "g/p#3"],
               ["pipeline", "g/p@main"], ["time", "in:5m"], ["script", "gitlab-mr"]]
    res = _run({"urls": ["x"], "watches": watches, "cards": []})
    assert set(res["w"].values()) == {""} and res["types"] == []


def test_generic_cards_render_gitlab_and_jira_layouts_and_nothing_without_projection():
    mr = {"integration": "gitlab", "driver": "gitlab", "kind": "merge_request", "ident": {"project": "g/p", "id": "42"},
          "title": "fix: x", "url": "https://u", "status": "opened", "status_emoji": "🟢", "project": "g/p",
          "fields": {"title": "fix: x", "state": "opened", "pipeline": "failed", "approvals": False, "author": "bob"}}
    jira = {"integration": "jira", "driver": "jira", "kind": "ticket", "ident": {"key": "PRJ-1"}, "title": "Summary",
            "url": "https://j", "status": "In Progress", "status_emoji": "", "key": "PRJ-1",
            "fields": {"key": "PRJ-1", "summary": "Summary", "status": "In Progress", "assignee": "Ann"}}
    res = _run({"urls": ["x"], "watches": [], "cards": [mr, jira, {**jira, "integration": "nope"}]})
    c_mr, c_jira, c_none = res["cards"]
    assert 'unfurl-project">p<' in c_mr and ">!42</a>" in c_mr
    assert "🟢 opened" in c_mr and "· failed ·" in c_mr and "⏳ approvals" in c_mr and "fix: x" in c_mr and "bob" in c_mr
    assert 'row1"' in c_jira and ">PRJ-1</a>" in c_jira and "🔄 In Progress" in c_jira
    assert "unfurl-row2\">Summary · Ann" in c_jira and "/static/icons/jira.svg" in c_jira and "/static/icons/gitlab.svg" in c_mr
    assert c_none is None
    assert res["off"] is False


def test_card_icon_comes_from_projection_icon_url_for_any_form():
    """Cards use the registry's resolved `icon_url` verbatim (static, driver-folder API or https/data URL)."""
    jira = {"integration": "jira", "driver": "jira", "kind": "ticket", "ident": {"key": "PRJ-1"}, "title": "S",
            "url": "https://j", "status": "x", "status_emoji": "", "key": "PRJ-1", "fields": {"key": "PRJ-1"}}
    for url in ("/api/drivers/jira/icon/a.svg", "https://cdn.example.com/j.png"):
        payload = {"urls": ["x"], "watches": [], "cards": [jira]}
        ints = default_integrations()
        proj = build_registry(ints).ui_projection()
        proj["integrations"][0]["kinds"]["ticket"]["icon_url"] = url
        out = subprocess.run(["node", "-e", HARNESS, str(FURLS)], input=json.dumps({**payload, "projection": proj}),
                             capture_output=True, text=True, timeout=30)
        assert out.returncode == 0, out.stderr
        assert f'src="{url}"' in json.loads(out.stdout)["cards"][0]
