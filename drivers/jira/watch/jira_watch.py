#!/usr/bin/env python3
"""Jira watcher (read-only). Script-watch exit codes: 0 = nothing new (silent), 200 = changed (notice printed),
1 = error (bad config / auth / JQL).

Mode (first arg):
  jql JQL            issues matching JQL that are new to the result set or whose `updated` moved since the last run
                     --new-only   report only issues that were not in the result set before
                     --limit N    max issues fetched and reported (default 30, max 100)

  ticket KEY         one issue: new comments, status transitions, other field changes
                     --match REGEX   only comments whose text matches (case-insensitive)
                     --author NAME   only comments whose author name contains NAME (case-insensitive)

Connection: JIRA_BASE_URL, JIRA_EMAIL, JIRA_TOKEN_FILE (injected from the integration instance; the token is read
from that file and never printed). State: $WATCH_STATE_DIR/jira-jql-<hash of JQL>.json (issue key -> updated) or jira-ticket-<KEY>-<hash of filters>.json
(status, updated, seen comment ids; no text).
The first run only records a baseline (exit 0). 429/5xx/network errors are skipped silently (exit 0) and retried
on the next run; auth/JQL errors exit 1.
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
    spec = importlib.util.spec_from_file_location("jira_cli", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def state_path(jql):
    d = Path(os.environ.get("WATCH_STATE_DIR") or tempfile.gettempdir())
    d.mkdir(parents=True, exist_ok=True)
    return d / f"jira-jql-{hashlib.sha1(jql.encode()).hexdigest()[:12]}.json"


def save(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def run_ticket(a, cli):
    key = a.jql[0].strip().upper() if a.jql else ""
    if not re.fullmatch(r"[A-Z][A-Z0-9]*-\d+", key):
        sys.exit("ticket mode needs a bare issue key like PRJ-123")
    pattern = re.compile(a.match, re.IGNORECASE) if a.match else None
    cli.configure()
    issue = cli.call("GET", f"{cli.API}/issue/{key}", {"fields": "summary,status,updated"})
    f = issue["fields"]
    status, updated = (f.get("status") or {}).get("name") or "", f.get("updated") or ""
    comments = cli.call("GET", f"{cli.API}/issue/{key}/comment", {"maxResults": "100", "orderBy": "created"}).get("comments", [])
    ids = [str(c["id"]) for c in comments]
    d = Path(os.environ.get("WATCH_STATE_DIR") or tempfile.gettempdir())
    d.mkdir(parents=True, exist_ok=True)
    tag = hashlib.sha1(f"{a.match}|{a.author}".encode()).hexdigest()[:8]
    path = d / f"jira-ticket-{key}-{tag}.json"
    try:
        prev = json.loads(path.read_text())
    except (OSError, ValueError):
        prev = None
    save(path, {"status": status, "updated": updated, "comments": ids[-KEEP:]})
    if prev is None:
        return 0
    new = []
    for c in comments:
        if str(c["id"]) in prev.get("comments", []):
            continue
        author = (c.get("author") or {}).get("displayName") or "unknown"
        text = cli.adf_to_text(c.get("body")) or ""
        if a.author and a.author.lower() not in author.lower():
            continue
        if pattern and not pattern.search(text):
            continue
        new.append((author, text))
    status_changed = status != prev.get("status")
    fields_changed = updated != prev.get("updated") and not status_changed and not new
    if not (new or status_changed or fields_changed):
        return 0
    print(f"Jira ticket {key}: {cli.short(f.get('summary'), 100)}")
    if status_changed:
        print(f"Status changed: {prev.get('status')} -> {status}")
    if new:
        print("New comment(s):")
        for author, text in new:
            print(f"- {author}: {cli.short(text.splitlines()[0] if text else '', 200)}")
    if fields_changed:
        print("Ticket fields changed (no status/comment change detected); check description/summary/assignee/labels.")
    print(f"{cli.CONF['base_url'].rstrip('/')}/browse/{key}")
    return RC_CHANGED


def run(a, cli):
    if a.mode == "ticket":
        return run_ticket(a, cli)
    jql = " ".join(a.jql).strip()
    if not jql:
        sys.exit("jql mode needs a JQL string")
    limit = max(1, min(a.limit, 100))
    cli.configure()
    r = cli.call("GET", cli.API + "/search/jql", {"jql": jql, "fields": "summary,status,assignee,updated", "maxResults": str(limit)})
    issues = r.get("issues", [])
    path = state_path(jql)
    try:
        prev = json.loads(path.read_text())
    except (OSError, ValueError):
        prev = None
    cur = {i["key"]: i["fields"].get("updated") or "" for i in issues}
    if prev is None:
        save(path, {"seen": cur})
        return 0
    seen = prev.get("seen", {})
    out = []
    for i in issues:
        k, f = i["key"], i["fields"]
        if k not in seen:
            tag = "new"
        elif not a.new_only and seen[k] != (f.get("updated") or ""):
            tag = "updated"
        else:
            continue
        who = (f.get("assignee") or {}).get("displayName") or "unassigned"
        out.append(f"{k} [{tag}] {(f.get('status') or {}).get('name', '?')} | {who} | {cli.short(f.get('summary'), 100)}"
                   f" — {cli.CONF['base_url'].rstrip('/')}/browse/{k}")
    merged = {**seen, **cur}
    save(path, {"seen": dict(list(merged.items())[-KEEP:])})
    if not out:
        return 0
    print(f"Jira: {len(out)} issue(s) changed for JQL: {cli.short(jql, 120)}")
    print("\n".join(out))
    return RC_CHANGED


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=["jql", "ticket"])
    p.add_argument("jql", nargs="*")
    p.add_argument("--new-only", action="store_true")
    p.add_argument("--match")
    p.add_argument("--author")
    p.add_argument("--limit", type=int, default=30)
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
