#!/usr/bin/env python3
"""Wiz CLI: vulnerability findings, fleet snapshots/diffs and on-demand rescans (part of the `wiz` package driver).

Global options go before the command (set from the instance options by dop-agent; env fallbacks in brackets):
--instance [WIZ_INSTANCE], --wiz-dir [WIZ_DIR], --data-center [WIZ_DATA_CENTER], --scope-tags [WIZ_SCOPE_TAGS], --env-tag-key [WIZ_ENV_TAG_KEY],
--cdp-url [WIZ_CDP_URL]. See README.md.
"""
import argparse
import json
import os
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wiz_api as api  # noqa: E402
import wiz_auth as auth  # noqa: E402
import wiz_furl as furl  # noqa: E402
import wiz_rescan as rescan  # noqa: E402


def _envs(args):
    return api._split(getattr(args, "env", None)) or [None]


def _sev(args, default=None):
    return api._split(args.severity, upper=True) if args.severity else default


def _extra_tags(args):
    return api.parse_tags(",".join(args.tag or []))


def cmd_test(args):
    print(json.dumps(furl.run_test()))


def cmd_fetch(args):
    print(json.dumps(furl.fetch(args.kind, json.loads(args.ident))))


def cmd_auth_status(args):
    st = auth.auth_status()
    print(json.dumps(st, indent=2))
    sys.exit(0 if st.get("ok") else 1)


def cmd_findings(args):
    tmap = json.loads(Path(args.ticket_map).read_text()) if args.ticket_map else None
    rows = api.find_rows(_envs(args), args.host, _sev(args, api.DEFAULT_SEVERITIES), args.status, _extra_tags(args),
                         tmap, max_pages=args.max_pages)
    if args.format == "json":
        print(json.dumps(rows, indent=2))
    elif not rows:
        print("No findings matched.")
    elif args.format == "assets":
        seen = {r["asset_id"]: r["host"] for r in rows if r["asset_id"]}
        for aid, label in sorted(seen.items(), key=lambda kv: kv[1] or ""):
            print(f"{(label or '')[:48]:<48} {aid}")
    elif args.format == "links":
        for r in rows:
            print(f"{r['host']}  {r['severity']:<8}  {r['package']} ({r['cve']})  {r['url']}")
    else:
        head = f"{'HOST':<34} {'SEVERITY':<9} {'PACKAGE':<28} {'CVE/FINDING':<24} {'FIXED':<12} {'TICKETS':<12}"
        print(head + "\n" + "-" * len(head))
        for r in rows:
            print(f"{(r['host'] or '')[:34]:<34} {r['severity'] or '':<9} {(r['package'] or '')[:28]:<28} "
                  f"{(r['cve'] or '')[:24]:<24} {(r['fixed_version'] or '')[:12]:<12} {(r['tickets'] or '')[:12]:<12}")
        print(f"\n{len(rows)} deduped finding(s).")


def cmd_count_by_path(args):
    by = api.count_by_path(_envs(args), args.path, _sev(args, api.DEFAULT_SEVERITIES), args.status,
                           _extra_tags(args), max_pages=args.max_pages)
    print(f"{'HOST':<40} {'TOTAL':<8} MATCHED ({args.path})")
    for name, c in sorted(by.items()):
        print(f"{name[:40]:<40} {c['total']:<8} {c['matched']}")
        if args.show_paths:
            for p in sorted(set(c["paths"]))[:10]:
                print(f"    {p}")


def cmd_snapshot(args):
    if args.snapshot_command == "collect":
        snap = api.collect_snapshot(_envs(args), api._split(args.asset_severities, upper=True), _extra_tags(args))
        for env, d in snap["envs"].items():
            print(f"{env}: {d['total_count']} open findings {d['by_severity']}", file=sys.stderr)
        out = Path(args.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.name + ".tmp")
        tmp.write_text(json.dumps(snap, indent=2))
        os.replace(tmp, out)
        print(f"Snapshot written to {out}")
    else:
        before = json.loads(Path(args.before).expanduser().read_text())
        after = json.loads(Path(args.after).expanduser().read_text())
        print("\n".join(api.diff_snapshots(before, after, args.per_asset)))


def cmd_rescan(args):
    if not args.yes:
        print("DRY RUN (no scan requested). Resources:", ", ".join(args.asset_id), "- add --yes to trigger the rescan.")
        return
    session = rescan.open_session(args.tab_id)
    results = {}
    try:
        for aid in args.asset_id:
            results[aid] = rescan.request_scan(session, aid, args.type)
            print(f"{aid}: {results[aid]['status']} - {results[aid]['message']}")
    finally:
        session.close()
    print(json.dumps(results, indent=2))


def cmd_rescan_status(args):
    ids = dict.fromkeys(args.asset_id or [])
    if args.from_map:
        for aid, info in json.loads(Path(args.from_map).read_text()).items():
            ids[aid] = (info or {}).get("scan_id")
    if not ids:
        sys.exit("error: pass --asset-id or --from-map")
    session = rescan.open_session(args.tab_id)
    results = {}
    try:
        for aid, scan_id in ids.items():
            results[aid] = rescan.scan_activities(session, aid, scan_id)
            r = results[aid]
            state = "ERROR" if r["done"] is None else "DONE" if r["done"] else "STILL RUNNING"
            names = ", ".join(f"{a['name']}={a['status']}" for a in r.get("activities", [])) or r.get("note") or r.get("error")
            print(f"{aid}: {state} ({names})")
    finally:
        session.close()
    print(json.dumps(results, indent=2))


def build():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--instance", default=os.environ.get("WIZ_INSTANCE"), help="integration instance (resolves service-account credentials from config.yaml)")
    p.add_argument("--wiz-dir", default=os.environ.get("WIZ_DIR"), help="wizcli directory holding auth.json (default ~/.wiz)")
    p.add_argument("--data-center", default=os.environ.get("WIZ_DATA_CENTER"), help="override the auth.json data center; 'auto' = use it")
    p.add_argument("--scope-tags", default=os.environ.get("WIZ_SCOPE_TAGS"), help="asset tags applied to every query: k=v,k=v or none")
    p.add_argument("--env-tag-key", default=os.environ.get("WIZ_ENV_TAG_KEY"), help="tag key that --env filters on (default environment)")
    p.add_argument("--cdp-url", default=os.environ.get("WIZ_CDP_URL"), help="browser CDP endpoint for rescan commands")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        s = sub.add_parser(name, help=help_)
        s.set_defaults(fn=fn)
        return s

    def scope(s):
        s.add_argument("--env", help="comma-separated value(s) of the env tag (omit = no env filter)")
        s.add_argument("--tag", action="append", help="extra asset tag key=value (repeatable, ANDed)")
        s.add_argument("--status", default="OPEN")
        s.add_argument("--max-pages", type=int, default=20, help="page cap of 500 findings (truncation is reported)")

    add("test", cmd_test, "check python deps, wizcli, auth file, token and GraphQL (JSON; exit 0 even when a step fails)")
    s = add("fetch", cmd_fetch, "furl card for a Wiz link (used by dop-agent)")
    s.add_argument("--kind", required=True, choices=["issue", "finding"])
    s.add_argument("--ident", required=True)
    add("auth-status", cmd_auth_status, "token validity + GraphQL reachability (exit 1 when not ok)")

    s = add("findings", cmd_findings, "open vulnerability findings (deduped)")
    scope(s)
    s.add_argument("--host", help="client-side substring filter on the asset name")
    s.add_argument("--severity", default="CRITICAL,HIGH", help="comma-separated (CRITICAL,HIGH,MEDIUM,LOW,NONE)")
    s.add_argument("--ticket-map", help="JSON {KEY: {uuid}} mapping posture-issue UUIDs to ticket keys")
    s.add_argument("--format", choices=["table", "json", "links", "assets"], default="table",
                   help="assets: one row per host with its Wiz resource UUID (input for rescan)")
    s = add("count-by-path", cmd_count_by_path, "findings whose locationPath/artifactPackagePath contains a substring, per host")
    scope(s)
    s.add_argument("--path", required=True, help="substring, e.g. log4j-core.jar or /opt/app")
    s.add_argument("--severity", help="default CRITICAL,HIGH")
    s.add_argument("--show-paths", action="store_true")

    s = add("snapshot", cmd_snapshot, "fleet snapshots: collect | diff")
    ss = s.add_subparsers(dest="snapshot_command", required=True)
    c = ss.add_parser("collect")
    c.add_argument("--env", help="comma-separated env tag values (omit = one 'all' group)")
    c.add_argument("--tag", action="append")
    c.add_argument("--asset-severities", default="CRITICAL,HIGH", help="severities broken down per asset")
    c.add_argument("--out", required=True)
    d = ss.add_parser("diff")
    d.add_argument("before")
    d.add_argument("after")
    d.add_argument("--per-asset", action="store_true")

    s = add("rescan", cmd_rescan, "request an on-demand rescan (dry run unless --yes)")
    s.add_argument("--asset-id", action="append", required=True, help="Wiz cloud-resource UUID (repeatable)")
    s.add_argument("--type", default="VIRTUAL_MACHINE")
    s.add_argument("--tab-id")
    s.add_argument("--yes", action="store_true", help="really request the scan")
    s = add("rescan-status", cmd_rescan_status, "have the scan activities of resource(s) finished? (no waiting)")
    s.add_argument("--asset-id", action="append")
    s.add_argument("--from-map", help="JSON {asset_id: {scan_id}}")
    s.add_argument("--tab-id")
    return p


def main():
    args = build().parse_args()
    try:
        auth.configure(args.wiz_dir, args.data_center)
        auth.configure_instance(args.instance)
        api.configure(args.scope_tags, args.env_tag_key)
        rescan.configure(args.cdp_url)
        args.fn(args)
    except (RuntimeError, ValueError, ImportError, FileNotFoundError) as exc:
        sys.exit(f"error: {exc}")
    except (urllib.error.URLError, OSError) as exc:
        sys.exit(f"error: network/HTTP failure talking to Wiz or the browser: {exc}")
    except BrokenPipeError:
        pass


if __name__ == "__main__":
    main()
