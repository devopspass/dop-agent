import json
import sys
from pathlib import Path

import pytest

import importlib.util

CLI_DIR = Path(__file__).resolve().parents[1] / "drivers" / "gitlab" / "cli"
sys.path.insert(0, str(CLI_DIR))
_spec = importlib.util.spec_from_file_location("gitlab_cli", CLI_DIR / "cli.py")
gl_api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gl_api)


class Resp:
    def __init__(self, data, headers=None, status=200):
        self._d, self.headers, self.status_code = data, headers or {}, status
        self.ok = status < 400
        self.text = data if isinstance(data, str) else json.dumps(data)

    def json(self):
        return self._d


class FakeClient(gl_api.Client):
    def __init__(self, routes):
        self.routes, self.calls = routes, []
        self.api = "https://x/api/v4"

    def _get(self, path, params=None):
        self.calls.append(path)
        for k, v in self.routes.items():
            if path.endswith(k):
                return v if isinstance(v, Resp) else Resp(v)
        raise gl_api.ConfigError(f"404 {path}")

    def get_job_trace_tail(self, project, job_id, max_bytes=0, hard_cap=0):
        return self.routes["trace"]

    def request(self, method, path, params=None, body=None):
        self.calls.append((method, path))
        return {"ok": 1, "extra": {"a": 5}}


PROJ = "g/s/proj"
MR = {"title": "feat: x", "state": "opened", "draft": True, "source_branch": "f", "target_branch": "master",
      "detailed_merge_status": "not_approved", "web_url": "https://x/mr"}
DISC = [
    {"id": "aaaaaaaaaa", "notes": [{"resolvable": True, "resolved": False, "author": {"username": "bot"},
                                    "body": "line\n" + "y" * 300,
                                    "position": {"new_path": "a.rb", "new_line": 4}}]},
    {"id": "bbbbbbbbbb", "notes": [{"resolvable": True, "resolved": True, "author": {"username": "u"}, "body": "ok"}]},
    {"id": "cccccccccc", "notes": [{"resolvable": False, "system": True, "author": {"username": "u"}, "body": "s"}]},
]
JOBS = [{"id": 1, "name": "a", "stage": "s", "status": "success", "duration": 3.2},
        {"id": 2, "name": "b", "stage": "s", "status": "failed", "duration": 9}]


def routes():
    return {"/merge_requests/5": MR, "/approvals": {"approved_by": [{"user": {"username": "jo"}}], "approvals_left": 0},
            "/discussions": DISC, "/merge_requests/5/pipelines": [{"id": 77, "status": "failed"}],
            "/pipelines/77/jobs": JOBS, "/pipelines/77/bridges": []}


def test_parse_ref():
    assert gl_api.parse_ref("a/b!12") == ("a/b", "12")
    assert gl_api.parse_ref("b") == ("b", None)
    with pytest.raises(gl_api.GlError):
        gl_api.parse_ref("b!x")


def test_mr_status_rc():
    assert gl_api.main(["mr-status", f"{PROJ}!5"], FakeClient(routes())) == 0


def test_mr_status_text(capsys):
    gl_api.main(["mr-status", f"{PROJ}!5"], FakeClient(routes()))
    out = capsys.readouterr().out
    assert "draft" in out and "approvals:1/1 (jo)" in out and "threads_open:1" in out
    assert "FAILED 2 b" in out and "#77 failed" in out
    assert len(out) < 600


def test_threads_unresolved_truncates():
    rows = gl_api.thread_rows(DISC, True)
    assert len(rows) == 1 and "a.rb:4" in rows[0] and "@bot" in rows[0]
    assert len(rows[0].split("| ", 1)[1]) <= 200
    assert len(gl_api.thread_rows(DISC)) == 2


def test_pagination():
    class C(FakeClient):
        n = 0

        def _get(self, path, params=None):
            self.n += 1
            return Resp([self.n], {"x-next-page": "2" if self.n < 3 else ""})
    assert C({}).get_all("/x") == [1, 2, 3]


def test_pipeline_jobs_failed(capsys):
    gl_api.main(["pipeline-jobs", PROJ, "--mr", "5", "--failed"], FakeClient(routes()))
    out = capsys.readouterr().out
    assert "2 b s failed 9s" in out and "1 a" not in out


RAW = ("2026-10-14T12:11:52.95Z 00O section_start:1791979912:step_script\n"
       "2026-10-14T12:11:52.96Z 00O \x1b[31mERROR\x1b[0m: boom token=abc123\n"
       "2026-10-14T12:11:52.97Z 01O \n"
       "2026-10-14T12:11:52.98Z 00O ok line\n")


def test_clean_log():
    lines = gl_api.clean_log(RAW)
    assert lines == ["ERROR: boom token=***", "ok line"]
    assert all("\x1b" not in ln and "2026-" not in ln for ln in lines)


def test_log_tail_and_grep():
    lines = [f"l{i}" for i in range(100)] + ["ERROR bad"]
    t = gl_api.log_tail(lines, 5)
    assert t[0] == "l96" and "last error-like line: 101" in t[-1]
    g = gl_api.log_tail(lines, grep="error", ctx=1)
    assert g == ["100: l99", "101: ERROR bad"]
    assert gl_api.log_tail(lines, grep="zzz")[0].startswith("(no match")


def test_job_log_tail_cmd(capsys):
    gl_api.main(["job-log-tail", PROJ, "9", "--lines", "2"], FakeClient({"trace": RAW}))
    assert "ok line" in capsys.readouterr().out


def test_resolve_project_suffix(tmp_path, monkeypatch):
    monkeypatch.setattr(gl_api, "CACHE", tmp_path / "c.json")
    c = FakeClient({"/projects": [{"path_with_namespace": "g/a/proj"}, {"path_with_namespace": "g/a/proj-2"}]})
    assert gl_api.resolve_project(c, "proj") == "g/a/proj"
    n = len(c.calls)
    assert gl_api.resolve_project(c, "proj") == "g/a/proj" and len(c.calls) == n  # cached
    assert gl_api.resolve_project(c, "x/y") == "x/y"


def test_resolve_project_ambiguous_and_none(tmp_path, monkeypatch):
    monkeypatch.setattr(gl_api, "CACHE", tmp_path / "c.json")
    c = FakeClient({"/projects": [{"path_with_namespace": "g/a/p"}, {"path_with_namespace": "h/p"}]})
    with pytest.raises(gl_api.GlError, match="ambiguous"):
        gl_api.resolve_project(c, "p")
    with pytest.raises(gl_api.GlError, match="not found"):
        gl_api.resolve_project(FakeClient({"/projects": []}), "p")


def test_api_requires_yes_for_writes(capsys):
    c = FakeClient({})
    assert gl_api.main(["api", "POST", "/x"], c) == 1
    assert ("POST", "/x") not in c.calls
    assert gl_api.main(["api", "POST", "/x", "--yes"], c) == 0


def test_api_fields_and_truncate(capsys):
    gl_api.main(["api", "GET", "/x", "--fields", "ok,extra.a"], FakeClient({}))
    assert json.loads(capsys.readouterr().out) == {"ok": 1, "extra.a": 5}
    gl_api.main(["api", "GET", "/x", "--max-chars", "5"], FakeClient({}))
    assert "truncated" in capsys.readouterr().out


class RecClient(FakeClient):
    """Records writer REST calls (method, path, body) and returns canned JSON."""

    def request(self, method, path, params=None, body=None):
        self.calls.append((method, path, body))
        return {"id": 9, "status": "pending", "state": "merged", "name": "n", "iid": 3, "title": "t",
                "web_url": "u", "detailed_merge_status": "mergeable", "approved_by": [1],
                "commit": {"short_id": "abc"}}


@pytest.mark.parametrize("argv,call", [
    (["mr-comment", f"{PROJ}!5", "--body", "hi"], ("POST", "/projects/g%2Fs%2Fproj/merge_requests/5/notes", {"body": "hi"})),
    (["mr-comment", f"{PROJ}!5", "--body", "hi", "--reply", "d" * 40],
     ("POST", "/projects/g%2Fs%2Fproj/merge_requests/5/discussions/" + "d" * 40 + "/notes", {"body": "hi"})),
    (["thread-resolve", f"{PROJ}!5", "d" * 40], ("PUT", "/projects/g%2Fs%2Fproj/merge_requests/5/discussions/" + "d" * 40, {"resolved": True})),
    (["thread-resolve", f"{PROJ}!5", "d" * 40, "--unresolve"],
     ("PUT", "/projects/g%2Fs%2Fproj/merge_requests/5/discussions/" + "d" * 40, {"resolved": False})),
    (["mr-approve", f"{PROJ}!5"], ("POST", "/projects/g%2Fs%2Fproj/merge_requests/5/approve", None)),
    (["mr-merge", f"{PROJ}!5", "--squash", "--auto"],
     ("PUT", "/projects/g%2Fs%2Fproj/merge_requests/5/merge",
      {"squash": True, "should_remove_source_branch": False, "auto_merge": True})),
    (["job-play", PROJ, "7"], ("POST", "/projects/g%2Fs%2Fproj/jobs/7/play", None)),
    (["job-retry", PROJ, "7"], ("POST", "/projects/g%2Fs%2Fproj/jobs/7/retry", None)),
    (["job-cancel", PROJ, "7"], ("POST", "/projects/g%2Fs%2Fproj/jobs/7/cancel", None)),
    (["pipeline-retry", PROJ, "8"], ("POST", "/projects/g%2Fs%2Fproj/pipelines/8/retry", None)),
    (["pipeline-cancel", PROJ, "8"], ("POST", "/projects/g%2Fs%2Fproj/pipelines/8/cancel", None)),
    (["pipeline-create", PROJ, "master", "--var", "A=1"],
     ("POST", "/projects/g%2Fs%2Fproj/pipeline", {"ref": "master", "variables": [{"key": "A", "value": "1"}]})),
    (["branch-create", PROJ, "f/x", "--ref", "master"],
     ("POST", "/projects/g%2Fs%2Fproj/repository/branches", {"branch": "f/x", "ref": "master"})),
    (["branch-delete", PROJ, "f/x"], ("DELETE", "/projects/g%2Fs%2Fproj/repository/branches/f%2Fx", None)),
    (["tag-create", PROJ, "v1", "--ref", "master", "--message", "m"],
     ("POST", "/projects/g%2Fs%2Fproj/repository/tags", {"tag_name": "v1", "ref": "master", "message": "m"})),
    (["tag-delete", PROJ, "v1"], ("DELETE", "/projects/g%2Fs%2Fproj/repository/tags/v1", None)),
])
def test_writers_send_expected_rest_call(argv, call, capsys):
    c = RecClient({})
    assert gl_api.main(argv, c) == 0
    assert c.calls == [call]
    assert capsys.readouterr().out.strip()


def test_mr_create_draft_and_description_file(tmp_path, capsys):
    f = tmp_path / "d.md"
    f.write_text("Why/What")
    c = RecClient({})
    gl_api.main(["mr-create", PROJ, "--source", "feature/x", "--title", "feat: x", "--description-file", str(f),
                 "--draft"], c)
    _, path, body = c.calls[0]
    assert path.endswith("/merge_requests") and body["title"] == "Draft: feat: x"
    assert body["description"] == "Why/What" and body["target_branch"] == "master"


def test_mr_comment_requires_body_and_explicit_mr(capsys):
    c = RecClient({})
    assert gl_api.main(["mr-comment", f"{PROJ}!5"], c) == 1
    assert gl_api.main(["mr-merge", PROJ], c) == 1
    assert c.calls == []


def test_short_discussion_id_expanded_and_ambiguity_refused():
    """`threads` prints 8-char ids; writers must expand them to the full id (and refuse ambiguous prefixes)."""
    full = "a" * 40
    c = RecClient({"/discussions": [{"id": full, "notes": []}, {"id": "b" * 40, "notes": []}]})
    assert gl_api.main(["thread-resolve", f"{PROJ}!5", "aaaaaaaa"], c) == 0
    assert c.calls[-1][1].endswith(f"/discussions/{full}")
    c2 = RecClient({"/discussions": [{"id": "ab" + "1" * 38, "notes": []}, {"id": "ab" + "2" * 38, "notes": []}]})
    assert gl_api.main(["mr-comment", f"{PROJ}!5", "--body", "x", "--reply", "ab"], c2) == 1
    assert c2.calls == ["/projects/g%2Fs%2Fproj/merge_requests/5/discussions"]


def test_pipeline_create_bad_var():
    assert gl_api.main(["pipeline-create", PROJ, "master", "--var", "oops"], RecClient({})) == 1


def test_read_helpers(capsys):
    c = FakeClient({"/changes": {"changes": [{"new_file": True, "deleted_file": False, "renamed_file": False,
                                              "new_path": "a"}]},
                    "/lint": {"valid": False, "errors": ["bad"], "warnings": ["w", "w"]},
                    "/notes/4": {"id": 4, "author": {"username": "u"}, "created_at": "2026-01-01T00:00:00", "body": "b"},
                    "/compare": {"commits": [1], "diffs": [{"new_file": False, "deleted_file": False, "new_path": "z"}]}})
    for argv, needle in [(["mr-files", f"{PROJ}!5"], "A a"), (["ci-lint", PROJ], "ERR bad"),
                         (["note-get", f"{PROJ}!5", "4"], "@u"), (["branch-diffs", PROJ, "a", "b"], "1 commits, 1 files")]:
        assert gl_api.main(argv, c) == 0
        assert needle in capsys.readouterr().out


def test_fetch_returns_one_furl_card_json_per_kind(capsys):
    """Link cards come from the CLI `fetch` (no bot-side GitLab code): title/url/status/emoji/fields."""
    class C(FakeClient):
        def request(self, method, path, params=None, body=None):
            if path.endswith("/approvals"):
                return {"approved": True}
            return {"title": "T", "state": "merged", "status": "failed", "web_url": "https://x/u", "name": "n",
                    "head_pipeline": {"status": "success"}, "author": {"username": "jo"}}
    ident = json.dumps({"project": PROJ, "id": "5"})
    for kind, emoji in (("merge_request", "🟣"), ("pipeline", "❌"), ("job", "❌")):
        assert gl_api.main(["fetch", "--kind", kind, "--ident", ident], C({})) == 0
        card = json.loads(capsys.readouterr().out)
        assert card["url"] == "https://x/u" and card["status_emoji"] == emoji
