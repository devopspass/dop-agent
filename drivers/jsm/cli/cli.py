#!/usr/bin/env python3
"""Compact Jira Service Management (JSM) CLI for dop-agent (terse, LLM-friendly output). Part of the `jsm` package driver.

Connection: --instance NAME (or JSM_INSTANCE). base_url, options.email/cloud_id and auth.token are read from
config.yaml (integrations.<NAME>, driver jsm); auth.token is a ${env:X}/${file:P}/${keychain:S} ref, never printed.

Service desk (requests, queues, SLAs, approvals):
  desks | request-types DESK | request-type-fields DESK TYPE | queues DESK
  queue-issues DESK QUEUE [--limit 30]
  requests [--ownership ALL_REQUESTS|OWNED_REQUESTS|PARTICIPATED_REQUESTS|APPROVER] [--status OPEN_REQUESTS|CLOSED_REQUESTS|ALL_REQUESTS]
           [--desk ID] [--search TEXT] [--approval MY_PENDING_APPROVAL|MY_HISTORY] [--limit 30]
  request KEY [--comments 3]        sla KEY        approvals KEY        transitions KEY
  comment KEY "text" | --file f [--internal] [--dry-run]
  transition KEY <id|name> [--comment TEXT] [--dry-run]
  approve|decline KEY [APPROVAL_ID] [--dry-run]
  create --desk ID --type ID --summary S [--desc D] [--field fieldId=value ...] [--dry-run]
Ops (alerts, on-call):
  alerts [--query 'status: open'] [--limit 30]   alert ID
  alert-ack|alert-close ID [--note TEXT] [--dry-run]
  teams | schedules | oncall [--schedule ID|--name TEXT] [--date ISO] [--next]
Other:
  raw desk|ops|jira GET|POST|PUT|DELETE PATH [-q k=v] [--data JSON|--data-file f] [--jq a.b[*].c] [--max 4000]
  fetch --kind request --ident '{"key":"KEY"}'   (furl card JSON, used by dop-agent)
  test                                           (connection check JSON, used by the Settings Test button)
"""
import argparse
import base64
import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

CONF = {"base_url": "", "email": "", "cloud_id": ""}
_TOKEN = ""
_CACHE = {}
KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*-\d+")
ID_RE = re.compile(r"[A-Za-z0-9._:\-]{1,80}")
GUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def die(msg):
    sys.exit(msg)


class Http(Exception):
    def __init__(self, code, text):
        super().__init__(f"HTTP {code}: {text}")
        self.code, self.text = code, text


def use(base_url, email, token, cloud_id=""):
    """Set the connection directly (tests, embedding); the token stays in module state, never in CONF."""
    global _TOKEN
    CONF.update(base_url=base_url, email=email, cloud_id="" if (cloud_id or "").lower() == "auto" else (cloud_id or ""))
    _TOKEN = token
    _CACHE.clear()


def configure(instance=None):
    """Resolve base_url/email/token/cloud_id of the integration instance from config.yaml (gitlab-driver convention)."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import jsmconfig
    try:
        _, base, token, opts = jsmconfig.load_instance(instance)
    except jsmconfig.InstanceError as e:
        die(str(e))
    use(base, str(opts.get("email") or ""), token, str(opts.get("cloud_id") or ""))


def creds():
    base, email, tok = CONF["base_url"].rstrip("/"), CONF["email"], _TOKEN
    if not (base and email and tok):
        die("JSM is not configured: pass --instance <name> (integrations.<name> with driver jsm, base_url, options.email, auth.token)")
    if not re.match(r"^https?://", base):
        die("base URL must start with http(s)://")
    return base, "Basic " + base64.b64encode(f"{email}:{tok}".encode()).decode()


def _open(url, method, auth, data=None, headers=None):
    h = {"Authorization": auth, "Accept": "application/json", "Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, method=method, headers=h, data=json.dumps(data).encode() if data is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        raise Http(e.code, e.read().decode(errors="replace")[:300].replace(auth, "***"))
    except urllib.error.URLError as e:
        die(f"network error {method} {url.split('?')[0]}: {e.reason}")
    try:
        return json.loads(body) if body else {}
    except ValueError:
        return {}


def cloud_id():
    if CONF["cloud_id"]:
        if not GUID_RE.fullmatch(CONF["cloud_id"]):
            die("cloud id must be a GUID")
        return CONF["cloud_id"]
    if "cloud" not in _CACHE:
        base, _ = creds()
        try:
            with urllib.request.urlopen(base + "/_edge/tenant_info", timeout=20) as r:
                _CACHE["cloud"] = json.load(r).get("cloudId") or ""
        except (urllib.error.URLError, ValueError) as e:
            die(f"cannot detect cloud id from {base}/_edge/tenant_info ({e}); set the cloud_id option")
    if not GUID_RE.fullmatch(_CACHE["cloud"]):
        die("cloud id not found; set the cloud_id option")
    return _CACHE["cloud"]


ROOTS = {"desk": "/rest/servicedeskapi", "jira": "/rest/api/3"}


def call(api, method, path, query=None, data=None):
    """api: desk (service desk REST), jira (platform REST, user lookups), ops (JSM Ops REST on api.atlassian.com)."""
    base, auth = creds()
    if not path.startswith("/"):
        die("path must start with /")
    if api == "ops":
        url = f"https://api.atlassian.com/jsm/ops/api/{cloud_id()}/v1{path}"
    elif api in ROOTS:
        url = base + ROOTS[api] + path
    else:
        die(f"unknown api {api!r} (desk|ops|jira)")
    if query:
        url += "?" + urllib.parse.urlencode(query)
    try:
        return _open(url, method, auth, data, {"X-ExperimentalApi": "opt-in"} if api == "desk" else None)
    except Http as e:
        die(f"HTTP {e.code} {method} {api}:{path.split('?')[0]}: {e.text}")


def short(s, n):
    s = " ".join(str(s or "").split())
    return s if n <= 0 or len(s) <= n else s[:n] + "…"


def project(obj, path):
    def one(o, parts):
        if not parts:
            return o
        p, rest = parts[0], parts[1:]
        if p.endswith("[*]"):
            o = o.get(p[:-3]) if isinstance(o, dict) else None
            return [one(x, rest) for x in o] if isinstance(o, list) else None
        if isinstance(o, dict):
            return one(o.get(p), rest)
        if isinstance(o, list) and p.isdigit() and int(p) < len(o):
            return one(o[int(p)], rest)
        return None
    paths = [p for p in path.split(",") if p]
    if len(paths) == 1:
        return one(obj, paths[0].split("."))
    return {p: one(obj, p.split(".")) for p in paths}


def emit(s, mx=0):
    s = s if isinstance(s, str) else json.dumps(s, indent=1, ensure_ascii=False)
    if mx and len(s) > mx:
        s = s[:mx] + f"\n…[+{len(s) - mx} chars, use --max]"
    print(s)


def key_arg(k):
    if not KEY_RE.fullmatch(k or ""):
        die(f"bad request key {k!r} (expected like ABC-123)")
    return k.upper()


def id_arg(v, what="id"):
    if not ID_RE.fullmatch(str(v or "")):
        die(f"bad {what} {v!r}")
    return str(v)


def pages(api, path, query, limit, key="values"):
    """start/limit pagination of the service desk API; stops at `limit` items."""
    out, start = [], 0
    while len(out) < limit:
        r = call(api, "GET", path, dict(query or {}, start=start, limit=min(50, limit - len(out))))
        vals = r.get(key) or []
        out += vals
        if r.get("isLastPage", True) or not vals:
            break
        start += len(vals)
    return out[:limit]


def write(dry, api, method, path, data=None, ok=""):
    if dry:
        print(f"DRY-RUN {method} {api}:{path} {json.dumps(data, ensure_ascii=False) if data is not None else ''}".rstrip())
        return None
    r = call(api, method, path, data=data)
    print(f"OK {ok}".rstrip())
    return r


def text_arg(a):
    if getattr(a, "file", None):
        with open(os.path.expanduser(a.file)) as f:
            return f.read()
    return a.text or ""


# ---------- service desk ----------
def summary_of(r):
    return next((f.get("value") for f in r.get("requestFieldValues", []) if f.get("fieldId") == "summary"), "") or ""


def status_of(r):
    return (r.get("currentStatus") or {}).get("status") or "?"


def req_line(r, width=80):
    created = ((r.get("createdDate") or {}).get("iso8601") or "")[:16]
    return f"{r.get('issueKey')} [{status_of(r)}] {short(summary_of(r), width)} | {(r.get('reporter') or {}).get('displayName') or '?'} | {created}"


def cmd_desks(a):
    for d in pages("desk", "/servicedesk", None, 100):
        print(f"{d['id']} {d.get('projectKey', '')} {d.get('projectName', '')}")


def cmd_request_types(a):
    for t in pages("desk", f"/servicedesk/{id_arg(a.desk)}/requesttype", None, 100):
        print(f"{t['id']} {t.get('name', '')} | {short(t.get('description'), 80)}")


def cmd_request_type_fields(a):
    r = call("desk", "GET", f"/servicedesk/{id_arg(a.desk)}/requesttype/{id_arg(a.type)}/field")
    for f in r.get("requestTypeFields", []):
        print(f"{f['fieldId']} {'REQUIRED' if f.get('required') else 'optional'} {f.get('name', '')}")


def cmd_queues(a):
    for q in pages("desk", f"/servicedesk/{id_arg(a.desk)}/queue", {"includeCount": "true"}, 100):
        print(f"{q['id']} {q.get('name', '')} ({q.get('issueCount', '?')})")


def queue_issues(desk, queue, limit):
    return pages("desk", f"/servicedesk/{id_arg(desk)}/queue/{id_arg(queue)}/issue", None, limit)


def cmd_queue_issues(a):
    for i in queue_issues(a.desk, a.queue, a.limit):
        f = i.get("fields") or {}
        print(f"{i.get('key')} [{(f.get('status') or {}).get('name', '?')}] {short(f.get('summary'), 80)} | "
              f"{(f.get('assignee') or {}).get('displayName') or 'unassigned'}")


def list_requests(ownership="ALL_REQUESTS", status="OPEN_REQUESTS", desk=None, search=None, approval=None, limit=30):
    q = {"requestOwnership": ownership, "requestStatus": status, "expand": "status"}
    if desk:
        q["serviceDeskId"] = id_arg(desk)
    if search:
        q["searchTerm"] = search
    if approval:
        q["approvalStatus"] = approval
    return pages("desk", "/request", q, limit)


def cmd_requests(a):
    rows = list_requests(a.ownership, a.status, a.desk, a.search, a.approval, a.limit)
    for r in rows:
        print(req_line(r))
    print(f"-- {len(rows)} request(s)")


def cmd_request(a):
    k = key_arg(a.key)
    r = call("desk", "GET", f"/request/{k}", {"expand": "requestType,serviceDesk,status"})
    print(req_line(r, 200))
    print(f"type: {(r.get('requestType') or {}).get('name', '?')} | desk: {(r.get('serviceDesk') or {}).get('projectName', '?')}"
          f" | url: {creds()[0]}/browse/{k}")
    for f in r.get("requestFieldValues", []):
        v = f.get("value")
        if f.get("fieldId") != "summary" and v not in (None, "", []):
            print(f"{f.get('label') or f.get('fieldId')}: {short(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False), 300)}")
    if a.comments:
        cs = call("desk", "GET", f"/request/{k}/comment", {"limit": 100, "start": 0, "internal": "true"}).get("values", [])
        for c in cs[-a.comments:]:
            print(f"-- {(c.get('author') or {}).get('displayName', '?')} {(c.get('created') or {}).get('iso8601', '')[:16]} "
                  f"({'public' if c.get('public') else 'internal'}): {short(c.get('body'), 400)}")


def sla_rows(k):
    out = []
    for s in call("desk", "GET", f"/request/{k}/sla", {"limit": 50}).get("values", []):
        cyc = s.get("ongoingCycle")
        if cyc:
            rem = cyc.get("remainingTime") or {}
            out.append({"name": s.get("name", "?"), "state": "BREACHED" if cyc.get("breached") else ("paused" if cyc.get("paused") else "running"),
                        "remaining_ms": rem.get("millis"), "remaining": rem.get("friendly") or "?",
                        "goal": (cyc.get("goalDuration") or {}).get("friendly") or "?", "breach_time": (cyc.get("breachTime") or {}).get("iso8601", "")})
        elif s.get("completedCycles"):
            last = s["completedCycles"][-1]
            out.append({"name": s.get("name", "?"), "state": "breached(done)" if last.get("breached") else "met", "remaining_ms": None,
                        "remaining": "-", "goal": (last.get("goalDuration") or {}).get("friendly") or "?", "breach_time": ""})
    return out


def cmd_sla(a):
    for s in sla_rows(key_arg(a.key)):
        print(f"{s['name']}: {s['state']} | remaining {s['remaining']} | goal {s['goal']}" + (f" | breach {s['breach_time']}" if s["breach_time"] else ""))


def cmd_approvals(a):
    for ap in call("desk", "GET", f"/request/{key_arg(a.key)}/approval").get("values", []):
        who = ", ".join(f"{(x.get('approver') or {}).get('displayName', '?')}={x.get('approverDecision', '?')}" for x in ap.get("approvers", []))
        print(f"{ap['id']} {ap.get('name', '')} | {ap.get('finalDecision', '?')} | canAnswer={ap.get('canAnswerApproval')} | {who}")


def cmd_transitions(a):
    for t in call("desk", "GET", f"/request/{key_arg(a.key)}/transition").get("values", []):
        print(f"{t['id']} {t.get('name', '')}")


def cmd_comment(a):
    k = key_arg(a.key)
    body = text_arg(a)
    if not body.strip():
        die("comment text is empty")
    write(a.dry_run, "desk", "POST", f"/request/{k}/comment", {"body": body, "public": not a.internal},
          f"comment on {k} ({'internal' if a.internal else 'public'})")


def cmd_transition(a):
    k = key_arg(a.key)
    ts = call("desk", "GET", f"/request/{k}/transition").get("values", [])
    hit = [t for t in ts if t["id"] == a.target or t.get("name", "").lower() == a.target.lower()]
    if len(hit) != 1:
        die(f"transition {a.target!r} not unique/available for {k}; available: " + ", ".join(f"{t['id']}={t.get('name')}" for t in ts))
    data = {"id": hit[0]["id"]}
    if a.comment:
        data["additionalComment"] = {"body": a.comment}
    write(a.dry_run, "desk", "POST", f"/request/{k}/transition", data, f"{k} -> {hit[0].get('name')}")


def cmd_decide(decision):
    def run(a):
        k = key_arg(a.key)
        aps = [x for x in call("desk", "GET", f"/request/{k}/approval").get("values", []) if x.get("canAnswerApproval")]
        if a.approval_id:
            aps = [x for x in aps if str(x["id"]) == a.approval_id]
        if len(aps) != 1:
            die(f"need exactly one approval you can answer on {k}, found {len(aps)}; list with `approvals {k}` and pass APPROVAL_ID")
        write(a.dry_run, "desk", "POST", f"/request/{k}/approval/{aps[0]['id']}", {"decision": decision}, f"{decision} {k} ({aps[0].get('name')})")
    return run


def cmd_create(a):
    fields = {"summary": a.summary}
    if a.desc:
        fields["description"] = a.desc
    for kv in a.field:
        k, sep, v = kv.partition("=")
        if not sep or not re.fullmatch(r"[A-Za-z0-9_\-]{1,60}", k):
            die(f"--field needs fieldId=value, got {kv!r}")
        fields[k] = v
    r = write(a.dry_run, "desk", "POST", "/request",
              {"serviceDeskId": id_arg(a.desk), "requestTypeId": id_arg(a.type), "requestFieldValues": fields})
    if r:
        print(f"created {r.get('issueKey')} {creds()[0]}/browse/{r.get('issueKey')}")


# ---------- ops ----------
def alert_line(x):
    return (f"{x.get('tinyId', '?')} [{x.get('priority', '?')}] {short(x.get('message'), 100)} "
            f"(status={x.get('status', '?')}, created={x.get('createdAt', '?')}, id={x.get('id')})")


def fetch_alerts(query, limit):
    out, off = [], 0
    while len(out) < limit:
        size = min(100, limit - len(out))
        r = call("ops", "GET", "/alerts", {"query": query, "size": size, "offset": off, "sort": "createdAt", "order": "desc"})
        rows = r.get("values", r.get("data", []))
        out += rows
        if len(rows) < size:
            break
        off += size
    return out


def cmd_alerts(a):
    rows = fetch_alerts(a.query, a.limit)
    for x in rows:
        print(alert_line(x))
    print(f"-- {len(rows)} alert(s) for: {a.query}")


def cmd_alert(a):
    emit(call("ops", "GET", f"/alerts/{id_arg(a.id)}"), 4000)


def cmd_alert_act(verb):
    def run(a):
        write(a.dry_run, "ops", "POST", f"/alerts/{id_arg(a.id)}/{verb}", {"note": a.note or ""}, f"{verb} requested for {a.id}")
    return run


def values(r):
    return r if isinstance(r, list) else (r.get("values") or r.get("platformTeams") or [])


def cmd_teams(a):
    for t in values(call("ops", "GET", "/teams")):
        print(f"{t.get('id')} {t.get('name', '')}")


def list_schedules():
    out, off = [], 0
    while off < 500:
        vals = call("ops", "GET", "/schedules", {"size": 50, "offset": off}).get("values", [])
        out += vals
        if len(vals) < 50:
            break
        off += 50
    return out


def cmd_schedules(a):
    for s in list_schedules():
        print(f"{s.get('id')} {s.get('name', '')} | {'enabled' if s.get('enabled', True) else 'disabled'} | tz={s.get('timezone', '?')}")


def flatten(payload):
    for k in ("onCallRecipients", "onCallUsers", "nextOnCallRecipients", "nextOnCallUsers"):
        if k in payload:
            return [str(v) for v in payload[k]]
    out = []
    for k in ("onCallParticipants", "nextOnCallParticipants"):
        for p in payload.get(k, []):
            out.append(f"{p.get('type', '?')}:{p.get('id', '?')}")
            if p.get(k):
                out += flatten({k: p[k]})
    return out


def user_name(ident):
    if not re.fullmatch(r"[0-9a-zA-Z:\-]{6,80}", ident) or ident.startswith(("team:", "schedule:", "escalation:")):
        return ident
    names = _CACHE.setdefault("names", {})
    if ident not in names:
        try:
            names[ident] = call("jira", "GET", "/user", {"accountId": ident}).get("displayName") or ident
        except SystemExit:
            names[ident] = ident
    return names[ident]


def resolve_schedule(sid, name):
    if sid:
        return id_arg(sid), sid
    if not name:
        die("pass --schedule ID or --name TEXT (list: `schedules`)")
    hit = [s for s in list_schedules() if name.lower() in (s.get("name") or "").lower()]
    if len(hit) != 1:
        die(f"--name {name!r} matches {len(hit)} schedules: " + ", ".join(f"{s['id']}={s.get('name')}" for s in hit[:10]))
    return hit[0]["id"], hit[0].get("name")


def on_call(schedule_id, date=None, nxt=False):
    q = {"flat": "true"}
    if date:
        q["date"] = date
    return flatten(call("ops", "GET", f"/schedules/{schedule_id}/{'next-on-calls' if nxt else 'on-calls'}", q))


def norm_date(d):
    if not d:
        return None
    if re.fullmatch(r"\d{4}-\d\d-\d\d", d):
        d += "T12:00:00Z"
    try:
        dt = datetime.datetime.fromisoformat(d.replace("Z", "+00:00"))
    except ValueError:
        die(f"bad --date {d!r} (ISO 8601)")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cmd_oncall(a):
    sid, name = resolve_schedule(a.schedule, a.name)
    ids = on_call(sid, norm_date(a.date), a.next)
    print(f"{'next on call' if a.next else 'on call'} in {name} ({sid}){' at ' + a.date if a.date else ''}: "
          + (", ".join(user_name(i) for i in ids) or "nobody"))


# ---------- generic ----------
def cmd_raw(a):
    if a.data:
        data = json.loads(a.data)
    elif a.data_file:
        with open(os.path.expanduser(a.data_file)) as f:
            data = json.load(f)
    else:
        data = None
    q = dict(x.split("=", 1) for x in a.q) or None
    r = call(a.api, a.method.upper(), a.path, q, data)
    emit(project(r, a.jq) if a.jq else r, a.max)


def cmd_fetch(a):
    if a.kind not in ("request", "agent_request"):
        die(f"jsm has no kind {a.kind!r}")
    k = key_arg(json.loads(a.ident).get("key", ""))
    r = call("desk", "GET", f"/request/{k}", {"expand": "status"})
    status = status_of(r) if r.get("currentStatus") else ""
    summ = summary_of(r)
    print(json.dumps({"title": summ, "status": status, "url": f"{creds()[0]}/browse/{k}",
                      "fields": {"key": k, "summary": summ, "status": status, "reporter": (r.get("reporter") or {}).get("displayName") or ""}}))


def cmd_test(a):
    steps, state = [], {}

    def step(name, fn, optional=False):
        try:
            steps.append({"name": name, "ok": True, "message": fn()})
            return True
        except SystemExit as e:
            steps.append({"name": name, "ok": False, "optional": optional, "message": str(e.code)[:200]})
            return optional

    def conf():
        creds()
        return CONF["base_url"]

    def who():
        u = call("jira", "GET", "/myself")
        state["user"] = u.get("displayName") or u.get("emailAddress") or "?"
        return state["user"]

    def desks():
        return f"{len(pages('desk', '/servicedesk', None, 100))} service desk(s) visible"

    def ops():
        return f"Ops API ok (cloud {cloud_id()[:8]}…, {len(call('ops', 'GET', '/schedules', {'size': 1}).get('values', []))}+ schedule)"
    ok = step("config", conf) and step("auth", who) and step("service_desks", desks)
    if ok:
        step("ops", ops, optional=True)
    required = next((s["message"] for s in steps if not s["ok"] and not s.get("optional")), "")
    ops_down = any(not s["ok"] for s in steps)
    msg = (f"authenticated as {state['user']}" + (" (Ops alerts/on-call unavailable)" if ops_down else "")) if ok else required
    print(json.dumps({"ok": ok, "message": msg, "steps": steps}))


def build():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instance", default=os.environ.get("JSM_INSTANCE"))
    sp = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, *args, dry=False):
        p = sp.add_parser(name)
        for x in args:
            p.add_argument(x)
        if dry:
            p.add_argument("--dry-run", action="store_true")
        p.set_defaults(fn=fn)
        return p
    add("desks", cmd_desks)
    add("request-types", cmd_request_types, "desk")
    add("request-type-fields", cmd_request_type_fields, "desk", "type")
    add("queues", cmd_queues, "desk")
    p = add("queue-issues", cmd_queue_issues, "desk", "queue")
    p.add_argument("--limit", type=int, default=30)
    p = add("requests", cmd_requests)
    p.add_argument("--ownership", default="ALL_REQUESTS", choices=["ALL_REQUESTS", "OWNED_REQUESTS", "PARTICIPATED_REQUESTS", "APPROVER"])
    p.add_argument("--status", default="OPEN_REQUESTS", choices=["OPEN_REQUESTS", "CLOSED_REQUESTS", "ALL_REQUESTS"])
    p.add_argument("--approval", choices=["MY_PENDING_APPROVAL", "MY_HISTORY", "ALL"])
    p.add_argument("--desk")
    p.add_argument("--search")
    p.add_argument("--limit", type=int, default=30)
    p = add("request", cmd_request, "key")
    p.add_argument("--comments", type=int, default=0)
    add("sla", cmd_sla, "key")
    add("approvals", cmd_approvals, "key")
    add("transitions", cmd_transitions, "key")
    p = add("comment", cmd_comment, "key", dry=True)
    p.add_argument("text", nargs="?")
    p.add_argument("--file")
    p.add_argument("--internal", action="store_true")
    p = add("transition", cmd_transition, "key", "target", dry=True)
    p.add_argument("--comment")
    for name in ("approve", "decline"):
        p = add(name, cmd_decide(name), "key", dry=True)
        p.add_argument("approval_id", nargs="?")
    p = add("create", cmd_create, dry=True)
    for x in ("desk", "type", "summary"):
        p.add_argument("--" + x, required=True)
    p.add_argument("--desc")
    p.add_argument("--field", action="append", default=[])
    p = add("alerts", cmd_alerts)
    p.add_argument("--query", default="status: open")
    p.add_argument("--limit", type=int, default=30)
    add("alert", cmd_alert, "id")
    for name, verb in (("alert-ack", "acknowledge"), ("alert-close", "close")):
        p = add(name, cmd_alert_act(verb), "id", dry=True)
        p.add_argument("--note")
    add("teams", cmd_teams)
    add("schedules", cmd_schedules)
    p = add("oncall", cmd_oncall)
    p.add_argument("--schedule")
    p.add_argument("--name")
    p.add_argument("--date")
    p.add_argument("--next", action="store_true")
    p = add("raw", cmd_raw, "api", "method", "path")
    p.add_argument("-q", action="append", default=[])
    p.add_argument("--data")
    p.add_argument("--data-file")
    p.add_argument("--jq")
    p.add_argument("--max", type=int, default=4000)
    p = add("fetch", cmd_fetch)
    p.add_argument("--kind", required=True)
    p.add_argument("--ident", required=True)
    add("test", cmd_test)
    return ap


def main(argv=None):
    a = build().parse_args(argv)
    configure(a.instance)
    try:
        a.fn(a)
    except BrokenPipeError:
        pass


if __name__ == "__main__":
    main()
