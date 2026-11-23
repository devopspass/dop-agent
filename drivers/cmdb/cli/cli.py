#!/usr/bin/env python3
"""Compact read-only ServiceNow CMDB CLI for dop-agent (terse, LLM-friendly output). Part of the `cmdb` package driver.

Connection: --instance NAME (or CMDB_INSTANCE). base_url, options.username and auth.token come from config.yaml
(integrations.<NAME>, driver cmdb); auth.token is a ${env:X}/${file:P}/${keychain:S} ref, never printed.
`options.username` set -> HTTP Basic (username + password); empty -> `Authorization: Bearer <token>` (OAuth token).
Only GET requests to the Table API (/api/now/table/...) are ever sent.

  search [--class cmdb_ci] [--name TEXT] [--ip IP] [--owner TEXT] [--group TEXT] [--where ENCODED] [--limit 25]
  ci REF [--class cmdb_ci]                      details of one CI (REF = sys_id or exact name)
  rels REF [--direction down|up|both] [--depth 2] [--max-nodes 100]     dependency tree, bounded
  app-servers APP [--depth 3] [--direction down|up|both] [--class-filter server] [--max-nodes 200]
  owners REF                                    owner / managed by / support group / change group
  export APP [--depth 3] [--direction ...] [--format table|csv] [--out FILE] [--max-nodes 500]
  fetch --kind ci --ident '{"sys_id":"..."}'    (furl card JSON, used by dop-agent)
  test                                          (connection check JSON, used by the Settings Test button)
"""
import argparse
import base64
import csv
import io
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

CONF = {"base_url": "", "username": "", "app_table": "cmdb_ci_business_app"}
_TOKEN = ""
SYS_ID_RE = re.compile(r"[0-9a-f]{32}")
TABLE_RE = re.compile(r"cmdb_[a-z0-9_]{2,70}")
FIELD_RE = re.compile(r"[a-z][a-z0-9_.]{0,60}")
SAFE_VALUE_RE = re.compile(r"[^\^\r\n\x00]{1,200}")
SECRET_RE = re.compile(r"(?i)(token|password|secret|authorization)([=:]\s*)(Basic |Bearer )?\S+")
MAX_DEPTH = 5
PAGE = 100
CI_FIELDS = ("sys_id,name,sys_class_name,ip_address,operational_status,install_status,owned_by,managed_by,support_group,"
             "u_environment,environment,short_description,sys_updated_on,sys_created_on")
DETAIL_SKIP = {"sys_mod_count", "sys_tags", "sys_domain", "sys_domain_path"}


def die(msg):
    sys.exit(SECRET_RE.sub(r"\1\2***", str(msg)))


class Http(Exception):
    def __init__(self, code, text):
        super().__init__(f"HTTP {code}: {text}")
        self.code, self.text = code, text


def use(base_url, username, token, app_table=""):
    """Set the connection directly (tests, embedding); the token stays in module state, never in CONF."""
    global _TOKEN
    CONF.update(base_url=(base_url or "").rstrip("/"), username=username or "",
                app_table=app_table or "cmdb_ci_business_app")
    _TOKEN = token


def configure(instance=None):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import cmdbconfig
    try:
        _, base, token, opts = cmdbconfig.load_instance(instance)
    except cmdbconfig.InstanceError as e:
        die(e)
    use(base, str(opts.get("username") or ""), token, str(opts.get("app_table") or ""))


def creds():
    base = CONF["base_url"]
    if not (base and _TOKEN):
        die("CMDB is not configured: pass --instance <name> (integrations.<name> with driver cmdb, base_url, auth.token)")
    if not re.match(r"^https?://", base):
        die("base URL must start with http(s)://")
    if CONF["username"]:
        return base, "Basic " + base64.b64encode(f"{CONF['username']}:{_TOKEN}".encode()).decode()
    return base, "Bearer " + _TOKEN


def call(path, query=None):
    """GET only. Returns the parsed JSON body (`result` is inside)."""
    base, auth = creds()
    if not path.startswith("/api/now/"):
        die("only /api/now/ paths are allowed")
    url = base + path + ("?" + urllib.parse.urlencode(query) if query else "")
    req = urllib.request.Request(url, method="GET", headers={"Authorization": auth, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        raise Http(e.code, e.read().decode(errors="replace")[:300].replace(auth, "***"))
    except urllib.error.URLError as e:
        die(f"network error GET {path}: {e.reason}")
    try:
        return json.loads(body) if body else {}
    except ValueError:
        return {}


def table(name, query="", fields="", limit=25, order=""):
    """Rows of a table, paged by 100, at most `limit` rows. Reference fields come as {value, display_value}."""
    if not TABLE_RE.fullmatch(name):
        die(f"table {name!r} not allowed (must be a cmdb_* table)")
    rows, offset = [], 0
    while len(rows) < limit:
        q = {"sysparm_query": query + (("^" if query else "") + order if order else ""), "sysparm_limit": min(PAGE, limit - len(rows)),
             "sysparm_offset": offset, "sysparm_display_value": "all", "sysparm_exclude_reference_link": "true"}
        if fields:
            q["sysparm_fields"] = fields
        try:
            got = (call(f"/api/now/table/{name}", q) or {}).get("result") or []
        except Http as e:
            die(f"HTTP {e.code} GET table {name}: {e.text}")
        rows += got
        if len(got) < q["sysparm_limit"]:
            break
        offset += len(got)
    return rows[:limit]


def val(x):
    """Raw value of a plain / display_value=all field."""
    return str(x.get("value") or "") if isinstance(x, dict) else str(x or "")


def disp(x):
    if isinstance(x, dict):
        return str(x.get("display_value") or x.get("value") or "")
    return str(x or "")


def clean(v, what="value"):
    v = str(v or "").strip()
    # encoded queries can evaluate `javascript:` server-side, so it is refused in every user value
    if not SAFE_VALUE_RE.fullmatch(v) or "javascript:" in v.lower().replace(" ", ""):
        die(f"{what} must be 1-200 chars without '^', newlines or javascript:")
    return v


def table_arg(v):
    v = (v or "").strip()
    if not TABLE_RE.fullmatch(v):
        die(f"class {v!r} must look like cmdb_ci, cmdb_ci_server, cmdb_ci_appl, cmdb_ci_business_app ...")
    return v


def short(s, n=60):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def ci_row(r):
    return {"sys_id": val(r.get("sys_id")), "name": disp(r.get("name")), "class": val(r.get("sys_class_name")),
            "ip": disp(r.get("ip_address")), "status": disp(r.get("operational_status")) or disp(r.get("install_status")),
            "owner": disp(r.get("owned_by")), "group": disp(r.get("support_group")),
            "env": disp(r.get("u_environment")) or disp(r.get("environment")),
            "updated": val(r.get("sys_updated_on"))}


COLS = ("name", "class", "ip", "status", "env", "owner", "group", "sys_id")


def print_table(rows, cols=COLS):
    cols = [c for c in cols if any(r.get(c) for r in rows)] or list(cols[:1])
    print(" | ".join(cols))
    for r in rows:
        print(" | ".join(short(r.get(c, ""), 40) if c != "sys_id" else r.get(c, "") for c in cols))


def resolve(ref, cls="cmdb_ci"):
    """REF -> CI row. A sys_id is looked up directly, anything else as exact name (several -> error with candidates)."""
    ref = clean(ref, "REF")
    cls = table_arg(cls)
    q = f"sys_id={ref}" if SYS_ID_RE.fullmatch(ref) else f"name={ref}"
    rows = table(cls, q, CI_FIELDS, 6)
    if not rows:
        die(f"no CI {ref!r} in {cls}; use `search --name` to find it")
    if len(rows) > 1:
        die(f"{ref!r} is ambiguous in {cls}, use a sys_id: " + "; ".join(f"{val(r['sys_id'])} ({val(r.get('sys_class_name'))})" for r in rows))
    return ci_row(rows[0])


def resolve_app(ref):
    try:
        return resolve(ref, CONF["app_table"])
    except SystemExit:
        return resolve(ref, "cmdb_ci")


def edges(ids, direction):
    """Relationships touching `ids`: list of (from_id, to_id, type) where `from` is the node already known."""
    out = []
    ids = list(ids)
    for i in range(0, len(ids), 40):
        chunk = ",".join(ids[i:i + 40])
        if direction in ("down", "both"):
            for r in table("cmdb_rel_ci", f"parentIN{chunk}", "parent,child,type", 2000):
                out.append((val(r["parent"]), val(r["child"]), disp(r.get("type"))))
        if direction in ("up", "both"):
            for r in table("cmdb_rel_ci", f"childIN{chunk}", "parent,child,type", 2000):
                out.append((val(r["child"]), val(r["parent"]), disp(r.get("type"))))
    return out


def ci_by_ids(ids):
    ids, out = list(ids), {}
    for i in range(0, len(ids), 40):
        for r in table("cmdb_ci", "sys_idIN" + ",".join(ids[i:i + 40]), CI_FIELDS, 40):
            out[val(r["sys_id"])] = ci_row(r)
    return out


def walk(root, direction="down", depth=2, max_nodes=100):
    """Bounded BFS over cmdb_rel_ci. Returns (nodes{id: row+depth}, links[(from, to, type)], truncated)."""
    depth = max(1, min(depth, MAX_DEPTH))
    max_nodes = max(1, min(max_nodes, 1000))
    nodes = {root["sys_id"]: dict(root, depth=0)}
    links, frontier, truncated = [], [root["sys_id"]], False
    for d in range(1, depth + 1):
        if not frontier:
            break
        found = edges(frontier, direction)
        fresh = []
        for a, b, t in found:
            if b not in nodes and b not in fresh:
                if len(nodes) + len(fresh) >= max_nodes:
                    truncated = True
                    continue
                fresh.append(b)
            if b in nodes or b in fresh:
                links.append((a, b, t))
        details = ci_by_ids(fresh)
        for i in fresh:
            nodes[i] = dict(details.get(i) or {"sys_id": i, "name": "(not in cmdb_ci)", "class": ""}, depth=d)
        frontier = fresh
    return nodes, links, truncated


def cmd_search(a):
    cls = table_arg(a.cls)
    parts = []
    if a.name:
        parts.append("nameLIKE" + clean(a.name, "--name"))
    if a.ip:
        parts.append("ip_address=" + clean(a.ip, "--ip"))
    if a.owner:
        parts.append("owned_by.nameLIKE" + clean(a.owner, "--owner"))
    if a.group:
        parts.append("support_group.nameLIKE" + clean(a.group, "--group"))
    if a.where:
        parts.append(clean(a.where, "--where"))
    rows = [ci_row(r) for r in table(cls, "^".join(parts), CI_FIELDS, max(1, min(a.limit, 500)), "ORDERBYname")]
    print(f"{len(rows)} CI(s) in {cls}" + (" (limit reached)" if len(rows) >= a.limit else ""))
    if rows:
        print_table(rows)


def cmd_ci(a):
    base = resolve(a.ref, a.cls)
    full = table(table_arg(a.cls), "sys_id=" + base["sys_id"], "", 1)
    print(f"{base['name']} [{base['class']}] {base['sys_id']}")
    for k, v in sorted((full[0] if full else {}).items()):
        d = disp(v)
        if d and k not in DETAIL_SKIP and not k.startswith("sys_mod"):
            print(f"{k}: {short(d, 120)}")
    print(f"link: {CONF['base_url']}/nav_to.do?uri=" + urllib.parse.quote(f"{base['class']}.do?sys_id={base['sys_id']}"))


def print_tree(nodes, links, root_id):
    children = {}
    for a, b, t in links:
        children.setdefault(a, []).append((b, t))
    seen = set()

    def rec(i, ind, rel):
        n = nodes[i]
        again = i in seen
        seen.add(i)
        print(f"{'  ' * ind}{('[' + rel + '] ') if rel else ''}{n['name']} ({n['class']}{', ' + n['ip'] if n.get('ip') else ''}) {i}" + (" …" if again else ""))
        if not again:
            for b, t in children.get(i, []):
                if b in nodes:
                    rec(b, ind + 1, t)
    rec(root_id, 0, "")


def cmd_rels(a):
    root = resolve(a.ref)
    nodes, links, trunc = walk(root, a.direction, a.depth, a.max_nodes)
    print_tree(nodes, links, root["sys_id"])
    print(f"-- {len(nodes) - 1} related CI(s), depth<={min(a.depth, MAX_DEPTH)}, direction {a.direction}" + (", TRUNCATED at --max-nodes" if trunc else ""))


def app_cis(a):
    root = resolve_app(a.app)
    nodes, links, trunc = walk(root, a.direction, a.depth, a.max_nodes)
    return root, nodes, links, trunc


def server_like(row, flt):
    return flt.lower() in (row.get("class") or "").lower()


def cmd_app_servers(a):
    root, nodes, links, trunc = app_cis(a)
    rows = [n for i, n in nodes.items() if i != root["sys_id"] and server_like(n, a.class_filter)]
    print(f"{root['name']} [{root['class']}] -> {len(rows)} CI(s) with class containing {a.class_filter!r}" + (" (TRUNCATED)" if trunc else ""))
    if rows:
        print_table(sorted(rows, key=lambda r: (r["depth"], r["name"])))


def cmd_export(a):
    root, nodes, links, trunc = app_cis(a)
    rows = sorted(nodes.values(), key=lambda r: (r["depth"], r["class"], r["name"]))
    cols = ("depth",) + COLS + ("updated",)
    buf = io.StringIO()
    if a.format == "csv":
        w = csv.writer(buf)
        w.writerow(cols)
        for r in rows:
            w.writerow([r.get(c, "") for c in cols])
        text = buf.getvalue()
    else:
        text = " | ".join(cols) + "\n" + "\n".join(" | ".join(short(r.get(c, ""), 40) if c != "sys_id" else str(r.get(c, "")) for c in cols) for r in rows) + "\n"
    if a.out:
        tmp = a.out + ".tmp"
        with open(tmp, "w", newline="") as f:
            f.write(text)
        os.replace(tmp, a.out)
        print(f"wrote {len(rows)} CI(s) of {root['name']} to {a.out}" + (" (TRUNCATED)" if trunc else ""))
    else:
        sys.stdout.write(text)
        print(f"-- {len(rows)} CI(s) of {root['name']}" + (", TRUNCATED at --max-nodes" if trunc else ""))


def cmd_owners(a):
    r = table("cmdb_ci", ("sys_id=" if SYS_ID_RE.fullmatch(clean(a.ref)) else "name=") + clean(a.ref),
              "sys_id,name,sys_class_name,owned_by,managed_by,support_group,change_control,assignment_group,u_business_owner", 6)
    if not r:
        die(f"no CI {a.ref!r}")
    for x in r:
        print(f"{disp(x.get('name'))} [{val(x.get('sys_class_name'))}] {val(x.get('sys_id'))}")
        for k in ("owned_by", "managed_by", "support_group", "change_control", "assignment_group", "u_business_owner"):
            if disp(x.get(k)):
                print(f"  {k}: {disp(x.get(k))}")


def cmd_fetch(a):
    if a.kind != "ci":
        die(f"cmdb has no kind {a.kind!r}")
    try:
        sid = clean(json.loads(a.ident).get("sys_id", ""), "sys_id")
    except (ValueError, AttributeError):
        die("--ident must be a JSON object with sys_id")
    if not SYS_ID_RE.fullmatch(sid):
        die("sys_id must be 32 hex chars")
    rows = table("cmdb_ci", "sys_id=" + sid, CI_FIELDS, 1)
    if not rows:
        die(f"CI {sid} not found")
    r = ci_row(rows[0])
    print(json.dumps({"title": r["name"], "status": r["status"],
                      "url": f"{creds()[0]}/nav_to.do?uri=" + urllib.parse.quote(f"{r['class']}.do?sys_id={sid}"),
                      "fields": {"name": r["name"], "class": r["class"], "status": r["status"], "owner": r["owner"]}}))


def cmd_test(a):
    steps = []

    def step(name, fn):
        try:
            steps.append({"name": name, "ok": True, "message": fn()})
            return True
        except SystemExit as e:
            steps.append({"name": name, "ok": False, "message": str(e.code)[:200]})
            return False

    def conf():
        creds()
        return CONF["base_url"] + (" (basic auth)" if CONF["username"] else " (bearer token)")

    def auth():
        rows = table("cmdb_ci", "", "sys_id,name", 1)
        return f"cmdb_ci readable ({len(rows)} sample row)"

    def rels():
        return f"cmdb_rel_ci readable ({len(table('cmdb_rel_ci', '', 'sys_id', 1))} sample row)"

    def apps():
        return f"{CONF['app_table']} readable ({len(table(CONF['app_table'], '', 'sys_id', 1))} sample row)"
    ok = step("config", conf) and step("auth", auth) and step("relationships", rels)
    if ok:
        step("business_apps", apps)
    bad = next((s["message"] for s in steps if not s["ok"]), "")
    msg = "authenticated; CMDB tables readable" if ok and all(s["ok"] for s in steps) else (bad or "ok")
    print(json.dumps({"ok": ok, "message": msg, "steps": steps}))


def build():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instance", default=os.environ.get("CMDB_INSTANCE"))
    sp = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn):
        p = sp.add_parser(name)
        p.set_defaults(fn=fn)
        return p

    def walkargs(p, depth, nodes):
        p.add_argument("--depth", type=int, default=depth)
        p.add_argument("--direction", choices=["down", "up", "both"], default="down")
        p.add_argument("--max-nodes", type=int, default=nodes)

    p = add("search", cmd_search)
    p.add_argument("--class", dest="cls", default="cmdb_ci")
    for x in ("name", "ip", "owner", "group", "where"):
        p.add_argument("--" + x)
    p.add_argument("--limit", type=int, default=25)
    p = add("ci", cmd_ci)
    p.add_argument("ref")
    p.add_argument("--class", dest="cls", default="cmdb_ci")
    p = add("rels", cmd_rels)
    p.add_argument("ref")
    walkargs(p, 2, 100)
    p = add("app-servers", cmd_app_servers)
    p.add_argument("app")
    p.add_argument("--class-filter", default="server")
    walkargs(p, 3, 200)
    p = add("export", cmd_export)
    p.add_argument("app")
    p.add_argument("--format", choices=["table", "csv"], default="table")
    p.add_argument("--out")
    walkargs(p, 3, 500)
    p = add("owners", cmd_owners)
    p.add_argument("ref")
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
