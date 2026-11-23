#!/usr/bin/env python3
"""Kargo CLI for the agent: compact JSON over the `kargo` binary. Instance via --instance (token never in argv).

Reads: test, projects, warehouses, freight, freight-info, stages, stage-info, verification, promotions,
promotion-info, analysis-logs, fetch (link card for the web UI).
Writes (need --yes, otherwise a dry-run preview is printed): promote, promote-abort, approve, verify, refresh.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kargoconfig as kc  # noqa: E402
import kargoview as kv  # noqa: E402

MAX_CHARS = 6000


def _out(obj, max_chars=MAX_CHARS):
    s = json.dumps(obj, ensure_ascii=False, indent=1)
    return s if not max_chars or len(s) <= max_chars else s[:max_chars] + f"\n... [cut at {max_chars} chars; narrow the query or raise --max-chars]"


def _get(inst, kind, project=None, names=(), extra=(), scoped=True):
    args = ["get", kind]
    if scoped:
        args.append(f"--project={kc.project_of(inst, project)}")
    args += list(extra)
    args += [kc.check_name(n, kind) for n in names]
    return kc.kargo_json(inst, args)


def cmd_projects(inst, a):
    return _out([kv.name(p) for p in _get(inst, "projects", scoped=False)], a.max_chars)


def cmd_warehouses(inst, a):
    rows = [{"name": kv.name(w), "created": kv.created(w),
             "subscriptions": [next(iter(kv._d(s)), "?") for s in kv._l(kv._d(w.get("spec")).get("subscriptions"))]}
            for w in _get(inst, "warehouses", a.project)]
    return _out(rows, a.max_chars)


def cmd_freight(inst, a):
    extra = [f"--warehouse={kc.check_name(a.warehouse, 'warehouse')}"] if a.warehouse else []
    items = kv.newest_first(_get(inst, "freight", a.project, extra=extra))[: a.limit]
    return _out([kv.freight_summary(f) for f in items], a.max_chars)


def _one_freight(inst, project, ref):
    for flag in ("name", "alias"):
        items = _get(inst, "freight", project, extra=[f"--{flag}={kc.check_name(ref, 'freight')}"])
        if items:
            return items[0]
    raise kc.KargoError(f"freight {ref!r} not found in project {kc.project_of(inst, project)}")


def cmd_freight_info(inst, a):
    f = _one_freight(inst, a.project, a.ref)
    return _out(kv.freight_summary(f), a.max_chars)


def cmd_stages(inst, a):
    return _out([kv.stage_summary(s) for s in _get(inst, "stages", a.project)], a.max_chars)


def cmd_stage_info(inst, a):
    items = _get(inst, "stages", a.project, [a.stage])
    if not items:
        raise kc.KargoError(f"stage {a.stage!r} not found")
    out = kv.stage_summary(items[0])
    conds = [{"type": c.get("type"), "status": c.get("status"), "message": str(c.get("message") or "")[:200]}
             for c in kv._l(kv._d(items[0].get("status")).get("conditions"))]
    out["conditions"] = conds
    out["health_issues"] = kv._d(kv._d(items[0].get("status")).get("health")).get("issues", [])
    return _out(out, a.max_chars)


def cmd_verification(inst, a):
    items = _get(inst, "stages", a.project, [a.stage])
    if not items:
        raise kc.KargoError(f"stage {a.stage!r} not found")
    hist = kv._l(kv._d(items[0].get("status")).get("freightHistory"))
    rows = []
    for coll in hist[: a.limit]:
        for v in kv._l(kv._d(coll).get("verificationHistory"))[:3]:
            rows.append({**kv.verification_summary(kv._d(v)),
                         "freight": [kv.freight_alias(kv._d(r)) or str(kv._d(r).get("name", ""))[:12]
                                     for r in kv._d(kv._d(coll).get("items")).values()]})
    return _out(rows, a.max_chars)


def cmd_promotions(inst, a):
    extra = [f"--stage={kc.check_name(a.stage, 'stage')}"] if a.stage else []
    items = kv.newest_first(_get(inst, "promotions", a.project, extra=extra))
    rows = [kv.promotion_summary(p) for p in items]
    if a.phase:
        rows = [r for r in rows if r["phase"].lower() == a.phase.lower()]
    return _out(rows[: a.limit], a.max_chars)


def cmd_promotion_info(inst, a):
    items = _get(inst, "promotions", a.project, [a.promotion])
    if not items:
        raise kc.KargoError(f"promotion {a.promotion!r} not found")
    out = kv.promotion_summary(items[0])
    out["steps"] = kv.promotion_steps(items[0])
    return _out(out, a.max_chars)


def cmd_analysis_logs(inst, a):
    args = ["logs", f"--project={kc.project_of(inst, a.project)}", kc.check_name(a.analysis_run, "analysis run")]
    if a.metric:
        args.append(f"--metric={kc.check_name(a.metric, 'metric')}")
    out = kc.redact(kc.run_kargo(inst, args, timeout=60), inst[2])
    lines = out.splitlines()[-a.lines:]
    return "\n".join(lines)[-a.max_bytes:]


def _write(inst, a, args, what):
    """Writes are confirm-first: without --yes only a preview of the exact command is printed."""
    shown = " ".join(args)
    if not a.yes:
        return _out({"dry_run": True, "instance": inst[0], "action": what, "would_run": f"kargo {shown}",
                     "hint": "ask the user to confirm, then re-run the same command with --yes"})
    out = kc.run_kargo(inst, args, timeout=a.timeout).strip()
    return _out({"ok": True, "instance": inst[0], "action": what, "output": out[:1500]})


def cmd_promote(inst, a):
    proj = kc.project_of(inst, a.project)
    chosen = [x for x in (a.freight, a.alias, a.warehouse) if x]
    if len(chosen) != 1:
        raise kc.KargoError("give exactly one of --freight, --alias, --warehouse")
    if bool(a.stage) == bool(a.downstream_from):
        raise kc.KargoError("give exactly one of --stage, --downstream-from")
    args = ["promote", f"--project={proj}"]
    for flag, v in (("freight", a.freight), ("freight-alias", a.alias), ("warehouse", a.warehouse),
                    ("stage", a.stage), ("downstream-from", a.downstream_from)):
        if v:
            args.append(f"--{flag}={kc.check_name(v, flag)}")
    if a.wait:
        args.append("--wait")
    return _write(inst, a, args, f"promote {chosen[0]} -> {a.stage or 'downstream of ' + a.downstream_from} in {proj}")


def cmd_promote_abort(inst, a):
    proj = kc.project_of(inst, a.project)
    args = ["promote", f"--project={proj}", f"--name={kc.check_name(a.promotion, 'promotion')}", "--abort"]
    return _write(inst, a, args, f"abort promotion {a.promotion} in {proj}")


def cmd_approve(inst, a):
    proj = kc.project_of(inst, a.project)
    if bool(a.freight) == bool(a.alias):
        raise kc.KargoError("give exactly one of --freight, --alias")
    flag, v = ("freight", a.freight) if a.freight else ("freight-alias", a.alias)
    args = ["approve", f"--project={proj}", f"--{flag}={kc.check_name(v, flag)}",
            f"--stage={kc.check_name(a.stage, 'stage')}"]
    return _write(inst, a, args, f"approve {v} for stage {a.stage} in {proj}")


def cmd_verify(inst, a):
    proj = kc.project_of(inst, a.project)
    args = ["verify", "stage", f"--project={proj}", kc.check_name(a.stage, "stage")]
    if a.abort:
        args.append("--abort")
    return _write(inst, a, args, f"{'abort' if a.abort else 're-run'} verification of stage {a.stage} in {proj}")


def cmd_refresh(inst, a):
    proj = kc.project_of(inst, a.project)
    args = ["refresh", a.kind, f"--project={proj}", kc.check_name(a.name, a.kind)]
    return _write(inst, a, args, f"refresh {a.kind} {a.name} in {proj}")


def cmd_fetch(inst, a):
    """Link card for the web UI: ONE JSON {title, status, status_emoji, url, fields}."""
    ident = json.loads(a.ident)
    proj = ident.get("project", "")
    base = inst[1]
    if a.kind == "stage":
        s = kv.stage_summary(_get(inst, "stages", proj, [ident.get("stage", "")])[0])
        lp = s["last_promotion"]
        return json.dumps({"title": f"Stage {s['name']}", "status": s["health"] or s["phase"],
                           "status_emoji": kv.HEALTH_EMOJI.get(s["health"], "❔"),
                           "url": f"{base}/project/{proj}/stage/{s['name']}",
                           "fields": {"project": proj, "phase": s["phase"], "health": s["health"],
                                      "freight": ", ".join(s["freight"]),
                                      "last_promotion": f"{lp.get('name', '')} ({lp.get('phase', '')})" if lp else ""}})
    if a.kind == "freight":
        f = kv.freight_summary(_one_freight(inst, proj, ident.get("freight", "")))
        return json.dumps({"title": f["alias"] or f["name"][:12], "status": f"verified in {len(f['verified_in'])}",
                           "status_emoji": "📦", "url": f"{base}/project/{proj}/freight/{f['name']}",
                           "fields": {"project": proj, "warehouse": f["warehouse"], "artifacts": "; ".join(f["artifacts"])[:200],
                                      "verified_in": ", ".join(f["verified_in"]), "approved_for": ", ".join(f["approved_for"]),
                                      "created": f["created"]}})
    if a.kind == "promotion":
        p = kv.promotion_summary(_get(inst, "promotions", proj, [ident.get("promotion", "")])[0])
        return json.dumps({"title": p["name"], "status": p["phase"], "status_emoji": kv.PROMO_EMOJI.get(p["phase"], "❔"),
                           "url": f"{base}/project/{proj}/promotion/{p['name']}",
                           "fields": {"project": proj, "stage": p["stage"], "phase": p["phase"], "freight": p["freight"][:12],
                                      "finished": p["finished"]}})
    raise kc.KargoError(f"unknown kind {a.kind!r}")


def cmd_test(_inst, a):
    """Per-step connection test for Settings -> Test; never raises, never prints the token."""
    steps = []
    out = {"ok": False, "message": "", "steps": steps}

    def step(name, ok, detail=""):
        steps.append({"step": name, "ok": ok, "detail": detail})
        return ok

    try:
        inst = kc.load_instance(a.instance)
        step("config", True, f"instance {inst[0]} -> {inst[1]}")
    except kc.InstanceError as exc:
        step("config", False, str(exc))
        out["message"] = str(exc)
        return json.dumps(out)
    try:
        step("binary", True, kc.find_binary())
        ver = kc.run_kargo(inst, ["version"], timeout=30)
        m = re.search(r"Server Version:\s*(\S+)", ver)
        step("server", True, f"Kargo server {m.group(1)}" if m else "reachable")
    except kc.KargoError as exc:
        ok_bin = bool(steps and steps[-1]["step"] == "binary")
        step("server" if ok_bin else "binary", False, str(exc))
        out["message"] = str(exc)
        return json.dumps(out)
    try:
        projects = kv_names(kc.kargo_json(inst, ["get", "projects"], timeout=30))
        step("auth", True, f"{len(projects)} project(s) visible")
    except kc.KargoError as exc:
        step("auth", False, f"{exc} (check auth.token role/expiry)")
        out["message"] = f"token rejected or no access: {exc}"
        return json.dumps(out)
    out.update(ok=True, message=f"connected, {len(projects)} project(s) visible")
    return json.dumps(out)


def kv_names(items):
    return [kv.name(p) for p in items]


def build_parser():
    p = argparse.ArgumentParser(prog="kargo-cli", description=__doc__.splitlines()[0])
    p.add_argument("--instance", default=None)
    p.add_argument("--max-chars", type=int, default=MAX_CHARS, help="cut output (0 = unlimited)")
    s = p.add_subparsers(dest="cmd", required=True)

    def sub(name, *pos, project=True, write=False, **kw):
        x = s.add_parser(name, **kw)
        for q in pos:
            x.add_argument(q)
        if project:
            x.add_argument("--project", "-p")
        if write:
            x.add_argument("--yes", action="store_true", help="really run (otherwise dry-run preview)")
            x.add_argument("--timeout", type=int, default=120)
        return x

    s.add_parser("test")
    f = s.add_parser("fetch", help="link card JSON (web UI): --kind stage|freight|promotion --ident '<json>'")
    f.add_argument("--kind", required=True)
    f.add_argument("--ident", required=True)
    sub("projects", project=False)
    sub("warehouses")
    x = sub("freight"); x.add_argument("--warehouse"); x.add_argument("--limit", type=int, default=15)
    sub("freight-info", "ref")
    sub("stages")
    sub("stage-info", "stage")
    x = sub("verification", "stage"); x.add_argument("--limit", type=int, default=3)
    x = sub("promotions"); x.add_argument("--stage"); x.add_argument("--phase"); x.add_argument("--limit", type=int, default=15)
    sub("promotion-info", "promotion")
    x = sub("analysis-logs", "analysis_run"); x.add_argument("--metric")
    x.add_argument("--lines", type=int, default=80); x.add_argument("--max-bytes", type=int, default=32768)
    x = sub("promote", write=True)
    for flag in ("freight", "alias", "warehouse", "stage", "downstream-from"):
        x.add_argument(f"--{flag}")
    x.add_argument("--wait", action="store_true")
    sub("promote-abort", "promotion", write=True)
    x = sub("approve", write=True); x.add_argument("--freight"); x.add_argument("--alias"); x.add_argument("--stage", required=True)
    x = sub("verify", "stage", write=True); x.add_argument("--abort", action="store_true")
    x = sub("refresh", "kind", "name", write=True); x.set_defaults()
    return p


CMDS = {"projects": cmd_projects, "warehouses": cmd_warehouses, "freight": cmd_freight, "freight-info": cmd_freight_info,
        "stages": cmd_stages, "stage-info": cmd_stage_info, "verification": cmd_verification,
        "promotions": cmd_promotions, "promotion-info": cmd_promotion_info, "analysis-logs": cmd_analysis_logs,
        "promote": cmd_promote, "promote-abort": cmd_promote_abort, "approve": cmd_approve, "verify": cmd_verify,
        "refresh": cmd_refresh, "fetch": cmd_fetch}


def main(argv=None):
    a = build_parser().parse_args(argv)
    try:
        if a.cmd == "test":
            print(cmd_test(None, a))
            return 0
        if a.cmd == "refresh" and a.kind not in ("warehouse", "stage"):
            raise kc.KargoError("refresh kind must be warehouse or stage")
        print(CMDS[a.cmd](kc.load_instance(a.instance), a))
    except (kc.KargoError, kc.InstanceError, json.JSONDecodeError, IndexError) as e:
        print(f"ERROR: {kc.redact(str(e))[:400] or type(e).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
