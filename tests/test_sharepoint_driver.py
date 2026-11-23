"""SharePoint / OneDrive package driver (drivers/ms_sharepoint): extraction, addressing, draft-first writes, watcher, skill.

Feature: read M365 files through the Graph token of a signed-in Teams tab (no real credentials needed here: the Graph
seam `api.request` / the watcher's `graph_get` are faked). Guarantees tested: docx/pptx/xlsx/vtt become compact text
(xlsx ranges as tables), targets resolve to the right Graph resource, `upload`/`cell-update`/`link-create` write
NOTHING without --yes (bug class: an agent "testing" a write against a shared App Register), the watcher records a
silent baseline then reports new/changed files once, auth errors exit 1 while 429 is silent, option values cannot
inject into the skill/argv, and the skill renders per instance with the generic short description.
"""
import importlib.util
import io
import json
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from integrations.registry import build_registry
from integrations.skills import ensure_skills
import watch_scripts

DRV = Path(__file__).resolve().parents[1] / "drivers" / "ms_sharepoint"
CLI = DRV / "cli"
_NAMES = ("api", "util", "furl", "files", "extract", "cli")


def _load_cli():
    """Import the driver CLI modules under their plain names, then restore sys.modules (ms_outlook has its own `api`)."""
    saved = {n: sys.modules.pop(n, None) for n in _NAMES}
    sys.path.insert(0, str(CLI))
    try:
        import api, extract, files, furl, util  # noqa: E401
        return SimpleNamespace(api=api, extract=extract, files=files, furl=furl, util=util)
    finally:
        sys.path.remove(str(CLI))
        for n in _NAMES:
            sys.modules.pop(n, None)
            if saved[n] is not None:
                sys.modules[n] = saved[n]


M = _load_cli()


def _watch():
    spec = importlib.util.spec_from_file_location("sharepoint_watch", DRV / "watch" / "sharepoint_watch.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sw = _watch()


def _zip(files):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for k, v in files.items():
            z.writestr(k, v)
    return b.getvalue()


WNS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
DOCX = _zip({"word/document.xml": f'<w:document {WNS}><w:body>'
             '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Plan</w:t></w:r></w:p>'
             '<w:p><w:r><w:t>Move apps</w:t></w:r></w:p>'
             '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>App</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Wave</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
             '</w:body></w:document>'})
ANS = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
PPTX = _zip({"ppt/slides/slide2.xml": f"<p {ANS}><a:p><a:r><a:t>Second</a:t></a:r></a:p></p>",
             "ppt/slides/slide1.xml": f"<p {ANS}><a:p><a:r><a:t>First</a:t></a:r></a:p></p>"})
XNS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
XLSX = _zip({
    "xl/workbook.xml": f'<workbook {XNS}><sheets><sheet name="Apps" sheetId="1" r:id="rId1"/>'
                       '<sheet name="Hidden" sheetId="2" state="hidden" r:id="rId2"/></sheets></workbook>',
    "xl/_rels/workbook.xml.rels": '<Relationships xmlns="x"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
                                  '<Relationship Id="rId2" Target="/xl/worksheets/sheet2.xml"/></Relationships>',
    "xl/sharedStrings.xml": f'<sst {XNS}><si><t>App</t></si><si><t>Wave</t></si><si><t>CRM</t></si></sst>',
    "xl/worksheets/sheet1.xml": f'<worksheet {XNS}><sheetData>'
        '<row r="2"><c r="B2" t="s"><v>0</v></c><c r="C2" t="s"><v>1</v></c></row>'
        '<row r="3"><c r="B3" t="s"><v>2</v></c><c r="C3"><v>12</v></c><c r="D3" t="b"><v>1</v></c></row>'
        '</sheetData></worksheet>',
    "xl/worksheets/sheet2.xml": f'<worksheet {XNS}><sheetData/></worksheet>'})
VTT = (b"WEBVTT\n\n1\n00:00:01.000 --> 00:00:04.000\n<v Anna>Hello</v>\n\n"
       b"2\n00:00:04.500 --> 00:00:06.000\n<v Anna>team</v>\n\n3\n00:01:05.000 --> 00:01:07.000\n<v Bob>Hi Anna</v>\n")


def test_docx_pptx_text_keeps_headings_tables_and_slide_order():
    assert M.extract.docx_text(DOCX).splitlines() == ["# Plan", "Move apps", "| App | Wave |"]
    assert M.extract.pptx_text(PPTX).index("First") < M.extract.pptx_text(PPTX).index("Second")


def test_xlsx_range_is_a_bounded_table_with_column_letters_and_row_numbers():
    res = M.extract.xlsx_range(XLSX)
    assert res["range"] == "B2:D3" and res["rows"][1] == ["CRM", "12", "TRUE"]
    out = M.extract.render_table(M.extract.xlsx_range(XLSX, "Apps", "C2:C3"))
    assert out.splitlines()[1:] == ["row\tC", "2\tWave", "3\t12"]
    assert M.extract.xlsx_range(XLSX, max_rows=1)["truncated"]
    assert [s["name"] for s in M.extract.xlsx_sheets(XLSX)] == ["Apps", "Hidden"]
    with pytest.raises(M.extract.ExtractError):
        M.extract.xlsx_range(XLSX, "Nope")
    with pytest.raises(M.extract.ExtractError):
        M.extract.parse_range("D3:A1")


def test_vtt_transcript_merges_same_speaker_cues_and_optionally_keeps_times():
    assert M.extract.vtt_text(VTT) == "Anna: Hello team\nBob: Hi Anna"
    assert M.extract.vtt_text(VTT, timestamps=True).splitlines()[1].startswith("[00:01:05] Bob")
    with pytest.raises(M.extract.ExtractError):
        M.extract.to_text("pic.png", b"x")


def test_corrupt_office_file_gives_a_clear_error_not_a_traceback():
    with pytest.raises(M.extract.ExtractError):
        M.extract.docx_text(b"not a zip")


def test_targets_resolve_to_graph_resources_and_bad_libraries_are_rejected():
    a = M.api
    assert a.item_path("/Recordings/a b.vtt") == "/me/drive/root:/Recordings/a%20b.vtt:"
    assert a.item_path("b!Ab_-1:01XYZ") == "/drives/b!Ab_-1/items/01XYZ"
    assert a.item_path("f2", refs={"f2": "b!d:IT"}) == "/drives/b!d/items/IT"
    assert a.item_path("https://x.sharepoint.com/:x:/s/A/f").startswith("/shares/u!")
    assert a.drive_base("https://acme.sharepoint.com/sites/Team") == "/sites/acme.sharepoint.com:/sites/Team:/drive"
    assert a.drive_base(None, "b!abc") == "/drives/b!abc" and a.drive_base() == "/me/drive"
    for bad in ("https://evil.com/sites/x", "acme.sharepoint.com/sites/a b"):
        with pytest.raises(a.GraphError):
            a.drive_base(bad)
    with pytest.raises(a.GraphError):
        a.drive_base(None, "b!x/../me")
    with pytest.raises(a.GraphError):
        a.item_path("f9", refs={})


def test_errors_never_contain_bearer_tokens():
    assert "abc123" not in M.api.redact("Authorization: Bearer abc123 token=abc123")


def test_bearer_token_is_never_sent_to_non_graph_urls():
    """A poisoned @odata.nextLink/deltaLink or path must not exfiltrate the Graph token."""
    with pytest.raises(M.api.GraphError):
        M.api.request("https://evil.example.com/steal")


class Fake:
    """Records Graph calls and answers from a table keyed by substring."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, path, method="GET", body=None, data=None, headers=None, **kw):
        self.calls.append((method, path, body))
        for key, val in self.answers.items():
            if key in path:
                if isinstance(val, Exception):
                    raise val
                return val(path) if callable(val) else val
        raise AssertionError("unexpected call " + path)


def _args(**kw):
    base = dict(site=None, drive=None, json=False, yes=False)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def cli(monkeypatch, tmp_path):
    monkeypatch.setattr(M.util, "ROOT", str(tmp_path / "refs"))
    return M


def test_ls_assigns_refs_that_later_commands_resolve(cli, monkeypatch, capsys):
    item = {"id": "I1", "name": "App Register.xlsx", "size": 2048, "file": {}, "lastModifiedDateTime": "2026-11-20T10:00:00Z",
            "lastModifiedBy": {"user": {"displayName": "Anna"}}, "parentReference": {"driveId": "b!d", "path": "/drive/root:/Docs"}}
    fake = Fake({"/children": {"value": [item]}})
    monkeypatch.setattr(M.api, "request", fake)
    M.files.cmd_ls(_args(target="/Docs", top=50))
    out = capsys.readouterr().out
    assert "f1\tfile\tApp Register.xlsx\t2.0KB" in out
    assert M.api.item_path("f1", refs=M.util.load_refs()) == "/drives/b!d/items/I1"
    assert fake.calls[0][1].startswith("/me/drive/root:/Docs:/children")


def test_search_escapes_quotes_in_the_query(cli, monkeypatch, capsys):
    fake = Fake({"search(q=": {"value": []}})
    monkeypatch.setattr(M.api, "request", fake)
    M.files.cmd_search(_args(query="it's", top=25))
    assert "search(q='it%27%27s')" in fake.calls[0][1]


def test_upload_without_yes_is_a_draft_and_never_writes(cli, monkeypatch, capsys, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hi")
    old = {"id": "1", "name": "a.txt", "file": {}, "lastModifiedDateTime": "2026-01-01T00:00:00Z"}
    fake = Fake({"/root:/Docs/a.txt:": old})
    monkeypatch.setattr(M.api, "request", fake)
    M.files.cmd_upload(_args(local=str(f), to="/Docs/", replace=False))
    assert "DRAFT" in capsys.readouterr().out
    assert all(c[0] == "GET" for c in fake.calls)
    with pytest.raises(SystemExit):  # exists and no --replace: even with --yes it refuses
        M.files.cmd_upload(_args(local=str(f), to="/Docs/", replace=False, yes=True))
    assert all(c[0] == "GET" for c in fake.calls)


def test_upload_with_yes_puts_small_file_with_conflict_behavior(cli, monkeypatch, capsys, tmp_path):
    f = tmp_path / "n.txt"
    f.write_text("hi")

    def answer(path):
        if "content" in path:
            return {"webUrl": "https://w/n"}
        raise M.api.GraphError("HTTP 404 GET x")

    fake = Fake({"/root:/n.txt:": answer})
    monkeypatch.setattr(M.api, "request", fake)
    M.files.cmd_upload(_args(local=str(f), to="/n.txt", replace=False, yes=True))
    put = [c for c in fake.calls if c[0] == "PUT"][0]
    assert "conflictBehavior=fail" in put[1] and "uploaded: https://w/n" in capsys.readouterr().out


def test_cell_update_draft_shows_old_and_new_and_validates_shape(cli, monkeypatch, capsys):
    fake = Fake({"range(address='B2:C2')": {"values": [["x", "y"]]}})
    monkeypatch.setattr(M.api, "request", fake)
    a = _args(target="/book.xlsx", sheet="Apps", range="b2:c2", values='[["A",1]]', values_file=None)
    M.files.cmd_cell_update(a)
    out = capsys.readouterr().out
    assert "DRAFT" in out and '["x", "y"] -> ["A", 1]' in out and all(c[0] == "GET" for c in fake.calls)
    with pytest.raises(SystemExit):
        M.files.cmd_cell_update(_args(target="/b.xlsx", sheet="Apps", range="B2:C2", values='[["A"]]', values_file=None))
    M.files.cmd_cell_update(_args(**{**a.__dict__, "yes": True}))
    assert fake.calls[-1][0] == "PATCH" and fake.calls[-1][2] == {"values": [["A", 1]]}


def test_link_create_is_draft_and_anonymous_needs_explicit_flag(cli, monkeypatch, capsys):
    fake = Fake({"/createLink": {"link": {"webUrl": "https://l"}}, "/me/drive/root:/f.docx:": {"name": "f.docx", "webUrl": "https://w"}})
    monkeypatch.setattr(M.api, "request", fake)
    M.files.cmd_link_create(_args(target="/f.docx", type="view", scope="organization", allow_anonymous=False))
    assert "DRAFT" in capsys.readouterr().out and not [c for c in fake.calls if c[0] == "POST"]
    with pytest.raises(SystemExit):
        M.files.cmd_link_create(_args(target="/f.docx", type="view", scope="anonymous", allow_anonymous=False, yes=True))
    M.files.cmd_link_create(_args(target="/f.docx", type="edit", scope="organization", allow_anonymous=False, yes=True))
    assert [c for c in fake.calls if c[0] == "POST"][0][2] == {"type": "edit", "scope": "organization"}


def test_read_vtt_and_xlsx_end_to_end(cli, monkeypatch, capsys):
    monkeypatch.setattr(M.api, "download", lambda item, limit=0: ("call.vtt", VTT))
    base = dict(target="/call.vtt", chars=6000, full=False, sheets=False, sheet=None, range=None, max_rows=50,
                max_cols=20, timestamps=False, max_bytes=1000)
    M.files.cmd_read(_args(**base))
    assert capsys.readouterr().out.strip() == "Anna: Hello team\nBob: Hi Anna"
    monkeypatch.setattr(M.api, "download", lambda item, limit=0: ("reg.xlsx", XLSX))
    M.files.cmd_read(_args(**{**base, "sheets": True}))
    assert "Apps\tvisible\tB2:D3" in capsys.readouterr().out


def test_download_sanitizes_the_remote_file_name(cli, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(M.api, "download", lambda item, limit=0: ("../../evil.txt", b"x"))
    M.files.cmd_download(_args(target="/x", out=str(tmp_path), max_bytes=10))
    assert (tmp_path / "evil.txt").read_bytes() == b"x" and not (tmp_path.parent / "evil.txt").exists()


def test_fetch_builds_a_card_from_a_sharing_url(monkeypatch):
    seen = []
    monkeypatch.setattr(M.api, "request", lambda p, *a, **k: seen.append(p) or {"name": "Plan.docx", "size": 10, "file": {},
                                                                                "webUrl": "https://w", "lastModifiedDateTime": "2026-11-20T10:00:00Z"})
    card = M.furl.fetch("file", {"url": "https://acme.sharepoint.com/:w:/s/A/xyz"})
    assert card["title"] == "Plan.docx" and card["fields"]["type"] == "file" and seen[0].startswith("/shares/u!")
    with pytest.raises(RuntimeError):
        M.furl.fetch("other", {})


def test_test_subcommand_reports_missing_cdp_as_a_failed_step_not_a_crash(monkeypatch):
    class Auth:
        _cdp_url = "http://x"

        @staticmethod
        def _cdp_json(p):
            raise RuntimeError("CDP endpoint http://x is not available")

    monkeypatch.setattr(M.api, "_teams_auth", lambda: Auth)
    res = M.furl.run_test()
    assert res["ok"] is False and res["steps"][-1]["step"] == "cdp" and "token" not in json.dumps(res).lower().replace("no usable token", "")


def test_test_subcommand_success_persists_tenant_id(monkeypatch):
    class Auth:
        _cdp_url = "http://x"
        _cdp_json = staticmethod(lambda p: [{"id": 1}] if p == "/json" else {})
        _is_teams = staticmethod(lambda t: True)

    monkeypatch.setattr(M.api, "_teams_auth", lambda: Auth)
    monkeypatch.setattr(M.api, "token", lambda: "secret-token-value")
    monkeypatch.setattr(M.api, "claims", lambda: {"upn": "me@x.com", "tid": "AAAAAAAA-1111-2222-3333-444444444444", "scp": "Files.ReadWrite.All Mail.Read"})
    monkeypatch.setattr(M.api, "request", lambda p, *a, **k: {"displayName": "Me", "userPrincipalName": "me@x.com", "driveType": "business", "webUrl": "https://d"})
    res = M.furl.run_test()
    assert res["ok"] and res["set_options"] == {"tenant_id": "aaaaaaaa-1111-2222-3333-444444444444"}
    assert [s["step"] for s in res["steps"]] == ["python", "cdp", "teams_tab", "token", "graph_me", "onedrive"]
    assert "secret-token-value" not in json.dumps(res) and "Files.ReadWrite.All" in json.dumps(res)


# ---------- watcher ----------

def _it(i, name, etag, mod="2026-11-20T10:00:00Z", folder=False, **kw):
    d = {"id": i, "name": name, "eTag": etag, "size": 2048, "lastModifiedDateTime": mod, "webUrl": "https://w/" + name,
         "lastModifiedBy": {"user": {"displayName": "Anna"}},
         "parentReference": {"driveId": "b!d", "path": "/drive/root:/Recordings"}, **kw}
    if folder:
        d["folder"] = {}
    return d


@pytest.fixture
def wenv(tmp_path, monkeypatch):
    monkeypatch.setenv("WATCH_STATE_DIR", str(tmp_path))
    return tmp_path


def test_folder_watch_baselines_silently_then_reports_new_and_changed_once(wenv, monkeypatch, capsys):
    files = {"value": [_it("1", "a.vtt", "e1")]}
    monkeypatch.setattr(sw, "graph_get", lambda p: files)
    assert sw.main(["folder", "--path", "/Recordings"]) == 0 and capsys.readouterr().out == ""
    assert sw.main(["folder", "--path", "/Recordings"]) == 0
    files["value"] = [_it("1", "a.vtt", "e2"), _it("2", "b.vtt", "e1")]
    assert sw.main(["folder", "--path", "/Recordings"]) == 200
    out = capsys.readouterr().out
    assert "NEW /Recordings/b.vtt" in out and "CHANGED /Recordings/a.vtt" in out and "by Anna" in out
    assert sw.main(["folder", "--path", "/Recordings"]) == 0


def test_folder_watch_filters_new_only_ext_and_skips_subfolders(wenv, monkeypatch, capsys):
    files = {"value": [_it("1", "a.vtt", "e1")]}
    monkeypatch.setattr(sw, "graph_get", lambda p: files)
    argv = ["folder", "--path", "/R", "--new-only", "--ext", "vtt"]
    sw.main(argv)
    files["value"] = [_it("1", "a.vtt", "e2"), _it("2", "b.docx", "e1"), _it("3", "sub", "e1", folder=True), _it("4", "c.vtt", "e1")]
    assert sw.main(argv) == 200
    out = capsys.readouterr().out
    assert "c.vtt" in out and "a.vtt" not in out and "b.docx" not in out and "sub" not in out


def test_file_watch_fires_on_new_version_only(wenv, monkeypatch, capsys):
    cur = {"v": _it("1", "reg.xlsx", "e1")}
    monkeypatch.setattr(sw, "graph_get", lambda p: cur["v"])
    assert sw.main(["file", "--target", "/reg.xlsx"]) == 0
    assert sw.main(["file", "--target", "/reg.xlsx"]) == 0
    cur["v"] = _it("1", "reg.xlsx", "e2")
    assert sw.main(["file", "--target", "/reg.xlsx"]) == 200 and "CHANGED" in capsys.readouterr().out
    assert sw.main(["file"]) == 1


def test_site_watch_uses_delta_cursor_and_labels_new_changed_deleted(wenv, monkeypatch, capsys):
    calls = []

    def gg(p):
        calls.append(p)
        if "token=latest" in p:
            return {"@odata.deltaLink": "https://g/delta?token=T1", "value": []}
        return {"@odata.deltaLink": "https://g/delta?token=T2", "value": [
            _it("1", "n.docx", "e1", mod="2026-11-20T10:00:00Z", createdDateTime="2026-11-20T10:00:00Z"),
            _it("2", "o.docx", "e2", mod="2026-11-20T10:00:00Z", createdDateTime="2026-01-01T10:00:00Z"),
            {"id": "3", "name": "gone.docx", "deleted": {}, "parentReference": {"path": "/drive/root:/Recordings"}},
            _it("4", "f", "e", folder=True)]}

    monkeypatch.setattr(sw, "graph_get", gg)
    assert sw.main(["site"]) == 0
    assert sw.main(["site"]) == 200
    out = capsys.readouterr().out
    assert "NEW /Recordings/n.docx" in out and "CHANGED /Recordings/o.docx" in out and "DELETED gone.docx" in out and " f " not in out
    assert calls[-1] == "https://g/delta?token=T1"
    assert json.loads((wenv / "sharepoint-site.json").read_text())["delta"].endswith("T2")


def test_watcher_auth_failure_exits_1_and_throttling_is_silent(wenv, monkeypatch, capsys):
    def boom(msg):
        def f(p):
            raise sw.AuthError(msg) if "auth" in msg else sw.Transient(msg)
        return f
    monkeypatch.setattr(sw, "graph_get", boom("auth lost"))
    assert sw.main(["folder"]) == 1 and "auth lost" in capsys.readouterr().err
    monkeypatch.setattr(sw, "graph_get", boom("HTTP 429"))
    assert sw.main(["folder"]) == 0
    assert sw.main(["folder", "--name", "("]) == 1  # invalid regex is an error, not a crash


# ---------- driver wiring ----------

def _cfg(**opts):
    o = {"cdp_url": "http://localhost:9222", "account": "me@example.com", **opts}
    return {"main": {"driver": "ms_sharepoint", "enabled": True, "auth": {"type": "cdp_edge"}, "options": o}}


def test_driver_is_off_by_default_renders_skill_and_pins_instance_env(tmp_path):
    assert build_registry({}).package_instances() == []
    reg = build_registry(_cfg())
    assert ensure_skills(reg, tmp_path) == ["ms-sharepoint-main"]
    text = (tmp_path / "ms-sharepoint-main" / "SKILL.md").read_text()
    assert "${{" not in text.replace("$${{", "") and "me@example.com" in text and "sharepoint-folder" in text
    desc = [ln for ln in text.splitlines() if ln.startswith("description:")][0]
    assert "cli" not in desc.lower() and "--" not in desc and len(desc) < 220
    meta = watch_scripts.apply_watcher({"script": "sharepoint_watch.py", "watcher": "sharepoint-folder"}, reg)
    assert meta["integration"] == "main" and meta["args"][0] == "folder"
    env = watch_scripts.instance_env(meta, reg)
    assert env["SHAREPOINT_ACCOUNT"] == "me@example.com" and env["SHAREPOINT_CDP_URL"].startswith("http://localhost")


def test_sharepoint_links_resolve_to_the_instance_and_bad_options_are_rejected():
    reg = build_registry(_cfg())
    assert reg.resolve_url("https://acme-my.sharepoint.com/:x:/g/personal/me_example_com/Eabc?e=1")[0].name == "main"
    assert reg.resolve_url("https://example.com/x") is None
    for opts in ({"title": 'A"b'}, {"cdp_url": "http://h `id`"}, {"account": "a b@x.com"}, {"tenant_id": "nope"}):
        assert not build_registry(_cfg(**opts)).integrations["main"].valid


def test_watch_script_is_executable_and_not_group_writable():
    mode = (DRV / "watch" / "sharepoint_watch.py").stat().st_mode
    assert mode & 0o100 and not mode & 0o022
