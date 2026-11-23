#!/usr/bin/env python3
"""ServiceNow CMDB watcher (read-only). Script-watch exit codes: 0 = nothing new (silent), 200 = changed (notice
printed), 1 = error (bad config / auth / scope).

Modes (first arg); every mode's first run only records a baseline (exit 0):
  changed  [--class cmdb_ci] [--name TEXT] [--owner TEXT] [--group TEXT] [--where ENCODED] [--limit 50]
           CIs matching the filter that were created (NEW) or updated (CHANGED) since the previous run.
  app      --app NAME_OR_SYS_ID [--depth 3] [--direction down|up|both] [--max-nodes 300]
           CIs that joined (ADDED) or left (REMOVED) an application's dependency set.

Instance: --instance or the CMDB_INSTANCE env (set by the driver); base_url and the token are resolved from config.yaml
by the CLI (never passed through argv/env, never printed). State: $WATCH_STATE_DIR/cmdb-<mode>-<hash of args>.json
(sys_ids, names and timestamps only). 429/5xx/network errors are skipped silently (exit 0) and retried next run.
"""
import argparse
import datetime
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
from pathlib import Path

RC_CHANGED = 200
TRANSIENT = re.compile(r"^(HTTP (429|5\d\d)|network error)")


def load_cli():
    path = Path(__file__).resolve().parents[1] / "cli" / "cli.py"
    spec = importlib.util.spec_from_file_location("cmdb_cli", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def state_path(mode, ident):
    d = Path(os.environ.get("WATCH_STATE_DIR") or tempfile.gettempdir())
    d.mkdir(parents=True, exist_ok=True)
    return d / f"cmdb-{mode}-{hashlib.sha1(ident.encode()).hexdigest()[:12]}.json"


def load(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def save(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def ci_link(cli, row):
    return f"{cli.CONF['base_url']}/nav_to.do?uri=" + cli.urllib.parse.quote(f"{row['class']}.do?sys_id={row['sys_id']}")


def mode_changed(a, cli):
    cls = cli.table_arg(a.cls)
    parts = []
    if a.name:
        parts.append("nameLIKE" + cli.clean(a.name, "--name"))
    if a.owner:
        parts.append("owned_by.nameLIKE" + cli.clean(a.owner, "--owner"))
    if a.group:
        parts.append("support_group.nameLIKE" + cli.clean(a.group, "--group"))
    if a.where:
        parts.append(cli.clean(a.where, "--where"))
    flt = "^".join(parts)
    path = state_path("changed", cls + "|" + flt)
    prev = load(path)
    started = now_utc()
    if prev is None:
        save(path, {"since": started})
        return []
    since = prev["since"]
    q = (flt + "^" if flt else "") + f"sys_updated_on>{since}"
    rows = cli.table(cls, q, cli.CI_FIELDS, max(1, min(a.limit, 200)), "ORDERBYsys_updated_on")
    newest = max([cli.val(r.get("sys_updated_on")) for r in rows] + [since])
    # keep the cursor at the newest seen row when the page was full, otherwise at the start of this scan
    save(path, {"since": newest if len(rows) >= a.limit else max(started, newest)})
    out = []
    for r in rows:
        row = cli.ci_row(r)
        kind = "NEW" if cli.val(r.get("sys_created_on")) > since else "CHANGED"
        out.append(f"{kind} {row['name']} [{row['class']}] {row['status']} owner={row['owner'] or '-'} group={row['group'] or '-'} "
                   f"updated {row['updated']} {ci_link(cli, row)}")
    return out


def mode_app(a, cli):
    root = cli.resolve_app(a.app)
    nodes, _, trunc = cli.walk(root, a.direction, a.depth, a.max_nodes)
    cur = {i: f"{n['name']} [{n['class']}]" for i, n in nodes.items() if i != root["sys_id"]}
    path = state_path("app", f"{root['sys_id']}|{a.depth}|{a.direction}")
    prev = load(path)
    save(path, {"members": cur})
    if prev is None:
        return []
    old = prev.get("members", {})
    out = [f"ADDED {v} {k}" for k, v in cur.items() if k not in old]
    if not trunc:
        out += [f"REMOVED {v} {k}" for k, v in old.items() if k not in cur]
    return [f"App {root['name']}: {len(out)} dependency change(s)"] + out if out else []


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["changed", "app"])
    ap.add_argument("--instance", default=os.environ.get("CMDB_INSTANCE"))
    ap.add_argument("--class", dest="cls", default="cmdb_ci")
    for x in ("name", "owner", "group", "where", "app"):
        ap.add_argument("--" + x)
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--depth", type=int, default=3)
    ap.add_argument("--direction", choices=["down", "up", "both"], default="down")
    ap.add_argument("--max-nodes", type=int, default=300)
    a = ap.parse_args(argv)
    cli = load_cli()
    try:
        cli.configure(a.instance)
        if a.mode == "app" and not a.app:
            sys.exit("app mode needs --app")
        lines = (mode_changed if a.mode == "changed" else mode_app)(a, cli)
    except SystemExit as e:
        msg = str(e.code)
        if TRANSIENT.match(msg):
            return 0
        print(msg, file=sys.stderr)
        return 1
    if not lines:
        return 0
    print("\n".join(lines[:60]))
    return RC_CHANGED


if __name__ == "__main__":
    sys.exit(main())
