#!/usr/bin/env python3
"""Compact ServiceNow (ITSM) CLI for dop-agent (terse, LLM-friendly output). Part of the `servicenow` package driver.

Connection: --instance NAME (or SNOW_INSTANCE). base_url, options.username and auth.token are read from config.yaml
(integrations.<NAME>, driver servicenow); auth.token is a ${env:X}/${file:P}/${keychain:S} ref, never printed.
NUMBER is a ticket number (INC0010001, CHG0030001, RITM..., REQ..., SCTASK..., CTASK..., TASK..., PRB...).

Read:
  incidents | changes | requests | ritms | tasks | list --table T
       [--mine] [--opened-by-me] [--group NAME|SYS_ID] [--state LABEL|VALUE] [--all] [--days N] [--query ENCODED] [--limit 20]
       (default: active records only; --all includes closed)
  cutover [--days 14] [--group G]            changes whose planned start is within the next N days (schedule / cutover windows)
  ticket NUMBER [--comments 3]               fields (+ change schedule/risk/approval) and last comments/work notes
  comments NUMBER [--limit 10] [--kind all|comments|work_notes]
  approvals NUMBER | attachments NUMBER     approver rows / attachment list (names and sizes only)
  groups [--search TEXT] [--limit 20]        assignment groups
  query TABLE --query ENCODED [--fields a,b] [--limit 20] [--order -sys_updated_on]   generic read-only Table API query
Write (draft only: prints what would be sent until --confirm is added):
  comment NUMBER "text" | --file f [--work-note] [--confirm]
  transition NUMBER STATE [--note TEXT] [--confirm]      STATE = label (e.g. "Resolved") or numeric value
  create-incident --short S [--desc D] [--caller USER] [--group G] [--urgency 1-3] [--impact 1-3] [--confirm]
  create-change --short S [--type normal|standard|emergency] [--desc D] [--group G] [--start 'YYYY-MM-DD HH:MM:SS']
                [--end '...'] [--confirm]
Other:
  fetch --kind ticket --ident '{"table":"incident","sys_id":"<32 hex>"}'   (furl card JSON, used by dop-agent)
  test                                                                      (connection check JSON, Settings Test button)
"""
import argparse
import datetime
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import snowapi as api  # noqa: E402
from snowapi import ApiError, disp, val  # noqa: E402

PREFIX_TABLE = {"INC": "incident", "CHG": "change_request", "RITM": "sc_req_item", "REQ": "sc_request", "SCTASK": "sc_task",
                "CTASK": "change_task", "TASK": "task", "PRB": "problem", "INCTASK": "incident_task", "PTASK": "problem_task"}
NUMBER_RE = re.compile(r"([A-Z]+)(\d{5,})")
LIST_FIELDS = ["number", "short_description", "state", "priority", "assigned_to", "assignment_group", "sys_updated_on", "sys_id",
               "type", "risk", "approval", "start_date", "end_date"]
ME = "javascript:gs.getUserID()"  # resolved server-side to the authenticated user; only used internally
UTC_FMT = "%Y-%m-%d %H:%M:%S"


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def stamp(dt):
    return dt.strftime(UTC_FMT)


def short(s, n):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def ref(number):
    m = NUMBER_RE.fullmatch((number or "").strip().upper())
    if not m or m.group(1) not in PREFIX_TABLE:
        raise ApiError(f"unknown ticket number {number!r}; expected one of {', '.join(sorted(PREFIX_TABLE))} + digits")
    return PREFIX_TABLE[m.group(1)], m.group(0)


def find(number, fields=None):
    table, num = ref(number)
    rows = api.table_list(table, "number=" + num, fields, 1)
    if not rows:
        raise ApiError(f"{num} not found in {table} (or not visible to this user)")
    return table, rows[0]


def link(table, sys_id):
    return f"{api.CONF['base_url']}/{table}.do?sys_id={sys_id}"


def line(r):
    when = disp(r.get("sys_updated_on"))
    extra = []
    if r.get("start_date") and disp(r["start_date"]):
        extra.append(f"{disp(r['start_date'])}→{disp(r.get('end_date'))}")
    if r.get("approval") and disp(r["approval"]):
        extra.append(f"approval={disp(r['approval'])}")
    return " | ".join([disp(r.get("number")), disp(r.get("state")) or "-", "P" + str(val(r.get("priority")) or "-"),
                       disp(r.get("assigned_to")) or "unassigned", disp(r.get("assignment_group")) or "-", when]
                      + extra + [short(disp(r.get("short_description")), 90)])


def group_clause(g, field="assignment_group"):
    g = api.quote_value(g)
    return f"{field}={g}" if api.SYS_ID_RE.fullmatch(g) else f"{field}.name={g}"


def state_value(table, text):
    """Accept a numeric state or a label (resolved through sys_choice, also for task-derived tables)."""
    if str(text).isdigit():
        return str(text)
    label = api.quote_value(text)
    for name in (table, "task"):
        rows = api.table_list("sys_choice", f"name={name}^element=state^inactive=false^label={label}", ["value", "label"], 1, display="false")
        if rows:
            return str(val(rows[0]["value"]))
    rows = api.table_list("sys_choice", f"name={table}^element=state^inactive=false", ["value", "label"], 30, display="false")
    raise ApiError(f"unknown state {text!r} for {table}; choices: " + ", ".join(f"{val(r['label'])}={val(r['value'])}" for r in rows))


def build_query(a, table):
    parts = []
    if not a.all:
        parts.append("active=true")
    if a.mine:
        parts.append("assigned_to=" + ME)
    if a.opened_by_me:
        parts.append("opened_by=" + ME)
    if a.group:
        parts.append(group_clause(a.group))
    if a.state:
        parts.append("state=" + state_value(table, a.state))
    if a.days:
        parts.append("sys_updated_on>=" + stamp(utcnow() - datetime.timedelta(days=a.days)))
    if a.query:
        parts.append(api.check_user_query(a.query))
    return "^".join(parts)


def cmd_list(a, table=None):
    table = api.check_table(table or a.table)
    rows = api.table_list(table, build_query(a, table), LIST_FIELDS, a.limit, order="-sys_updated_on")
    print(f"{table}: {len(rows)} row(s)" + (" (limit reached)" if len(rows) >= a.limit else ""))
    for r in rows:
        print(line(r))


def cmd_cutover(a):
    now = utcnow()
    q = f"active=true^start_date>={stamp(now)}^start_date<={stamp(now + datetime.timedelta(days=a.days))}"
    if a.group:
        q += "^" + group_clause(a.group)
    rows = api.table_list("change_request", q, LIST_FIELDS, a.limit, order="start_date")
    print(f"changes starting within {a.days} day(s): {len(rows)}")
    for r in rows:
        print(line(r))


def journal(sys_id, limit, kind="all"):
    elements = {"all": "elementINcomments,work_notes", "comments": "element=comments", "work_notes": "element=work_notes"}[kind]
    return api.table_list("sys_journal_field", f"element_id={sys_id}^{elements}", ["element", "value", "sys_created_by", "sys_created_on"],
                          limit, order="-sys_created_on")


def print_journal(rows):
    for j in rows:
        kind = "note" if val(j.get("element")) == "work_notes" else "comment"
        print(f"[{disp(j.get('sys_created_on'))}] {kind} by {disp(j.get('sys_created_by'))}: {short(disp(j.get('value')), 600)}")


def cmd_ticket(a):
    table, num = ref(a.number)
    _, r = find(a.number)
    full = api.table_get(table, val(r["sys_id"]))
    print(f"{num} ({table}) {link(table, val(r['sys_id']))}")
    for k, v in full.items():
        d = short(disp(v), 1500)
        if d and k not in ("sys_id", "sys_mod_count", "sys_tags") and k in DETAIL_FIELDS:
            print(f"{k}: {d}")
    if a.comments:
        print_journal(journal(val(r["sys_id"]), a.comments))


DETAIL_FIELDS = {"short_description", "description", "state", "priority", "urgency", "impact", "severity", "category", "subcategory",
                 "caller_id", "opened_by", "opened_at", "assigned_to", "assignment_group", "approval", "type", "risk", "chg_model",
                 "start_date", "end_date", "work_start", "work_end", "requested_by_date", "justification", "implementation_plan",
                 "backout_plan", "test_plan", "cmdb_ci", "business_service", "parent", "request", "cat_item", "requested_for",
                 "close_code", "close_notes", "resolved_at", "closed_at", "sys_updated_on", "sys_updated_by", "on_hold_reason"}


def cmd_comments(a):
    _, r = find(a.number, ["sys_id"])
    print_journal(journal(val(r["sys_id"]), a.limit, a.kind))


def cmd_approvals(a):
    _, r = find(a.number, ["sys_id"])
    rows = api.table_list("sysapproval_approver", "sysapproval=" + val(r["sys_id"]), ["approver", "state", "comments", "sys_updated_on"], 50,
                          order="sys_created_on")
    print(f"{a.number.upper()}: {len(rows)} approval row(s)")
    for x in rows:
        print(f"{disp(x.get('approver'))} | {disp(x.get('state'))} | {disp(x.get('sys_updated_on'))} | {short(disp(x.get('comments')), 120)}")


def cmd_attachments(a):
    _, r = find(a.number, ["sys_id"])
    rows = api.table_list("sys_attachment", "table_sys_id=" + val(r["sys_id"]), ["file_name", "size_bytes", "content_type", "sys_created_by",
                                                                                "sys_created_on", "sys_id"], 50, order="-sys_created_on")
    print(f"{a.number.upper()}: {len(rows)} attachment(s) (listing only; download is not supported)")
    for x in rows:
        print(f"{disp(x.get('file_name'))} | {disp(x.get('size_bytes'))} B | {disp(x.get('content_type'))} | {disp(x.get('sys_created_by'))} | "
              f"{disp(x.get('sys_created_on'))} | id={val(x.get('sys_id'))}")


def cmd_groups(a):
    q = ("active=true^nameLIKE" + api.quote_value(a.search)) if a.search else "active=true"
    rows = api.table_list("sys_user_group", q, ["name", "description", "manager", "sys_id"], a.limit, order="name")
    for g in rows:
        print(f"{disp(g.get('name'))} | manager={disp(g.get('manager')) or '-'} | id={val(g.get('sys_id'))} | {short(disp(g.get('description')), 80)}")


def cmd_query(a):
    fields = [f for f in (a.fields or "").split(",") if f]
    rows = api.table_list(a.table, api.check_user_query(a.query), fields or None, a.limit, order=a.order)
    print(f"{a.table}: {len(rows)} row(s)")
    for r in rows:
        print(json.dumps({k: disp(v) for k, v in r.items()}, ensure_ascii=False)[:1500])


def draft(confirm, what, table, payload, sys_id=None):
    """Print the draft; perform the write only with --confirm. Returns True if the caller must send."""
    body = json.dumps(payload, ensure_ascii=False)
    print(("SENDING" if confirm else "DRAFT (nothing sent; re-run with --confirm)") + f": {what} on {table}" + (f" {sys_id}" if sys_id else ""))
    print(short(body, 1500))
    return confirm


def cmd_comment(a):
    text = open(a.file).read() if a.file else a.text
    if not text or not text.strip():
        raise ApiError("comment text is required (argument or --file)")
    table, r = find(a.number, ["sys_id"])
    field = "work_notes" if a.work_note else "comments"
    if draft(a.confirm, f"{'work note (internal)' if a.work_note else 'customer-visible comment'} for {a.number.upper()}", table, {field: text}):
        api.table_update(table, val(r["sys_id"]), {field: text})
        print("done: " + link(table, val(r["sys_id"])))


def cmd_transition(a):
    table, r = find(a.number, ["sys_id", "state"])
    payload = {"state": state_value(table, a.state)}
    if a.note:
        payload["work_notes"] = a.note
    print(f"current state: {disp(r.get('state'))}")
    if draft(a.confirm, f"state transition of {a.number.upper()} to {a.state!r}", table, payload):
        res = api.table_update(table, val(r["sys_id"]), payload)
        print(f"done: state is now {disp(res.get('state'))} (business rules may reject or adjust a transition)")


def put(payload, key, value):
    if value not in (None, ""):
        payload[key] = value


def lookup_sys_id(table, field, value):
    """sys_id of the record whose `field` equals `value` (a 32-hex value is taken as the sys_id itself)."""
    if api.SYS_ID_RE.fullmatch(value):
        return value
    rows = api.table_list(table, f"{field}={api.quote_value(value)}", ["sys_id"], 2, display="false")
    if len(rows) != 1:
        raise ApiError(f"{table} {field}={value!r}: " + ("not found" if not rows else "ambiguous, pass the sys_id"))
    return str(val(rows[0]["sys_id"]))


def cmd_create(table, required):
    def run(a):
        payload = {"short_description": a.short}
        put(payload, "description", a.desc)
        if a.group:
            payload["assignment_group"] = lookup_sys_id("sys_user_group", "name", a.group)
        for k in required:
            v = getattr(a, k, None)
            if k.endswith("_date") and v and not re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", v):
                raise ApiError(f"--{k[:-5]} must look like 'YYYY-MM-DD HH:MM:SS' (UTC)")
            put(payload, k, v)
        if table == "incident":
            if a.caller:
                payload["caller_id"] = lookup_sys_id("sys_user", "user_name", a.caller)
        if draft(a.confirm, "create", table, payload):
            res = api.table_create(table, payload)
            print(f"created {disp(res.get('number'))}: {link(table, val(res.get('sys_id')))}")
    return run


def cmd_fetch(a):
    ident = json.loads(a.ident)
    table, sys_id = api.check_table(ident.get("table", "")), ident.get("sys_id", "")
    r = api.table_get(table, sys_id, ["number", "short_description", "state", "assigned_to", "sys_id"])
    summary, status = disp(r.get("short_description")), disp(r.get("state"))
    print(json.dumps({"title": summary, "status": status, "url": link(table, sys_id),
                      "fields": {"number": disp(r.get("number")), "summary": summary, "status": status, "assignee": disp(r.get("assigned_to"))}}))


def cmd_test(a):
    steps = []
    api.TIMEOUT = 15

    def step(name, fn, optional=False):
        try:
            steps.append({"name": name, "ok": True, "message": fn()})
            return True
        except ApiError as e:
            hint = " — check auth.token scope/expiry and the user's REST roles" if e.code in (401, 403) else ""
            steps.append({"name": name, "ok": False, "optional": optional, "message": api.redact(e)[:200] + hint})
            return optional

    def conf():
        api._auth()
        return f"{api.CONF['base_url']} ({api.CONF['auth_type']})"

    def auth():
        if api.CONF["auth_type"] == "basic":
            rows = api.table_list("sys_user", "user_name=" + api.quote_value(api.CONF["username"]), ["name", "user_name"], 1)
            if not rows:
                raise ApiError("authenticated, but the user record is not readable")
            return disp(rows[0].get("name")) or api.CONF["username"]
        api.table_list("sys_user", "", ["sys_id"], 1)
        return "token accepted"

    def table_step(table, label):
        return lambda: f"{label}: {len(api.table_list(table, '', ['sys_id'], 1))}+ row visible"

    ok = step("config", conf) and step("auth", auth)
    if ok:
        step("incident", table_step("incident", "incidents"), optional=True)
        step("change", table_step("change_request", "changes"), optional=True)
    failed = [s for s in steps if not s["ok"]]
    msg = next((s["message"] for s in steps if not s["ok"] and not s.get("optional")), "")
    if ok:
        msg = f"authenticated ({next(s['message'] for s in steps if s['name'] == 'auth')})" + (
            " — " + ", ".join(f"{s['name']} not readable" for s in failed) if failed else "")
    print(json.dumps({"ok": ok, "message": msg, "steps": steps}))


def build():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instance", default=os.environ.get("SNOW_INSTANCE"))
    sp = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, *args):
        p = sp.add_parser(name)
        for x in args:
            p.add_argument(x)
        p.set_defaults(fn=fn)
        return p

    def lister(name, table):
        p = add(name, (lambda a, t=table: cmd_list(a, t)))
        if table is None:
            p.add_argument("--table", required=True)
        p.add_argument("--mine", action="store_true")
        p.add_argument("--opened-by-me", action="store_true")
        p.add_argument("--group")
        p.add_argument("--state")
        p.add_argument("--all", action="store_true")
        p.add_argument("--days", type=int)
        p.add_argument("--query")
        p.add_argument("--limit", type=int, default=20)
    for n, t in (("incidents", "incident"), ("changes", "change_request"), ("requests", "sc_request"), ("ritms", "sc_req_item"),
                 ("tasks", "task"), ("list", None)):
        lister(n, t)
    p = add("cutover", cmd_cutover)
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--group")
    p.add_argument("--limit", type=int, default=50)
    p = add("ticket", cmd_ticket, "number")
    p.add_argument("--comments", type=int, default=0)
    p = add("comments", cmd_comments, "number")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--kind", choices=["all", "comments", "work_notes"], default="all")
    add("approvals", cmd_approvals, "number")
    add("attachments", cmd_attachments, "number")
    p = add("groups", cmd_groups)
    p.add_argument("--search")
    p.add_argument("--limit", type=int, default=20)
    p = add("query", cmd_query, "table")
    p.add_argument("--query", required=True)
    p.add_argument("--fields")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--order")
    p = add("comment", cmd_comment, "number")
    p.add_argument("text", nargs="?")
    p.add_argument("--file")
    p.add_argument("--work-note", action="store_true")
    p = add("transition", cmd_transition, "number", "state")
    p.add_argument("--note")
    for name, table, extra in (("create-incident", "incident", ["urgency", "impact"]),
                               ("create-change", "change_request", ["type", "start_date", "end_date"])):
        p = add(name, cmd_create(table, extra))
        p.add_argument("--short", required=True)
        p.add_argument("--desc")
        p.add_argument("--group")
        if table == "incident":
            p.add_argument("--caller")
            p.add_argument("--urgency", choices=["1", "2", "3"])
            p.add_argument("--impact", choices=["1", "2", "3"])
        else:
            p.add_argument("--type", choices=["normal", "standard", "emergency"])
            p.add_argument("--start", dest="start_date")
            p.add_argument("--end", dest="end_date")
    for p in sp.choices.values():
        if p.prog.split()[-1] in ("comment", "transition", "create-incident", "create-change"):
            p.add_argument("--confirm", action="store_true")
    p = add("fetch", cmd_fetch)
    p.add_argument("--kind", required=True)
    p.add_argument("--ident", required=True)
    add("test", cmd_test)
    return ap


def main(argv=None):
    a = build().parse_args(argv)
    try:
        try:
            api.configure(a.instance)
        except ApiError as e:
            if a.cmd != "test":
                raise
            print(json.dumps({"ok": False, "message": str(e)[:200], "steps": [{"name": "config", "ok": False, "message": str(e)[:200]}]}))
            return
        a.fn(a)
    except ApiError as e:
        sys.exit(api.redact(e))
    except BrokenPipeError:
        pass


if __name__ == "__main__":
    main()
