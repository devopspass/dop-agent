#!/usr/bin/env python3
"""Compact Confluence Cloud CLI for dop-agent (terse, LLM-friendly output). Part of the `confluence` package driver.

Connection: --instance NAME (confluence integration in config.yaml; env CONFLUENCE_INSTANCE; optional when exactly one is
enabled). base_url, options.email and the token (auth.token, a ${env:X}/${file:P}/${keychain:S} ref) are resolved from
config.yaml by cfconfig.py, so the token never travels through argv/env and is never printed.

  cli.py search "<CQL>" [--limit 10] [--links]        e.g. 'title ~ "runbook" AND space = DOC'
  cli.py page ID [--format text|storage|view] [--max-chars 20000]
  cli.py children ID [--limit 25]
  cli.py comments ID [--limit 25]
  cli.py attachments ID [--limit 25]
  cli.py spaces [--limit 25]
  cli.py create --space KEY --title T (--body TEXT | --file f) [--parent ID] [--storage] [--dry-run]
  cli.py update ID (--body TEXT | --file f) [--title T] [--message M] [--storage] [--dry-run]   (version is bumped automatically)
  cli.py comment ID (--body TEXT | --file f) [--storage] [--dry-run]
  cli.py raw GET|POST|PUT|PATCH|DELETE /wiki/api/v2/... [-q k=v] [--data JSON|--data-file f] [--jq a.b[*].c] [--max 4000]
  cli.py fetch --kind page --ident '{"id":"123"}'   (furl card JSON, used by dop-agent)
  cli.py test                                               (connection check JSON, used by the Settings Test button)

Body text (without --storage) supports: paragraphs, "- " bullets, ``` code fences, **bold**, `code`, [t](url); HTML in it is escaped.
--storage takes raw Confluence storage XHTML instead.
"""
import argparse, base64, html, json, os, re, sys, urllib.error, urllib.parse, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cfconfig  # noqa: E402

WIKI = "/wiki"
V2 = WIKI + "/api/v2"
V1 = WIKI + "/rest/api"
CONF = {"base_url": "", "email": "", "token": ""}


def die(msg):
    sys.exit(msg)


def configure(instance=None):
    try:
        _name, CONF["base_url"], CONF["email"], CONF["token"] = cfconfig.load_instance(instance)
    except cfconfig.InstanceError as e:
        die(str(e))


def creds():
    base, email, tok = CONF["base_url"].rstrip("/"), CONF["email"], CONF["token"]
    if not (base and email and tok):
        die("Confluence is not configured: call configure() / pass --instance")
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


def short(s, n):
    s = " ".join((s or "").split())
    return s if n <= 0 or len(s) <= n else s[:n] + "…"


def emit(obj, limit=0):
    s = json.dumps(obj, indent=1, ensure_ascii=False)
    print(s if limit <= 0 or len(s) <= limit else s[:limit] + f"\n… truncated ({len(s)} chars, raise --max)")


def project(obj, path):
    """dotted projection: a.b[*].c ; comma separated list of paths -> dict"""
    def one(o, parts):
        if not parts:
            return o
        p, rest = parts[0], parts[1:]
        if p.endswith("[*]"):
            o = o.get(p[:-3]) if isinstance(o, dict) else None
            return [one(x, rest) for x in o] if isinstance(o, list) else None
        if isinstance(o, dict):
            return one(o.get(p), rest)
        return one(o[int(p)], rest) if isinstance(o, list) and p.isdigit() and int(p) < len(o) else None
    paths = [p.strip() for p in path.split(",") if p.strip()]
    if len(paths) == 1:
        return one(obj, paths[0].split("."))
    return {p: one(obj, p.split(".")) for p in paths}


def check_id(v):
    if not re.fullmatch(r"\d{1,20}", str(v)):
        die(f"bad page/comment id {v!r} (numeric id expected)")
    return str(v)


# ---------- storage format <-> text ----------
def storage_to_text(s):
    s = re.sub(r"<ac:structured-macro[^>]*ac:name=\"code\"[^>]*>.*?<ac:plain-text-body><!\[CDATA\[(.*?)\]\]></ac:plain-text-body>.*?</ac:structured-macro>",
               lambda m: "\n```\n" + m.group(1) + "\n```\n", s, flags=re.S)
    s = re.sub(r"<li[^>]*>", "\n- ", s)
    s = re.sub(r"</(p|h[1-6]|tr|div|ul|ol)>|<br\s*/?>", "\n", s)
    s = re.sub(r"</t[dh]>", " | ", s)
    s = html.unescape(re.sub(r"<[^>]+>", "", s))
    return re.sub(r"\n\s*\n+", "\n\n", s).strip()


def _inline(s):
    s = html.escape(s, quote=False)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    return re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', s)


def text_to_storage(t):
    out, para, bullets, code = [], [], [], None

    def flush():
        if para:
            out.append("<p>" + "<br/>".join(_inline(x) for x in para) + "</p>")
            para.clear()
        if bullets:
            out.append("<ul>" + "".join(f"<li>{_inline(b)}</li>" for b in bullets) + "</ul>")
            bullets.clear()
    for line in t.splitlines():
        if line.strip().startswith("```"):
            if code is None:
                flush()
                code = []
            else:
                body = "\n".join(code).replace("]]>", "]]]]><![CDATA[>")
                out.append('<ac:structured-macro ac:name="code"><ac:plain-text-body><![CDATA[' + body + "]]></ac:plain-text-body></ac:structured-macro>")
                code = None
        elif code is not None:
            code.append(line)
        elif not line.strip():
            flush()
        elif re.match(r"^\s*[-*] ", line):
            if para:
                flush()
            bullets.append(re.sub(r"^\s*[-*] ", "", line))
        else:
            if bullets:
                flush()
            para.append(line)
    flush()
    return "".join(out) or "<p></p>"


def read_body(a):
    if a.file:
        try:
            raw = open(os.path.expanduser(a.file)).read()
        except OSError as e:
            die(f"cannot read {a.file}: {e.strerror}")
    elif a.body is not None:
        raw = a.body
    else:
        die("need --body TEXT or --file PATH")
    return raw if a.storage else text_to_storage(raw)


def write(dry, method, path, data, ok):
    if dry:
        print(f"DRY-RUN {method} {path}")
        print(json.dumps(data, ensure_ascii=False)[:1500])
        return None
    r = call(method, path, data=data)
    print(ok(r) if callable(ok) else ok)
    return r


def space_id(key):
    r = call("GET", V2 + "/spaces", {"keys": key, "limit": "1"}).get("results", [])
    if not r:
        die(f"space {key!r} not found (list with: spaces)")
    return r[0]["id"]


# ---------- commands ----------
def page_url(p):
    webui = (p.get("_links") or {}).get("webui", "")
    return CONF["base_url"].rstrip("/") + WIKI + webui if webui else ""


def cmd_search(a):
    r = call("GET", V1 + "/search", {"cql": a.cql, "limit": str(max(1, min(a.limit, 100)))})
    rows = r.get("results", [])
    for x in rows:
        c = x.get("content") or {}
        link = CONF["base_url"].rstrip("/") + WIKI + x["url"] if a.links and x.get("url") else c.get("id", "?")
        space = (x.get("resultGlobalContainer") or {}).get("title", "")
        print(f"{link} [{c.get('type', x.get('entityType', '?'))}] {short(x.get('title'), 90)} | {space} | {(x.get('lastModified') or '')[:10]}")
    print(f"{len(rows)} of {r.get('totalSize', len(rows))} result(s)")


def cmd_page(a):
    pid = check_id(a.id)
    fmt = "storage" if a.format == "text" else a.format
    r = call("GET", f"{V2}/pages/{pid}", {"body-format": fmt})
    body = (r.get("body") or {}).get(fmt, {}).get("value", "")
    if a.format == "text":
        body = storage_to_text(body)
    v = r.get("version") or {}
    print(f"id: {r.get('id')}  title: {r.get('title')}  status: {r.get('status')}  version: {v.get('number')}  "
          f"updated: {(v.get('createdAt') or '')[:16]}  spaceId: {r.get('spaceId')}  parentId: {r.get('parentId') or '-'}")
    if page_url(r):
        print("url:", page_url(r))
    print()
    print(body if a.max_chars <= 0 or len(body) <= a.max_chars else body[:a.max_chars] + f"\n… truncated ({len(body)} chars, raise --max-chars)")


def cmd_children(a):
    for x in call("GET", f"{V2}/pages/{check_id(a.id)}/children", {"limit": str(a.limit)}).get("results", []):
        print(f"{x.get('id')} {short(x.get('title'), 100)} [{x.get('status')}]")


def cmd_comments(a):
    r = call("GET", f"{V2}/pages/{check_id(a.id)}/footer-comments", {"body-format": "storage", "limit": str(a.limit)})
    for x in r.get("results", []):
        v = x.get("version") or {}
        print(f"#{x.get('id')} {(v.get('createdAt') or '')[:16]} {v.get('authorId', '?')}: {short(storage_to_text((x.get('body') or {}).get('storage', {}).get('value', '')), 400)}")


def cmd_attachments(a):
    for x in call("GET", f"{V2}/pages/{check_id(a.id)}/attachments", {"limit": str(a.limit)}).get("results", []):
        print(f"{x.get('id')} {x.get('title')} ({x.get('mediaType')}, {x.get('fileSize')} B)")


def cmd_spaces(a):
    for x in call("GET", V2 + "/spaces", {"limit": str(a.limit)}).get("results", []):
        print(f"{x.get('key')} {short(x.get('name'), 80)} [{x.get('type')}, {x.get('status')}] id={x.get('id')}")


def cmd_create(a):
    body = read_body(a)
    data = {"spaceId": "<resolved>" if a.dry_run else space_id(a.space), "status": "current", "title": a.title,
            "body": {"representation": "storage", "value": body}}
    if a.parent:
        data["parentId"] = check_id(a.parent)
    write(a.dry_run, "POST", V2 + "/pages", data, lambda r: f"OK {r.get('id')} {page_url(r)}")


def cmd_update(a):
    pid = check_id(a.id)
    cur = call("GET", f"{V2}/pages/{pid}")
    n = (cur.get("version") or {}).get("number") or 0
    data = {"id": pid, "status": cur.get("status") or "current", "title": a.title or cur.get("title"),
            "body": {"representation": "storage", "value": read_body(a)},
            "version": {"number": n + 1, "message": a.message or ""}}
    write(a.dry_run, "PUT", f"{V2}/pages/{pid}", data, lambda r: f"OK {pid} version {(r.get('version') or {}).get('number')}")


def cmd_comment(a):
    data = {"pageId": check_id(a.id), "body": {"representation": "storage", "value": read_body(a)}}
    write(a.dry_run, "POST", V2 + "/footer-comments", data, lambda r: f"OK {r.get('id')}")


def cmd_raw(a):
    if not a.path.startswith("/"):
        die("path must start with / (e.g. /wiki/api/v2/spaces)")
    data = json.load(open(a.data_file)) if a.data_file else (json.loads(a.data) if a.data else None)
    q = dict(x.split("=", 1) for x in a.q) or None
    r = call(a.method.upper(), a.path, q, data)
    emit(project(r, a.jq) if a.jq else r, a.max)


def cmd_fetch(a):
    if a.kind != "page":
        die(f"confluence has no kind {a.kind!r}")
    pid = check_id(json.loads(a.ident).get("id", ""))
    p = call("GET", f"{V2}/pages/{pid}")
    sp = call("GET", f"{V2}/spaces/{p['spaceId']}").get("key", "") if p.get("spaceId") else ""
    v = p.get("version") or {}
    author = ""
    if v.get("authorId"):
        try:
            author = call("GET", V1 + "/user", {"accountId": v["authorId"]}).get("displayName") or ""
        except SystemExit:
            pass
    status = p.get("status") or ""
    print(json.dumps({"title": p.get("title") or "", "status": status, "url": page_url(p),
                      "fields": {"space": sp, "version": f"v{v.get('number', '?')}", "updated": (v.get("createdAt") or "")[:10],
                                 "author": author}}))


def cmd_test(a):
    steps, state = [], {}

    def step(name, fn):
        try:
            steps.append({"name": name, "ok": True, "message": fn()})
            return True
        except SystemExit as e:
            steps.append({"name": name, "ok": False, "message": str(e.code)[:200]})
            return False

    def conf():
        configure(a.instance)
        creds()
        return f"instance -> {CONF['base_url']}"

    def who():
        u = call("GET", V1 + "/user/current")
        state["user"] = u.get("displayName") or u.get("email") or "?"
        return state["user"]

    def spaces():
        r = call("GET", V2 + "/spaces", {"limit": "1"}).get("results", [])
        return f"readable ({r[0]['key']})" if r else "reachable, no spaces visible"
    ok = step("config", conf) and step("auth", who) and step("spaces", spaces)
    print(json.dumps({"ok": ok, "message": f"authenticated as {state['user']}" if ok else
                      next(s["message"] for s in steps if not s["ok"]), "steps": steps}))


def build():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instance", default=os.environ.get("CONFLUENCE_INSTANCE"), help="confluence integration instance (config.yaml)")
    sp = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, *args, dry=False, body=False):
        p = sp.add_parser(name)
        for x in args:
            p.add_argument(x)
        if body:
            p.add_argument("--body")
            p.add_argument("--file")
            p.add_argument("--storage", action="store_true")
        if dry:
            p.add_argument("--dry-run", action="store_true")
        p.set_defaults(fn=fn)
        return p
    p = add("search", cmd_search, "cql")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--links", action="store_true", help="print the page URL instead of the id")
    p = add("page", cmd_page, "id")
    p.add_argument("--format", choices=["text", "storage", "view"], default="text")
    p.add_argument("--max-chars", type=int, default=20000)
    for name, fn in (("children", cmd_children), ("comments", cmd_comments), ("attachments", cmd_attachments)):
        add(name, fn, "id").add_argument("--limit", type=int, default=25)
    add("spaces", cmd_spaces).add_argument("--limit", type=int, default=25)
    p = add("create", cmd_create, dry=True, body=True)
    p.add_argument("--space", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--parent")
    p = add("update", cmd_update, "id", dry=True, body=True)
    p.add_argument("--title")
    p.add_argument("--message")
    add("comment", cmd_comment, "id", dry=True, body=True)
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
    try:
        if a.cmd != "test":
            configure(a.instance)
        a.fn(a)
    except BrokenPipeError:
        pass


if __name__ == "__main__":
    main()
