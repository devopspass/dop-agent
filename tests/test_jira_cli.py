"""Jira package-driver CLI (drivers/jira/cli/cli.py): compact output, safe writes, generic config.

Nothing site-specific: base URL / email / token come from flags or JIRA_* env, story-point and sprint field ids
from options or auto-detection. Strict route-ordered fake of urllib so unexpected requests fail the test."""
import importlib.util, io, json, os, unittest, urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

_spec = importlib.util.spec_from_file_location("jira_cli", Path(__file__).resolve().parent.parent / "drivers/jira/cli/cli.py")
j = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(j)
j.SP_FIELD, j.SPRINT_FIELD = "customfield_10001", "customfield_10002"

TOKEN = "SECRET-TOKEN-123"
ENV = {"JIRA_BASE_URL": "https://x.example.net", "JIRA_EMAIL": "a@b.c", "JIRA_API_TOKEN": TOKEN,
       "JIRA_SP_FIELD": j.SP_FIELD, "JIRA_SPRINT_FIELD": j.SPRINT_FIELD}


class Resp:
    def __init__(self, data):
        self.b = json.dumps(data).encode()

    def read(self):
        return self.b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def run(argv, routes):
    """routes: list of (method, path-substring, response) consumed in order; returns (stdout, requests)."""
    reqs, queue = [], list(routes)

    def fake(req, timeout=0):
        path = req.full_url.split("example.net")[1]
        reqs.append((req.get_method(), path, json.loads(req.data) if req.data else None))
        m, sub, resp = queue.pop(0)
        assert m == req.get_method() and sub in path, f"unexpected {req.get_method()} {path}, wanted {m} {sub}"
        if isinstance(resp, Exception):
            raise resp
        return Resp(resp)
    buf = io.StringIO()
    with mock.patch.dict(os.environ, ENV), mock.patch("urllib.request.urlopen", fake), redirect_stdout(buf):
        j.main(argv)
    assert not queue, f"unused routes {queue}"
    return buf.getvalue(), reqs


ISSUE = {"fields": {
    "summary": "Do thing", "status": {"name": "Backlog"}, "assignee": {"displayName": "Al"}, "issuetype": {"name": "Task"},
    "priority": {"name": "Medium"}, "updated": "2026-10-16T10:00:00", j.SP_FIELD: 3.0, j.SPRINT_FIELD: [{"name": "S25"}, {"name": "S26"}],
    "parent": {"key": "PRJ-9", "fields": {"summary": "Epic"}},
    "issuelinks": [{"type": {"inward": "relates to", "outward": "relates to"}, "inwardIssue": {"key": "PRJ-1", "fields": {"summary": "Old", "status": {"name": "Done"}}}}],
    "description": {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Why " + "x" * 400}]}]},
    "comment": {"comments": [{"id": "1", "created": "2026-10-01T00:00", "author": {"displayName": "Bo"}, "body": {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "first"}]}]}},
                             {"id": "2", "created": "2026-10-02T00:00", "author": {"displayName": "Cy"}, "body": {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "second"}]}]}}]}}}


class Adf(unittest.TestCase):
    def test_roundtrip(self):
        t = "Hello **b** and `c` [l](https://x.io)\nline2\n\n- one\n- two\n\n```sh\nls -l\n```"
        d = j.text_to_adf(t)
        kinds = [n["type"] for n in d["content"]]
        self.assertEqual(kinds, ["paragraph", "bulletList", "codeBlock"])
        txt = j.adf_to_text(d)
        for s in ("Hello b and c l", "line2", "one", "two", "ls -l"):
            self.assertIn(s, txt)
        marks = [m["type"] for n in d["content"][0]["content"] for m in n.get("marks", [])]
        self.assertEqual(marks, ["strong", "code", "link"])

    def test_empty(self):
        self.assertTrue(j.text_to_adf("")["content"])


class Read(unittest.TestCase):
    def test_ticket(self):
        out, _ = run(["ticket", "PRJ-5", "--comments", "1", "--desc", "50"], [("GET", "/issue/PRJ-5", ISSUE)])
        self.assertIn("status=Backlog assignee=Al SP=3.0 sprint=S26", out)
        self.assertIn("epic=PRJ-9:Epic", out)
        self.assertIn("link relates to: PRJ-1 [Done] Old", out)
        self.assertIn("comments 2 (last 1):", out)
        self.assertIn("2 2026-10-02 Cy: second", out)
        self.assertNotIn("first", out)
        self.assertLess(len(out), 600)

    def test_ticket_no_desc(self):
        out, _ = run(["ticket", "PRJ-5", "--desc", "-1"], [("GET", "/issue/PRJ-5", ISSUE)])
        self.assertNotIn("desc:", out)

    def test_search(self):
        r = {"issues": [{"key": "PRJ-1", "fields": {"summary": "S", "status": {"name": "Backlog"}, j.SP_FIELD: 2.0}}], "nextPageToken": "t"}
        out, reqs = run(["search", "project=CB"], [("GET", "/search/jql", r)])
        self.assertIn("PRJ-1  Backlog | SP=2.0  S", out)
        self.assertIn("SP total 2", out)
        self.assertIn("Backlog=1", out)
        self.assertIn("+more", out)
        self.assertEqual(len(reqs), 1)

    def test_search_all_pages(self):
        a = {"issues": [{"key": "PRJ-1", "fields": {"summary": "A", "status": {"name": "x"}, j.SP_FIELD: 1}}], "nextPageToken": "t"}
        b = {"issues": [{"key": "PRJ-2", "fields": {"summary": "B", "status": {"name": "x"}, j.SP_FIELD: 1}}]}
        out, _ = run(["search", "q", "--all"], [("GET", "/search/jql", a), ("GET", "/search/jql", b)])
        self.assertIn("PRJ-2", out)
        self.assertIn("(2 issues, SP total 2", out)


class SearchFields(unittest.TestCase):
    def test_aliases_links_width(self):
        r = {"issues": [{"key": "PRJ-1", "fields": {"summary": "S" * 100, "status": {"name": "Done"}, j.SPRINT_FIELD: [{"name": "S26"}], "parent": {"key": "PRJ-9"}, "assignee": {"displayName": "Al"}}}]}
        out, reqs = run(["search", "q", "--fields", "summary,sprint,epic,assignee", "--links", "--width", "0"], [("GET", "/search/jql", r)])
        self.assertIn("https://x.example.net/browse/PRJ-1  S26 | PRJ-9 | Al  " + "S" * 100, out)
        self.assertEqual(reqs[0][1].count("summary"), 1)


class GroupBy(unittest.TestCase):
    def test_group_by_assignee(self):
        def it(k, who, sp):
            return {"key": k, "fields": {"summary": "x", "status": {"name": "Done"}, j.SP_FIELD: sp, "assignee": who}}
        r = {"issues": [it("PRJ-1", {"displayName": "Al"}, 2), it("PRJ-2", {"displayName": "Al"}, 1), it("PRJ-3", None, 0.5)]}
        out, reqs = run(["search", "q", "--group-by", "assignee"], [("GET", "/search/jql", r)])
        self.assertIn("Al  2 issues  SP=3", out)
        self.assertIn("unassigned  1 issues  SP=0.5", out)
        self.assertNotIn("PRJ-1", out.split("(")[0])
        self.assertIn("assignee", reqs[0][1])


class Transition(unittest.TestCase):
    T_BACKLOG = {"transitions": [{"id": "11", "name": "Selected for sprint", "to": {"name": "Ready to Sprint"}}, {"id": "141", "name": "Backlog", "to": {"name": "Backlog"}}]}
    T_READY = {"transitions": [{"id": "31", "name": "Ready to progress", "to": {"name": "In Progress"}}]}

    def test_direct_by_target(self):
        out, reqs = run(["transition", "PRJ-1", "in progress"], [("GET", "/transitions", self.T_READY), ("POST", "/transitions", {})])
        self.assertEqual(reqs[1][2], {"transition": {"id": "31"}})

    def test_dry_run_sends_nothing(self):
        out, reqs = run(["transition", "PRJ-1", "31", "--dry-run"], [("GET", "/transitions", self.T_READY)])
        self.assertEqual(len(reqs), 1)
        self.assertIn("DRY-RUN POST", out)

    def test_no_direct_without_chain(self):
        with self.assertRaises(SystemExit) as c:
            run(["transition", "PRJ-1", "In Progress"], [("GET", "/transitions", self.T_BACKLOG)])
        self.assertIn("use --chain", str(c.exception))

    def test_chain(self):
        routes = [("GET", "/transitions", self.T_BACKLOG), ("GET", "/transitions", self.T_BACKLOG),
                  ("POST", "/transitions", {}), ("GET", "/transitions", self.T_READY), ("POST", "/transitions", {}),
                  ("GET", "/issue/PRJ-1", {"fields": {"status": {"name": "In Progress"}}})]
        out, reqs = run(["transition", "PRJ-1", "In Progress", "--chain"], routes)
        self.assertEqual([r[2]["transition"]["id"] for r in reqs if r[0] == "POST"], ["11", "31"])
        self.assertIn("status now: In Progress", out)

    def test_ambiguous(self):
        t = {"transitions": [{"id": "1", "name": "A", "to": {"name": "X"}}, {"id": "2", "name": "B", "to": {"name": "X"}}]}
        with self.assertRaises(SystemExit):
            run(["transition", "PRJ-1", "X"], [("GET", "/transitions", t)])


class Write(unittest.TestCase):
    def test_comment(self):
        out, reqs = run(["comment", "PRJ-1", "hi **there**"], [("POST", "/issue/PRJ-1/comment", {"id": "77", "huge": "x" * 9999})])
        self.assertEqual(out.strip(), "OK comment 77")
        self.assertEqual(reqs[0][2]["body"]["type"], "doc")

    def test_sp_dry_and_real(self):
        out, reqs = run(["sp", "PRJ-1", "3", "--dry-run"], [])
        self.assertEqual(reqs, [])
        self.assertIn(j.SP_FIELD, out)
        _, reqs = run(["sp", "PRJ-1", "3"], [("PUT", "/issue/PRJ-1", {})])
        self.assertEqual(reqs[0][2], {"fields": {j.SP_FIELD: 3.0}})

    def test_assign_me(self):
        _, reqs = run(["assign", "PRJ-1", "me"], [("GET", "/myself", {"accountId": "acc"}), ("PUT", "/assignee", {})])
        self.assertEqual(reqs[1][2], {"accountId": "acc"})

    def test_assign_ambiguous(self):
        with self.assertRaises(SystemExit):
            run(["assign", "PRJ-1", "al"], [("GET", "/user/search", [{"displayName": "A", "accountId": "1"}, {"displayName": "B", "accountId": "2"}])])

    def test_link(self):
        _, reqs = run(["link", "PRJ-1", "Blocks", "PRJ-2"], [("POST", "/issueLink", {})])
        self.assertEqual(reqs[0][2]["outwardIssue"], {"key": "PRJ-1"})

    def test_create_with_sprint(self):
        out, reqs = run(["create", "--project", "CB", "--type", "Task", "--summary", "S", "--desc", "d", "--parent", "PRJ-9", "--sp", "2", "--sprint", "55"],
                        [("POST", "/issue", {"key": "PRJ-100"}), ("PUT", "/issue/PRJ-100", {})])
        self.assertIn("OK PRJ-100", out)
        self.assertEqual(reqs[0][2]["fields"]["parent"], {"key": "PRJ-9"})
        self.assertEqual(reqs[1][2], {"fields": {j.SPRINT_FIELD: 55}})

    def test_create_dry(self):
        out, reqs = run(["create", "--project", "CB", "--type", "Task", "--summary", "S", "--sprint", "5", "--dry-run"], [])
        self.assertEqual(reqs, [])


class Raw(unittest.TestCase):
    def test_truncate(self):
        out, _ = run(["raw", "GET", "/rest/api/3/x", "--max", "50"], [("GET", "/x", {"a": "y" * 500})])
        self.assertIn("…[+", out)

    def test_jq(self):
        out, _ = run(["raw", "GET", "/rest/api/3/x", "--jq", "issues[*].key"], [("GET", "/x", {"issues": [{"key": "A"}, {"key": "B"}]})])
        self.assertEqual(json.loads(out), ["A", "B"])

    def test_jq_multi(self):
        self.assertEqual(j.project({"a": {"b": 1}, "c": 2}, "a.b,c"), {"a.b": 1, "c": 2})


class Security(unittest.TestCase):
    def test_token_not_in_error(self):
        err = urllib.error.HTTPError("u", 401, "no", {}, io.BytesIO(("bad " + j.base64.b64encode(f"a@b.c:{TOKEN}".encode()).decode()).encode()))
        with self.assertRaises(SystemExit) as c:
            run(["ticket", "PRJ-1"], [("GET", "/issue", err)])
        self.assertNotIn(TOKEN, str(c.exception))
        self.assertIn("HTTP 401", str(c.exception))

    def test_no_creds(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("builtins.open", side_effect=OSError):
            with self.assertRaises(SystemExit):
                j.creds()


class DriverContract(unittest.TestCase):
    """`fetch` (furl card) and `test` (Settings Test button) print the JSON dop-agent's PackageDriver expects."""

    def test_fetch_prints_card_json_for_a_ticket(self):
        r = {"fields": {"summary": "S", "status": {"name": "Done"}, "assignee": {"displayName": "Al"}}}
        out, _ = run(["fetch", "--kind", "ticket", "--ident", '{"key": "PRJ-1"}'], [("GET", "/issue/PRJ-1", r)])
        d = json.loads(out)
        self.assertEqual((d["title"], d["status"], d["url"]), ("S", "Done", "https://x.example.net/browse/PRJ-1"))
        self.assertEqual(d["fields"]["assignee"], "Al")

    def test_fetch_rejects_a_malformed_key_without_calling_jira(self):
        with self.assertRaises(SystemExit):
            run(["fetch", "--kind", "ticket", "--ident", '{"key": "../x"}'], [])

    def test_test_reports_each_step_and_the_identity(self):
        out, _ = run(["test"], [("GET", "/myself", {"displayName": "Al"})])
        d = json.loads(out)
        self.assertTrue(d["ok"])
        self.assertEqual([s["name"] for s in d["steps"]], ["config", "auth", "fields"])
        self.assertIn("Al", d["message"])

    def test_test_fails_cleanly_when_auth_fails(self):
        err = urllib.error.HTTPError("u", 401, "no", {}, io.BytesIO(b"nope"))
        out, _ = run(["test"], [("GET", "/myself", err)])
        d = json.loads(out)
        self.assertFalse(d["ok"])
        self.assertIn("HTTP 401", d["message"])

    def test_token_is_read_from_the_token_file_not_the_command_line(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".tok") as f:
            f.write("FILE-TOKEN\n")
            f.flush()
            j.configure("https://x.example.net", "a@b.c", f.name)
            with mock.patch.dict(os.environ, {}, clear=True):
                _, auth = j.creds()
        self.assertEqual(auth, "Basic " + j.base64.b64encode(b"a@b.c:FILE-TOKEN").decode())

    def test_story_point_and_sprint_fields_are_autodetected_when_not_configured(self):
        fields = [{"id": "customfield_7", "name": "Story Points", "custom": True},
                  {"id": "customfield_8", "name": "Sprint", "custom": True, "schema": {"custom": "com.pyxis.greenhopper.jira:gh-sprint"}}]
        with mock.patch.dict(os.environ, {k: v for k, v in ENV.items() if "FIELD" not in k}), \
                mock.patch("urllib.request.urlopen", lambda req, timeout=0: Resp(fields)):
            j.configure()
            self.assertEqual((j.sp_field(), j.sprint_field()), ("customfield_7", "customfield_8"))


if __name__ == "__main__":
    unittest.main()
