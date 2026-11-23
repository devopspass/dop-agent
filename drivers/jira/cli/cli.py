#!/usr/bin/env python3
"""Compact Jira Cloud CLI for dop-agent (terse, LLM-friendly output). Part of the `jira` package driver.

Connection (global flags, else env; the API token is read from a file, never printed):
  --base-url URL      / JIRA_BASE_URL     https://<site>.atlassian.net
  --email EMAIL       / JIRA_EMAIL        account the token belongs to
  --token-file PATH   / JIRA_TOKEN_FILE   file holding the API token (JIRA_API_TOKEN env also accepted)
  --sp-field ID       / JIRA_SP_FIELD     story-points field (default: auto-detected by name)
  --sprint-field ID   / JIRA_SPRINT_FIELD sprint field (default: auto-detected by schema)

  cli.py ticket KEY [--comments 3] [--desc 300|0|-1] [--width 200]
  cli.py search "<JQL>" [--fields status,sp,sprint,epic,assignee,<any field id>] [--limit 30] [--all] [--width 70|0] [--links] [--group-by assignee]
  cli.py comment KEY "text" | --file f.md   [--dry-run]
  cli.py transition KEY <name|id|target-status> [--list] [--chain] [--dry-run]
  cli.py assign KEY <me|name|none> [--dry-run]
  cli.py sp KEY 3 [--dry-run]
  cli.py link KEY <type name> KEY2 [--dry-run]       # first KEY is the outward issue
  cli.py create --project KEY --type Task --summary S [--desc D] [--parent KEY] [--labels a,b]
                     [--assignee me] [--sp 2] [--sprint ID] [--dry-run]
  cli.py raw GET|POST|PUT|PATCH|DELETE /rest/api/3/... [-q k=v] [--data JSON|--data-file f] [--jq a.b[*].c] [--max 4000]
  cli.py fetch --kind ticket --ident '{"key":"KEY"}'     (furl card JSON, used by dop-agent)
  cli.py test                                            (connection check JSON, used by the Settings Test button)

Text for comment/create supports: paragraphs, "- " bullets, ``` code fences, **bold**, `code`, [t](url).
Richer ADF: use `raw ... --data-file`.
"""
import argparse, base64, json, os, re, sys, urllib.error, urllib.parse, urllib.request

API = "/rest/api/3"
CONF = {"base_url": "", "email": "", "token_file": "", "sp_field": "", "sprint_field": ""}
_FIELDS = {}


def die(msg):
    sys.exit(msg)


def configure(base_url=None, email=None, token_file=None, sp_field=None, sprint_field=None):
    for k, v, env in (("base_url", base_url, "JIRA_BASE_URL"), ("email", email, "JIRA_EMAIL"),
                      ("token_file", token_file, "JIRA_TOKEN_FILE"), ("sp_field", sp_field, "JIRA_SP_FIELD"),
                      ("sprint_field", sprint_field, "JIRA_SPRINT_FIELD")):
        CONF[k] = v or os.getenv(env) or ""
    _FIELDS.clear()


def creds():
    base, email = CONF["base_url"].rstrip("/"), CONF["email"]
    tok = os.getenv("JIRA_API_TOKEN") or ""
    if CONF["token_file"]:
        try:
            tok = open(os.path.expanduser(CONF["token_file"])).read().strip()
        except OSError as e:
            die(f"cannot read token file {CONF['token_file']}: {e.strerror}")
    if not (base and email and tok):
        die("Jira is not configured: need base URL, email and API token (--base-url/--email/--token-file or JIRA_* env)")
    if not re.match(r"^https?://", base):
        die("base URL must start with http(s)://")
    return base, "Basic " + base64.b64encode(f"{email}:{tok}".encode()).decode()


def call(method, path, query=None, data=None):
    base, auth = creds()
    url = base + path + (("?" + urllib.parse.urlencode(query)) if query else "")
    req = urllib.request.Request(url, method=method, data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Authorization": auth, "Accept": "application/json", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        txt = e.read().decode(errors="replace")[:300].replace(auth, "***")
        die(f"HTTP {e.code} {method} {path}: {txt}")
    except urllib.error.URLError as e:
        die(f"network error {method} {path}: {e.reason}")


def _discover():
    """Story-points / sprint field ids: explicit option/env wins, else looked up once by name/schema."""
    if _FIELDS:
        return
    sp, sprint = CONF["sp_field"], CONF["sprint_field"]
    if not (sp and sprint):
        for f in call("GET", API + "/field"):
            custom = (f.get("schema") or {}).get("custom", "")
            if not sprint and custom.endswith(":gh-sprint"):
                sprint = f["id"]
            if not sp and f.get("custom") and f.get("name", "").lower() in ("story points", "story point estimate"):
                sp = sp or f["id"]
    _FIELDS.update(sp=sp or "", sprint=sprint or "")


def sp_field(required=False):
    _discover()
    if required and not _FIELDS["sp"]:
        die("story-points field not found; set the sp_field option (customfield_NNNNN)")
    return _FIELDS["sp"]


def sprint_field(required=False):
    _discover()
    if required and not _FIELDS["sprint"]:
        die("sprint field not found; set the sprint_field option (customfield_NNNNN)")
    return _FIELDS["sprint"]


# ---------- ADF ----------
def adf_to_text(n):
    if isinstance(n, list):
        return "".join(adf_to_text(x) for x in n)
    if not isinstance(n, dict):
        return ""
    t = n.get("type")
    if t == "text":
        return n.get("text", "")
    if t == "hardBreak":
        return "\n"
    if t == "mention":
        return n.get("attrs", {}).get("text", "@?")
    if t in ("inlineCard", "blockCard"):
        return n.get("attrs", {}).get("url", "")
    s = adf_to_text(n.get("content", []))
    return s + "\n" if t in ("paragraph", "heading", "listItem", "codeBlock", "tableRow") else s


def _inline(s):
    out, pos = [], 0
    for m in re.finditer(r"\*\*(.+?)\*\*|`([^`]+)`|\[([^\]]+)\]\((https?://[^)\s]+)\)", s):
        if m.start() > pos:
            out.append({"type": "text", "text": s[pos:m.start()]})
        if m.group(1):
            out.append({"type": "text", "text": m.group(1), "marks": [{"type": "strong"}]})
        elif m.group(2):
            out.append({"type": "text", "text": m.group(2), "marks": [{"type": "code"}]})
        else:
            out.append({"type": "text", "text": m.group(3), "marks": [{"type": "link", "attrs": {"href": m.group(4)}}]})
        pos = m.end()
    if pos < len(s):
        out.append({"type": "text", "text": s[pos:]})
    return out or [{"type": "text", "text": " "}]


def text_to_adf(text):
    content, para, bullets, code, lang = [], [], [], None, None

    def flush():
        nonlocal para, bullets
        if para:
            nodes = []
            for i, l in enumerate(para):
                if i:
                    nodes.append({"type": "hardBreak"})
                nodes += _inline(l)
            content.append({"type": "paragraph", "content": nodes})
            para = []
        if bullets:
            content.append({"type": "bulletList", "content": [
                {"type": "listItem", "content": [{"type": "paragraph", "content": _inline(b)}]} for b in bullets]})
            bullets = []

    for line in text.split("\n"):
        if code is not None:
            if line.strip() == "```":
                node = {"type": "codeBlock", "content": [{"type": "text", "text": "\n".join(code) or " "}]}
                if lang:
                    node["attrs"] = {"language": lang}
                content.append(node)
                code = None
            else:
                code.append(line)
        elif line.strip().startswith("```"):
            flush()
            code, lang = [], line.strip()[3:].strip() or None
        elif re.match(r"^\s*[-*] +", line):
            if para:
                flush()
            bullets.append(re.sub(r"^\s*[-*] +", "", line))
        elif not line.strip():
            flush()
        else:
            if bullets:
                flush()
            para.append(line)
    if code is not None:
        content.append({"type": "codeBlock", "content": [{"type": "text", "text": "\n".join(code) or " "}]})
    flush()
    return {"type": "doc", "version": 1, "content": content or [{"type": "paragraph", "content": [{"type": "text", "text": " "}]}]}


# ---------- helpers ----------
def short(s, n):
    s = " ".join((s or "").split())
    return s if n <= 0 or len(s) <= n else s[:n] + "…"


def project(obj, path):
    """dotted projection: a.b[*].c ; comma separated list of paths -> dict"""
    def one(o, parts):
        if not parts:
            return o
        p, rest = parts[0], parts[1:]
        if p.endswith("[*]"):
            o = o.get(p[:-3]) if isinstance(o, dict) else None
            return [one(x, rest) for x in o] if isinstance(o, list) else None
        return one(o.get(p), rest) if isinstance(o, dict) else (one(o[int(p)], rest) if isinstance(o, list) and p.isdigit() and int(p) < len(o) else None)
    paths = [p for p in path.split(",") if p]
    if len(paths) == 1:
        return one(obj, paths[0].split("."))
    return {p: one(obj, p.split(".")) for p in paths}


def emit(s, mx=0):
    s = s if isinstance(s, str) else json.dumps(s, indent=1, ensure_ascii=False)
    if mx and len(s) > mx:
        s = s[:mx] + f"\n…[+{len(s) - mx} chars, use --max]"
    print(s)


def write(dry, method, path, data=None, ok=None):
    if dry:
        print(f"DRY-RUN {method} {path} {json.dumps(data, ensure_ascii=False) if data is not None else ''}".rstrip())
        return None
    r = call(method, path, data=data)
    if ok:
        print(ok(r) if callable(ok) else ok)
    return r


def me():
    return call("GET", API + "/myself")["accountId"]


def account(who):
    if who == "none":
        return None
    if who == "me":
        return me()
    u = call("GET", API + "/user/search", {"query": who})
    if len(u) != 1:
        die(f"'{who}' matches {len(u)} users: " + ", ".join(x["displayName"] for x in u[:5]))
    return u[0]["accountId"]


# ---------- commands ----------
def cmd_ticket(a):
    SP_FIELD, SPRINT_FIELD = sp_field(), sprint_field()
    extra = ",".join(x for x in (SP_FIELD, SPRINT_FIELD) if x)
    f = call("GET", f"{API}/issue/{a.key}", {"fields": f"summary,status,assignee,{extra},parent,issuelinks,issuetype,priority,updated,description,comment,labels"})["fields"]
    sprint = [s["name"] for s in f.get(SPRINT_FIELD) or []][-1:]
    par = f.get("parent")
    out = [f"{a.key} [{f['issuetype']['name']}] {f['summary']}",
           f"status={f['status']['name']} assignee={(f.get('assignee') or {}).get('displayName')} SP={f.get(SP_FIELD)} "
           f"sprint={sprint[0] if sprint else None} prio={(f.get('priority') or {}).get('name')} "
           f"epic={par['key'] + ':' + short(par['fields']['summary'], 40) if par else None} upd={f['updated'][:10]}"
           + (f" labels={','.join(f['labels'])}" if f.get("labels") else "")]
    for l in f.get("issuelinks", []):
        d = "inwardIssue" if "inwardIssue" in l else "outwardIssue"
        o = l[d]
        out.append(f"link {l['type']['inward' if d == 'inwardIssue' else 'outward']}: {o['key']} [{o['fields']['status']['name']}] {short(o['fields']['summary'], 60)}")
    if a.desc >= 0:
        out.append("desc: " + short(adf_to_text(f.get("description")), a.desc))
    cs = f.get("comment", {}).get("comments", [])
    out.append(f"comments {len(cs)}" + (f" (last {min(a.comments, len(cs))}):" if a.comments and cs else ""))
    for c in (cs[-a.comments:] if a.comments else []):
        out.append(f"  {c['id']} {c['created'][:10]} {c['author']['displayName']}: {short(adf_to_text(c['body']), a.width)}")
    emit("\n".join(out))


def cell(f, fl):
    SP_FIELD = _FIELDS.get("sp") or CONF["sp_field"]
    SPRINT_FIELD = _FIELDS.get("sprint") or CONF["sprint_field"]
    v = f.get(fl)
    if fl == SP_FIELD:
        return f"SP={v}"
    if fl == SPRINT_FIELD:
        return (v[-1].get("name") if v else None) or "None"
    if fl == "parent":
        return v["key"] if v else "None"
    if fl == "assignee" and not v:
        return "unassigned"
    if isinstance(v, dict):
        return str(v.get("name") or v.get("displayName"))
    if isinstance(v, list):
        return ",".join(str(x.get("name", x) if isinstance(x, dict) else x) for x in v)
    return str(v)


def cmd_search(a):
    names = [x for x in (a.fields.split(",") if a.fields else ["status", "sp"]) if x and x != "summary"]
    ALIASES = {"epic": "parent"}
    if "sp" in names:
        ALIASES["sp"] = sp_field(True)
    if "sprint" in names or a.group_by == "sprint":
        ALIASES["sprint"] = sprint_field(True)
    SP_FIELD = sp_field() if a.group_by or "sp" in names else ""
    fields = [ALIASES.get(x, x) for x in names]
    if a.group_by and a.all is False:
        a.all = True
    if a.group_by and ALIASES.get(a.group_by, a.group_by) not in fields:
        fields.append(ALIASES.get(a.group_by, a.group_by))
    if a.group_by and SP_FIELD and SP_FIELD not in fields:
        fields.append(SP_FIELD)
    q = {"jql": a.jql, "fields": ",".join(["summary"] + fields), "maxResults": str(100 if a.all else min(a.limit, 100))}
    rows = []
    while True:
        r = call("GET", API + "/search/jql", q)
        rows += r.get("issues", [])
        tok = r.get("nextPageToken")
        if not tok or not a.all:
            break
        q["nextPageToken"] = tok
    more = bool(tok) and not a.all
    base = creds()[0] if a.links else ""
    sp_total, by, groups = 0.0, {}, {}
    for i in rows:
        f = i["fields"]
        cols = [cell(f, fl) for fl in fields]
        if SP_FIELD in fields:
            sp_total += f.get(SP_FIELD) or 0
        if "status" in fields:
            st = f["status"]["name"]
            by[st] = by.get(st, 0) + 1
        if a.group_by:
            g = cell(f, ALIASES.get(a.group_by, a.group_by))
            c, p = groups.get(g, (0, 0.0))
            groups[g] = (c + 1, p + (f.get(SP_FIELD) or 0))
            continue
        key = f"{base}/browse/{i['key']}" if a.links else i["key"]
        print(f"{key}  {' | '.join(cols)}  {short(f['summary'], a.width)}")
    for g, (c, p) in sorted(groups.items(), key=lambda kv: -kv[1][1]):
        print(f"{g}  {c} issues  SP={p:g}")
    tail = f"{len(rows)} issues"
    if SP_FIELD in fields:
        tail += f", SP total {sp_total:g}"
    if by:
        tail += ", " + ", ".join(f"{k}={v}" for k, v in sorted(by.items(), key=lambda kv: -kv[1]))
    print(f"({tail}{', +more, use --all or --limit' if more else ''})")


def cmd_comment(a):
    text = open(a.file).read() if a.file else a.text
    if not text:
        die("comment text required")
    write(a.dry_run, "POST", f"{API}/issue/{a.key}/comment", {"body": text_to_adf(text)}, lambda r: f"OK comment {r['id']}")


def transitions(key):
    return call("GET", f"{API}/issue/{key}/transitions")["transitions"]


def plan_path(key, target, chain):
    """Return [transition] for a unique direct match by id/name/target status; None means multi-hop (needs --chain)."""
    t = target.lower()

    def match(trs):
        return [x for x in trs if x["id"] == target or x["name"].lower() == t or x["to"]["name"].lower() == t]
    trs = transitions(key)
    m = match(trs)
    if len(m) == 1:
        return [m[0]]
    if len(m) > 1:
        die("ambiguous: " + ", ".join(f"{x['id']}:{x['name']}->{x['to']['name']}" for x in m))
    if not chain:
        die(f"no direct transition '{target}'; available: " + " | ".join(f"{x['id']}:{x['name']}->{x['to']['name']}" for x in trs) + " (use --chain for multi-hop)")
    # multi-hop: the workflow graph is only visible state by state, so follow the known
    # forward chain 'Selected for sprint' -> 'Ready to progress' and re-resolve each step live.
    return None


FORWARD = ["selected for sprint", "ready to progress"]


def cmd_transition(a):
    if a.list:
        print(" | ".join(f"{x['id']}:{x['name']}->{x['to']['name']}" for x in transitions(a.key)))
        return
    if not a.target:
        die("target required")
    path = plan_path(a.key, a.target, a.chain)
    if path:
        print(f"path: {path[0]['id']}:{path[0]['name']}->{path[0]['to']['name']}")
        write(a.dry_run, "POST", f"{API}/issue/{a.key}/transitions", {"transition": {"id": path[0]["id"]}}, "OK")
        return
    # chain: iterate live, max 3 hops, each hop re-resolved
    hops = 0
    while hops < 3:
        trs = transitions(a.key)
        direct = [x for x in trs if a.target.lower() in (x["name"].lower(), x["to"]["name"].lower()) or x["id"] == a.target]
        step = direct[0] if direct else next((x for n in FORWARD for x in trs if x["name"].lower() == n), None)
        if not step:
            die("chain stuck; available: " + " | ".join(f"{x['id']}:{x['name']}->{x['to']['name']}" for x in trs))
        print(f"hop {hops + 1}: {step['id']}:{step['name']}->{step['to']['name']}")
        if a.dry_run:
            print("DRY-RUN stops after first hop; expected next: " + " -> ".join(FORWARD[1:]) + " (re-resolved live)")
            return
        call("POST", f"{API}/issue/{a.key}/transitions", data={"transition": {"id": step["id"]}})
        hops += 1
        if direct:
            break
    st = call("GET", f"{API}/issue/{a.key}", {"fields": "status"})["fields"]["status"]["name"]
    print(f"status now: {st}")
    if st.lower() != a.target.lower() and not direct:
        die(f"target '{a.target}' not reached after {hops} hops (status={st})")


def cmd_assign(a):
    write(a.dry_run, "PUT", f"{API}/issue/{a.key}/assignee", {"accountId": account(a.who)}, "OK")


def cmd_sp(a):
    write(a.dry_run, "PUT", f"{API}/issue/{a.key}", {"fields": {sp_field(True): float(a.value)}}, "OK")


def cmd_link(a):
    write(a.dry_run, "POST", API + "/issueLink", {"type": {"name": a.type}, "outwardIssue": {"key": a.outward}, "inwardIssue": {"key": a.inward}}, "OK")


def cmd_create(a):
    f = {"project": {"key": a.project}, "issuetype": {"name": a.type}, "summary": a.summary}
    if a.desc:
        f["description"] = text_to_adf(a.desc)
    if a.parent:
        f["parent"] = {"key": a.parent}
    if a.labels:
        f["labels"] = a.labels.split(",")
    if a.assignee:
        f["assignee"] = {"accountId": "<resolved>" if a.dry_run else account(a.assignee)}
    if a.sp is not None:
        f[sp_field(True)] = a.sp
    r = write(a.dry_run, "POST", API + "/issue", {"fields": f}, lambda r: f"OK {r['key']}")
    if a.sprint:
        write(a.dry_run, "PUT", f"{API}/issue/{r['key'] if r else '<KEY>'}", {"fields": {sprint_field(True): int(a.sprint)}}, "OK sprint set")


def cmd_raw(a):
    data = json.load(open(a.data_file)) if a.data_file else (json.loads(a.data) if a.data else None)
    q = dict(x.split("=", 1) for x in a.q) or None
    r = call(a.method.upper(), a.path, q, data)
    emit(project(r, a.jq) if a.jq else r, a.max)


def cmd_fetch(a):
    if a.kind != "ticket":
        die(f"jira has no kind {a.kind!r}")
    key = json.loads(a.ident).get("key", "")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*-\d+", key):
        die(f"bad issue key {key!r}")
    f = call("GET", f"{API}/issue/{key}", {"fields": "summary,status,assignee"})["fields"]
    status = (f.get("status") or {}).get("name") or ""
    print(json.dumps({"title": f.get("summary") or "", "status": status, "url": f"{creds()[0]}/browse/{key}",
                      "fields": {"key": key, "summary": f.get("summary") or "", "status": status,
                                 "assignee": (f.get("assignee") or {}).get("displayName") or ""}}))


def cmd_test(a):
    steps = []

    def step(name, fn):
        try:
            msg = fn()
            steps.append({"name": name, "ok": True, "message": msg})
            return True
        except SystemExit as e:
            steps.append({"name": name, "ok": False, "message": str(e.code)[:200]})
            return False
    state = {}

    def conf():
        creds()
        return CONF["base_url"]

    def who():
        u = call("GET", API + "/myself")
        state["user"] = u.get("displayName") or u.get("emailAddress") or "?"
        return state["user"]

    def fields():
        return f"sp={sp_field() or 'n/a'} sprint={sprint_field() or 'n/a'}"
    ok = step("config", conf) and step("auth", who) and step("fields", fields)
    print(json.dumps({"ok": ok, "message": f"authenticated as {state['user']}" if ok else
                      next(s["message"] for s in steps if not s["ok"]), "steps": steps}))


def build():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url")
    ap.add_argument("--email")
    ap.add_argument("--token-file")
    ap.add_argument("--sp-field")
    ap.add_argument("--sprint-field")
    sp = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, *args, dry=False):
        p = sp.add_parser(name)
        for x in args:
            p.add_argument(x)
        if dry:
            p.add_argument("--dry-run", action="store_true")
        p.set_defaults(fn=fn)
        return p
    p = add("ticket", cmd_ticket, "key")
    p.add_argument("--comments", type=int, default=0)
    p.add_argument("--desc", type=int, default=300)
    p.add_argument("--width", type=int, default=200)
    p = add("search", cmd_search, "jql")
    p.add_argument("--fields")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--all", action="store_true")
    p.add_argument("--width", type=int, default=70, help="summary width, 0 = full")
    p.add_argument("--links", action="store_true", help="print browse URL instead of key")
    p.add_argument("--group-by", help="roll up count+SP per field (assignee, status, sprint, epic) instead of listing issues")
    p = add("comment", cmd_comment, "key", dry=True)
    p.add_argument("text", nargs="?")
    p.add_argument("--file")
    p = add("transition", cmd_transition, "key", dry=True)
    p.add_argument("target", nargs="?")
    p.add_argument("--list", action="store_true")
    p.add_argument("--chain", action="store_true")
    add("assign", cmd_assign, "key", "who", dry=True)
    add("sp", cmd_sp, "key", "value", dry=True)
    add("link", cmd_link, "outward", "type", "inward", dry=True)
    p = add("create", cmd_create, dry=True)
    for x in ("project", "type", "summary"):
        p.add_argument("--" + x, required=True)
    for x in ("desc", "parent", "labels", "assignee", "sprint"):
        p.add_argument("--" + x)
    p.add_argument("--sp", type=float)
    p = add("fetch", cmd_fetch)
    p.add_argument("--kind", required=True)
    p.add_argument("--ident", required=True)
    add("test", cmd_test)
    p = add("raw", cmd_raw, "method", "path")
    p.add_argument("-q", action="append", default=[])
    p.add_argument("--data")
    p.add_argument("--data-file")
    p.add_argument("--jq")
    p.add_argument("--max", type=int, default=4000)
    return ap


def main(argv=None):
    a = build().parse_args(argv)
    configure(a.base_url, a.email, a.token_file, a.sp_field, a.sprint_field)
    try:
        a.fn(a)
    except BrokenPipeError:
        pass


if __name__ == "__main__":
    main()
