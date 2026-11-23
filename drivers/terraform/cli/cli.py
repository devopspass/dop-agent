#!/usr/bin/env python3
"""Terraform (HCP Terraform / Terraform Enterprise) CLI for dop-agent. Output is compact; tokens are never printed.

Global: --instance NAME (env TERRAFORM_INSTANCE). Reads (workspaces of the instance's organization):
  test | fetch --kind K --ident JSON
  workspaces [--search S] [--tag T] [--limit 50]      name, last-run status, lock, drift flag
  runs --workspace W [--limit 10] [--status S]        recent runs with plan summary (+add ~change -destroy)
  run RUN_ID                                          one run: status, actions, plan summary, policy checks
  policy RUN_ID                                       policy-check results of a run
  state --workspace W [--values]                      current state version + output names/types (values only for
                                                      non-sensitive outputs and only with --values)
  vars --workspace W                                  variable NAMES/category/sensitive flag (values never shown)
  drift [--workspace W] [--limit 50]                  latest health-assessment (drift) results
Writes (dry run unless --yes; always show the dry run to the user first):
  trigger --workspace W [--message M] [--plan-only] [--yes]
  apply RUN_ID [--comment C] [--yes] | discard RUN_ID [--comment C] [--yes] | cancel RUN_ID [--comment C] [--yes]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tfconfig  # noqa: E402
from tfclient import ATTENTION, ApiError, Client, attr, check_id, check_ws, workspaces_with_runs  # noqa: E402


def out(obj, as_json: bool, lines=None):
    if as_json or lines is None:
        print(json.dumps(obj, indent=1, default=str))
    else:
        print("\n".join(lines))


def fmt_plan(p: dict) -> str:
    return f"+{p.get('add', 0)} ~{p.get('change', 0)} -{p.get('destroy', 0)}" if p else "-"


def run_row(c: Client, run: dict, ws_name: str = "", with_plan: bool = True) -> dict:
    acts = attr(run, "actions", {}) or {}
    plan = c.plan_summary(run) if with_plan and attr(run, "has-changes") is not False else {}
    return {"id": run["id"], "workspace": ws_name, "status": attr(run, "status"), "message": attr(run, "message"),
            "created": attr(run, "created-at"), "plan_only": attr(run, "plan-only"),
            "destroy": attr(run, "is-destroy"), "has_changes": attr(run, "has-changes"),
            "needs_action": bool(acts.get("is-confirmable")) or attr(run, "status") in ATTENTION,
            "confirmable": bool(acts.get("is-confirmable")), "discardable": bool(acts.get("is-discardable")),
            "cancelable": bool(acts.get("is-cancelable")), "plan": plan,
            "url": c.run_url(ws_name, run["id"]) if ws_name else None}


def get_run(c: Client, run_id: str):
    d = c.get(f"runs/{check_id(run_id, 'run-')}", {"include": "workspace"})
    ws = next((i for i in d.get("included") or [] if i.get("type") == "workspaces"), {})
    return d["data"], attr(ws, "name") or ""


def cmd_workspaces(c, a):
    rows = []
    for w, run in workspaces_with_runs(c, a.search or "", a.tag or "")[: a.limit]:
        rows.append({"name": attr(w, "name"), "id": w["id"], "locked": attr(w, "locked"),
                     "terraform_version": attr(w, "terraform-version"), "run_status": attr(run, "status"),
                     "updated": attr(w, "updated-at"), "drift_checks": attr(w, "assessments-enabled")})
    out(rows, a.json, [f"{r['name']:<40} {str(r['run_status'] or '-'):<22} {'LOCKED ' if r['locked'] else ''}"
                       f"tf={r['terraform_version']} {r['id']}" for r in rows] or ["no workspaces"])


def cmd_runs(c, a):
    ws = c.workspace(a.workspace)
    params = {"filter[status]": a.status} if a.status else {}
    data, _ = c.paginate(f"workspaces/{ws['id']}/runs", params, size=min(a.limit, 100), max_pages=1)
    rows = [run_row(c, r, attr(ws, "name")) for r in data[: a.limit]]
    out(rows, a.json, [f"{r['id']} {r['status']:<20} {fmt_plan(r['plan']):<10} {(r['created'] or '')[:19]} "
                       f"{(r['message'] or '')[:50]}" for r in rows] or ["no runs"])


def policy_checks(c, run_id: str) -> list[dict]:
    d = c.get_or_none(f"runs/{check_id(run_id, 'run-')}/policy-checks") or {}
    res = []
    for p in d.get("data") or []:
        r = attr(p, "result", {}) or {}
        res.append({"id": p["id"], "status": attr(p, "status"), "scope": attr(p, "scope"),
                    "passed": r.get("passed"), "failed": r.get("total-failed"),
                    "advisory_failed": r.get("advisory-failed"), "soft_failed": r.get("soft-failed"),
                    "hard_failed": r.get("hard-failed")})
    return res


def cmd_run(c, a):
    run, wsn = get_run(c, a.run_id)
    row = run_row(c, run, wsn)
    row["policy_checks"] = policy_checks(c, a.run_id)
    lines = [f"{row['id']} [{row['status']}] workspace={wsn} plan={fmt_plan(row['plan'])}",
             f"message: {row['message']}", f"created: {row['created']}  destroy={row['destroy']} plan_only={row['plan_only']}",
             f"actions: confirmable={row['confirmable']} discardable={row['discardable']} cancelable={row['cancelable']}",
             f"url: {row['url']}"] + [f"policy {p['id']}: {p['status']} failed={p['failed']}" for p in row["policy_checks"]]
    out(row, a.json, lines)


def cmd_policy(c, a):
    rows = policy_checks(c, a.run_id)
    out(rows, a.json, [f"{p['id']} {p['status']} scope={p['scope']} passed={p['passed']} failed={p['failed']} "
                       f"hard={p['hard_failed']} soft={p['soft_failed']} advisory={p['advisory_failed']}"
                       for p in rows] or ["no policy checks"])


def cmd_state(c, a):
    ws = c.workspace(a.workspace)
    sv = (c.get_or_none(f"workspaces/{ws['id']}/current-state-version") or {}).get("data")
    if not sv:
        return out({"workspace": a.workspace, "state": None}, a.json, ["no state version yet"])
    outs = c.get(f"state-versions/{sv['id']}/outputs", {"page[size]": 100}).get("data") or []
    res = []
    for o in outs:
        sens = bool(attr(o, "sensitive"))
        item = {"name": attr(o, "name"), "type": attr(o, "type"), "sensitive": sens}
        if a.values and not sens:
            item["value"] = attr(o, "value")
        res.append(item)
    info = {"workspace": a.workspace, "state_version": sv["id"], "serial": attr(sv, "serial"),
            "created": attr(sv, "created-at"), "resources": attr(sv, "resources-processed"), "outputs": res}
    out(info, a.json, [f"state {sv['id']} serial={info['serial']} created={info['created']}"] +
       [f"  output {o['name']} ({o['type']}){' SENSITIVE' if o['sensitive'] else ''}"
        + (f" = {json.dumps(o['value'])[:200]}" if "value" in o else "") for o in res])


def cmd_vars(c, a):
    ws = c.workspace(a.workspace)
    data = c.get(f"workspaces/{ws['id']}/vars").get("data") or []
    rows = [{"name": attr(v, "key"), "category": attr(v, "category"), "sensitive": attr(v, "sensitive"),
             "hcl": attr(v, "hcl"), "description": (attr(v, "description") or "")[:80]} for v in data]
    out(rows, a.json, [f"{r['category']:<10} {r['name']}{' (sensitive)' if r['sensitive'] else ''}" for r in rows]
        or ["no variables"])


def assessment(c, w):
    d = c.get_or_none(f"workspaces/{w['id']}/current-assessment-result")
    r = (d or {}).get("data")
    if not r:
        return None
    return {"workspace": attr(w, "name"), "drifted": attr(r, "drifted"), "succeeded": attr(r, "succeeded"),
            "resources_drifted": attr(r, "resources-drifted"), "resources_undrifted": attr(r, "resources-undrifted"),
            "checked": attr(r, "created-at"), "error": attr(r, "error-msg")}


def cmd_drift(c, a):
    rows = []
    if a.workspace:
        ws = [c.workspace(a.workspace)]
    else:
        ws = [w for w, _ in workspaces_with_runs(c) if attr(w, "assessments-enabled")][: a.limit]
    for w in ws:
        r = assessment(c, w)
        if r:
            rows.append(r)
    out(rows, a.json, [f"{r['workspace']:<40} {'DRIFTED' if r['drifted'] else 'ok':<8} "
                       f"drifted={r['resources_drifted']} checked={(r['checked'] or '')[:19]}" for r in rows]
        or ["no assessment results (health assessments disabled or not run yet)"])


def _confirm(a, what: str, detail: dict) -> bool:
    if a.yes:
        return True
    print(f"DRY RUN (nothing sent): would {what}")
    print(json.dumps(detail, indent=1, default=str))
    print("Show this to the user; re-run with --yes only after explicit approval.")
    return False


def cmd_trigger(c, a):
    ws = c.workspace(a.workspace)
    detail = {"workspace": attr(ws, "name"), "id": ws["id"], "plan_only": a.plan_only, "message": a.message,
              "locked": attr(ws, "locked"), "auto_apply": attr(ws, "auto-apply")}
    if not _confirm(a, f"create a {'plan-only ' if a.plan_only else ''}run", detail):
        return
    body = {"data": {"type": "runs", "attributes": {"message": a.message, "plan-only": bool(a.plan_only)},
                     "relationships": {"workspace": {"data": {"type": "workspaces", "id": ws["id"]}}}}}
    r = c.post("runs", body)["data"]
    print(f"created {r['id']} status={attr(r, 'status')} {c.run_url(attr(ws, 'name'), r['id'])}")


def cmd_action(c, a):
    run, wsn = get_run(c, a.run_id)
    row = run_row(c, run, wsn)
    ok_key = {"apply": "confirmable", "discard": "discardable", "cancel": "cancelable"}[a.cmd]
    if not row[ok_key]:
        raise ApiError(f"run {run['id']} (status {row['status']}) is not {ok_key}: refusing to {a.cmd}")
    if not _confirm(a, f"{a.cmd} run {run['id']} in workspace {wsn}",
                    {k: row[k] for k in ("id", "workspace", "status", "destroy", "plan", "message", "url")}):
        return
    c.post(f"runs/{run['id']}/actions/{a.cmd}", {"comment": a.comment} if a.comment else None)
    print(f"{a.cmd} requested for {run['id']}")


def fetch_card(c: Client, kind: str, ident: dict) -> dict:
    if kind == "run":
        run, wsn = get_run(c, ident["id"])
        row = run_row(c, run, wsn)
        return {"title": f"{wsn}: {row['message'] or run['id']}"[:120], "status": row["status"],
                "status_emoji": {"applied": "✅", "planned_and_finished": "✅", "errored": "❌", "canceled": "⛔",
                                 "discarded": "⛔"}.get(row["status"], "🟡" if row["needs_action"] else "⏳"),
                "url": row["url"], "fields": {"plan": fmt_plan(row["plan"]), "created": (row["created"] or "")[:19]}}
    if kind == "workspace":
        ws = c.workspace(ident["name"])
        return {"title": attr(ws, "name"), "status": "locked" if attr(ws, "locked") else "ready",
                "url": f"{c.inst.base_url}/app/{c.inst.organization}/workspaces/{attr(ws, 'name')}",
                "fields": {"terraform": attr(ws, "terraform-version"), "updated": (attr(ws, "updated-at") or "")[:19]}}
    raise ApiError(f"unknown kind {kind!r}")


def run_test(name):
    steps = []

    def step(n, ok, detail):
        steps.append({"name": n, "ok": bool(ok), "detail": detail})
        return ok

    def done(msg):
        return {"ok": all(s["ok"] for s in steps), "message": msg, "steps": steps}

    try:
        inst = tfconfig.load_instance(name)
        step("config", True, f"instance {inst.name} -> {inst.base_url}, organization {inst.organization}")
    except tfconfig.InstanceError as exc:
        step("config", False, str(exc))
        return done(str(exc))
    c = Client(inst)
    try:
        me = c.get("account/details")["data"]
        step("auth", True, f"token accepted ({attr(me, 'username') or 'user/team/org token'})")
    except ApiError as exc:
        if "HTTP 404" in str(exc):  # team/org tokens have no account; org access is checked next
            step("auth", True, "token accepted (team/organization token)")
        else:
            step("auth", False, str(exc))
            return done(f"{exc} — check auth.token scope/expiry")
    try:
        data, _ = c.paginate(f"organizations/{check_ws(inst.organization)}/workspaces", size=20, max_pages=1)
        step("organization", True, f"{len(data)}+ workspaces visible in {inst.organization}")
    except ApiError as exc:
        step("organization", False, str(exc))
        return done(f"organization {inst.organization!r} not readable: {exc}")
    return done(f"{inst.organization}: {len(data)}+ workspaces visible")


def build():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--instance", help="terraform integration instance (env TERRAFORM_INSTANCE)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(n, ws=False, rid=False, write=False, js=True):
        s = sub.add_parser(n)
        if ws:
            s.add_argument("--workspace", required=n != "drift")
        if rid:
            s.add_argument("run_id")
        if write:
            s.add_argument("--yes", action="store_true")
        if js:
            s.add_argument("--json", action="store_true")
        return s

    add("test", js=False)
    s = add("fetch", js=False); s.add_argument("--kind", required=True); s.add_argument("--ident", required=True)
    s = add("workspaces"); s.add_argument("--search"); s.add_argument("--tag"); s.add_argument("--limit", type=int, default=50)
    s = add("runs", ws=True); s.add_argument("--limit", type=int, default=10); s.add_argument("--status")
    add("run", rid=True); add("policy", rid=True)
    s = add("state", ws=True); s.add_argument("--values", action="store_true")
    add("vars", ws=True)
    s = add("drift", ws=True); s.add_argument("--limit", type=int, default=50)
    s = add("trigger", ws=True, write=True); s.add_argument("--message", default="Triggered via dop-agent")
    s.add_argument("--plan-only", action="store_true")
    for n in ("apply", "discard", "cancel"):
        s = add(n, rid=True, write=True, js=False); s.add_argument("--comment")
    return p


def main(argv=None) -> int:
    a = build().parse_args(argv)
    token = ""
    try:
        if a.cmd == "test":
            print(json.dumps(run_test(a.instance)))
            return 0
        inst = tfconfig.load_instance(a.instance)
        token = inst.token
        c = Client(inst)
        if a.cmd == "fetch":
            print(json.dumps(fetch_card(c, a.kind, json.loads(a.ident))))
            return 0
        a.limit = max(1, min(getattr(a, "limit", 50), 200))
        fn = {"workspaces": cmd_workspaces, "runs": cmd_runs, "run": cmd_run, "policy": cmd_policy,
              "state": cmd_state, "vars": cmd_vars, "drift": cmd_drift, "trigger": cmd_trigger}.get(a.cmd, cmd_action)
        fn(c, a)
        return 0
    except (ApiError, tfconfig.InstanceError, ValueError, KeyError) as exc:
        print(f"ERROR: {tfconfig.redact(str(exc)[:400], token)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
