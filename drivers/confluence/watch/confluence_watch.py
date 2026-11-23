#!/usr/bin/env python3
"""Confluence watcher (read-only). Script-watch exit codes: 0 = nothing new (silent), 200 = changed (notice printed),
1 = error (bad config / auth / CQL).

Modes (first arg):
  cql CQL         pages/blog posts matching a CQL that are new to the result set or whose version moved since the last run
                  (`order by lastmodified desc` is appended when the CQL has no ORDER BY)
                  --new-only   report only content that was not in the result set before
                  --limit N    max items fetched and reported (default 30, max 100)
  space KEY       same as cql for `space = "KEY" AND type in (page,blogpost)`
  page ID         one page: new version and/or new footer comments since the last run

Instance: --instance or CONFLUENCE_INSTANCE (set by the driver); site, account and token are resolved from config.yaml
(auth.token ref) by the CLI's cfconfig.py and never printed. State: $WATCH_STATE_DIR/confluence-<instance>-<mode>-<hash>.json (ids -> version
numbers, no text). The first run only records a baseline (exit 0). 429/5xx/network errors are skipped silently (exit 0)
and retried on the next run; auth/CQL errors exit 1.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
from pathlib import Path

RC_CHANGED = 200
KEEP = 1000
TRANSIENT = re.compile(r"^(HTTP (429|5\d\d)|network error)")


def load_cli():
    path = Path(__file__).resolve().parents[1] / "cli" / "cli.py"
    spec = importlib.util.spec_from_file_location("confluence_cli", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def state_path(mode, key, instance=""):
    d = Path(os.environ.get("WATCH_STATE_DIR") or tempfile.gettempdir())
    d.mkdir(parents=True, exist_ok=True)
    return d / f"confluence-{re.sub(r'[^A-Za-z0-9_.-]+', '_', instance)}-{mode}-{hashlib.sha1(key.encode()).hexdigest()[:12]}.json"


def load(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def save(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def run_cql(cql, a, cli):
    cql = cql.strip()
    if not cql:
        sys.exit("cql mode needs a CQL string")
    if not re.search(r"\border\s+by\b", cql, re.I):
        cql += " order by lastmodified desc"
    limit = max(1, min(a.limit, 100))
    r = cli.call("GET", cli.V1 + "/content/search", {"cql": cql, "limit": str(limit), "expand": "version,space"})
    items = r.get("results", [])
    path = state_path("cql", cql, cli.INSTANCE)
    prev = load(path)
    cur = {i["id"]: (i.get("version") or {}).get("number", 0) for i in items}
    if prev is None:
        save(path, {"seen": cur})
        return 0
    seen = prev.get("seen", {})
    out = []
    for i in items:
        n = cur[i["id"]]
        if i["id"] not in seen:
            tag = "new"
        elif not a.new_only and seen[i["id"]] != n:
            tag = "updated"
        else:
            continue
        v = i.get("version") or {}
        link = cli.CONF["base_url"].rstrip("/") + cli.WIKI + (i.get("_links") or {}).get("webui", "")
        out.append(f"{i['id']} [{tag}] {cli.short(i.get('title'), 100)} | {(i.get('space') or {}).get('key', '?')} | "
                   f"v{n} by {(v.get('by') or {}).get('displayName', '?')} {(v.get('when') or '')[:16]} — {link}")
    merged = {**seen, **cur}
    save(path, {"seen": dict(list(merged.items())[-KEEP:])})
    if not out:
        return 0
    print(f"Confluence: {len(out)} item(s) changed for CQL: {cli.short(cql, 120)}")
    print("\n".join(out))
    return RC_CHANGED


def run_page(pid, cli):
    pid = cli.check_id(pid)
    p = cli.call("GET", f"{cli.V2}/pages/{pid}")
    cs = cli.call("GET", f"{cli.V2}/pages/{pid}/footer-comments", {"body-format": "storage", "limit": "50", "sort": "-created-date"}).get("results", [])
    ver = (p.get("version") or {}).get("number", 0)
    ids = [c["id"] for c in cs]
    path = state_path("page", pid, cli.INSTANCE)
    prev = load(path)
    save(path, {"version": ver, "comments": list(dict.fromkeys(ids + (prev or {}).get("comments", [])))[:KEEP]})
    if prev is None:
        return 0
    out = []
    if ver != prev.get("version"):
        v = p.get("version") or {}
        out.append(f"new version v{prev.get('version')} -> v{ver} {(v.get('createdAt') or '')[:16]} by {v.get('authorId', '?')}"
                   + (f" ({cli.short(v['message'], 80)})" if v.get("message") else ""))
    old = set(prev.get("comments", []))
    for c in cs:
        if c["id"] not in old:
            body = cli.storage_to_text(((c.get("body") or {}).get("storage") or {}).get("value", ""))
            out.append(f"new comment #{c['id']} by {(c.get('version') or {}).get('authorId', '?')}: {cli.short(body, 300)}")
    if not out:
        return 0
    print(f"Confluence page {pid} \"{cli.short(p.get('title'), 100)}\": {len(out)} change(s) — {cli.page_url(p)}")
    print("\n".join(out))
    return RC_CHANGED


def run(a, cli):
    cli.configure(a.instance)
    cli.INSTANCE = a.instance or os.environ.get("CONFLUENCE_INSTANCE") or ""
    if a.mode == "page":
        if len(a.arg) != 1:
            sys.exit("page mode needs exactly one page id")
        return run_page(a.arg[0], cli)
    text = " ".join(a.arg)
    if a.mode == "space":
        if not re.fullmatch(r"[A-Za-z0-9~_\-]+", text):
            sys.exit("space mode needs one space key")
        text = f'space = "{text}" AND type in (page,blogpost)'
    return run_cql(text, a, cli)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=["cql", "space", "page"])
    p.add_argument("arg", nargs="*")
    p.add_argument("--new-only", action="store_true")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--instance", default=os.environ.get("CONFLUENCE_INSTANCE"))
    a = p.parse_args()
    cli = load_cli()
    try:
        sys.exit(run(a, cli))
    except SystemExit as e:
        if e.code in (0, RC_CHANGED, None):
            raise
        if isinstance(e.code, str) and TRANSIENT.match(e.code):
            sys.exit(0)
        print(e.code, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
