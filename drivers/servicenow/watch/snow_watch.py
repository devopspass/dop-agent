#!/usr/bin/env python3
"""ServiceNow watcher (read-only). Script-watch exit codes: 0 = nothing new (silent), 200 = changed (notice printed),
1 = error (bad config / auth / permissions). Every mode's first run only records a baseline (exit 0).

Modes (first arg):
  mine    [--tables incident,change_request,sc_req_item,sc_task,change_task] [--scope assigned|opened|both] [--limit 50]
          Tickets assigned to me (and/or opened by me) updated since the last run (state, assignee, comments, work notes).
  change  [--group G | --mine] [--query ENCODED] [--limit 100]
          Active changes whose state or approval changed, appeared, or left the active set (closed/cancelled).
  group   --group NAME|SYS_ID [--tables incident,sc_task,sc_req_item] [--limit 50]
          New tickets created in an assignment group since the last run.

Instance: --instance or the SNOW_INSTANCE env (set by the driver); base_url, user and token are resolved from config.yaml by
snowapi (never passed through argv/env, never printed). State: $WATCH_STATE_DIR/snow-<mode>-<hash of args>.json (numbers,
state/approval labels and a timestamp only, no ticket text). 429/5xx/network errors are skipped silently (exit 0).
"""
import argparse
import datetime
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

RC_CHANGED = 200
FIELDS = ["number", "short_description", "state", "approval", "assigned_to", "assignment_group", "sys_updated_on", "sys_updated_by",
          "sys_created_on", "sys_id"]
DEFAULT_TABLES = {"mine": "incident,change_request,sc_req_item,sc_task,change_task", "group": "incident,sc_task,sc_req_item"}


def load_cli():
    path = Path(__file__).resolve().parents[1] / "cli" / "cli.py"
    spec = importlib.util.spec_from_file_location("snow_cli", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def state_path(mode, ident):
    d = Path(os.environ.get("WATCH_STATE_DIR") or tempfile.gettempdir())
    d.mkdir(parents=True, exist_ok=True)
    return d / f"snow-{mode}-{hashlib.sha1(ident.encode()).hexdigest()[:12]}.json"


def load(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def save(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def tables_of(a, cli):
    tables = [t.strip() for t in (a.tables or DEFAULT_TABLES[a.mode]).split(",") if t.strip()]
    for t in tables:
        cli.api.check_table(t)
    return tables


def since_scan(a, cli, ts_field, scopes):
    """(timestamp, table, row) of rows whose `ts_field` is newer than the stored timestamp; one query per table and scope
    clause (OR), deduplicated; the timestamp advances to the newest seen row. A None row marks a truncated table."""
    tables = tables_of(a, cli)
    path = state_path(a.mode, f"{'|'.join(scopes)}|{','.join(tables)}")
    prev = load(path)
    if prev is None:
        save(path, {"ts": cli.stamp(cli.utcnow())})
        return []
    last = newest = prev["ts"]
    seen, out = set(), []
    for t in tables:
        for scope in scopes:
            rows = cli.api.table_list(t, f"{ts_field}>{last}" + (("^" + scope) if scope else ""), FIELDS, a.limit, order=ts_field)
            for r in rows:
                key = (t, cli.val(r.get("sys_id")))
                if key not in seen:
                    seen.add(key)
                    raw = str(cli.val(r.get(ts_field)))  # raw UTC value; the display value may use the user's time zone
                    newest = max(newest, raw)
                    out.append((raw, t, r))
            if len(rows) >= a.limit:
                out.append(("", t, None))
    save(path, {"ts": newest})
    return out


def fmt(cli, t, r, what):
    who = cli.disp(r.get("sys_updated_by"))
    return (f"{what} {cli.disp(r.get('number'))} [{cli.disp(r.get('state')) or '-'}] {cli.disp(r.get('short_description'))[:100]}"
            f" — assignee {cli.disp(r.get('assigned_to')) or 'unassigned'}, group {cli.disp(r.get('assignment_group')) or '-'}"
            + (f", by {who}" if what == "UPDATED" and who else "") + f" {cli.link(t, cli.val(r.get('sys_id')))}")


def render(cli, items, what, title):
    out = [fmt(cli, t, r, what) for _, t, r in sorted(items, key=lambda x: x[0]) if r]
    out += [f"(more than the limit in {t}; narrow the scope)" for _, t, r in items if r is None]
    return ([title] + out) if out else []


def mode_mine(a, cli):
    scopes = {"assigned": ["assigned_to=" + cli.ME], "opened": ["opened_by=" + cli.ME]}.get(
        a.scope, ["assigned_to=" + cli.ME, "opened_by=" + cli.ME])
    return render(cli, since_scan(a, cli, "sys_updated_on", scopes), "UPDATED", "ServiceNow tickets changed:")


def mode_group(a, cli):
    if not a.group:
        raise cli.ApiError("group mode needs --group")
    return render(cli, since_scan(a, cli, "sys_created_on", [cli.group_clause(a.group)]), "NEW", f"New ServiceNow tickets in {a.group}:")


def mode_change(a, cli):
    q = ["active=true"]
    if a.group:
        q.append(cli.group_clause(a.group))
    if a.mine:
        q.append("assigned_to=" + cli.ME)
    if a.query:
        q.append(cli.api.check_user_query(a.query))
    rows = cli.api.table_list("change_request", "^".join(q), FIELDS, a.limit, order="number")
    cur = {cli.disp(r["number"]): [cli.disp(r.get("state")), cli.disp(r.get("approval"))] for r in rows}
    path = state_path("change", "|".join(q))
    prev = load(path)
    save(path, {"changes": cur})
    if prev is None:
        return []
    old, out = prev["changes"], []
    byn = {cli.disp(r["number"]): r for r in rows}
    for n, (st, ap) in cur.items():
        if n not in old:
            out.append(fmt(cli, "change_request", byn[n], "NEW") + f" approval={ap or '-'}")
        elif old[n] != [st, ap]:
            out.append(fmt(cli, "change_request", byn[n], "CHANGED") + f" state {old[n][0] or '-'}→{st or '-'}, approval {old[n][1] or '-'}→{ap or '-'}")
    if len(rows) < a.limit:
        out += [f"LEFT   {n} no longer active (was {old[n][0]}, approval {old[n][1] or '-'}); check with `ticket {n}`" for n in old if n not in cur]
    return (["ServiceNow changes:"] + out) if out else []


MODES = {"mine": mode_mine, "change": mode_change, "group": mode_group}


def run(a, cli):
    cli.api.configure(a.instance)
    out = MODES[a.mode](a, cli)
    if not out:
        return 0
    print("\n".join(out))
    return RC_CHANGED


def build():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=sorted(MODES))
    p.add_argument("--instance", default=os.environ.get("SNOW_INSTANCE"))
    p.add_argument("--tables")
    p.add_argument("--scope", choices=["assigned", "opened", "both"], default="both")
    p.add_argument("--group")
    p.add_argument("--mine", action="store_true")
    p.add_argument("--query")
    p.add_argument("--limit", type=int, default=None)
    return p


def main():
    a = build().parse_args()
    if a.limit is None:
        a.limit = {"mine": 50, "change": 100, "group": 50}[a.mode]
    a.limit = max(1, min(a.limit, 200))
    cli = load_cli()
    try:
        sys.exit(run(a, cli))
    except cli.ApiError as e:
        if e.transient:
            sys.exit(0)
        print(cli.api.redact(e), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
