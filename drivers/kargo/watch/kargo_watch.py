#!/usr/bin/env python3
"""Kargo watcher (read-only, runs the `kargo` binary). Script-watch exit codes: 0 = nothing new (silent),
200 = changed (notice printed), 1 = error (instance/token/binary/API failure).

Modes (first arg):
  new-freight           freight not seen before (optionally --warehouse W).
  promotion-result      promotions that reached Succeeded/Failed/Errored/Aborted (optionally --stage S,
                        --only-failed to skip Succeeded).
  stage-health          stages whose health status changed (Healthy/Unhealthy/Progressing/Unknown); a stage that
                        first appears Unhealthy is reported too.
  verification-failed   stages whose latest verification ended Failed/Error/Inconclusive/Aborted.
Scope: --project P, else the instance option default_project, else every project visible to the token
(at most --max-projects, default 20). Per project the newest --limit (default 50) objects are scanned.
Instance: --instance or the KARGO_INSTANCE env (set by the driver). State:
$WATCH_STATE_DIR/kargo-<instance>-<mode>[-<project>].json. The first run only records a baseline (exit 0).
Transient errors (connection refused, timeout, 5xx) are a silent exit 0 with a warning on stderr.
"""
import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

CLI_DIR = Path(__file__).resolve().parents[1] / "cli"
sys.path.insert(0, str(CLI_DIR))
import kargoconfig as kc  # noqa: E402
import kargoview as kv  # noqa: E402

RC_CHANGED = 200
KEEP = 500
TRANSIENT_RE = re.compile(r"(?i)connection refused|timed out|timeout|\b50[0234]\b|no such host|EOF|reset by peer")


class Transient(Exception):
    pass


def state_path(instance, mode, scope=""):
    d = Path(os.environ.get("WATCH_STATE_DIR") or Path.home() / ".dop-agent" / "watch-state" / "kargo-default")
    d.mkdir(parents=True, exist_ok=True)
    tag = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{instance}-{mode}" + (f"-{scope}" if scope else ""))
    return d / f"kargo-{tag}.json"


def load_state(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def save_state(path, st):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".kg.", suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        json.dump(st, fh)
    os.replace(tmp, path)


def kget(inst, args):
    try:
        return kc.kargo_json(inst, args)
    except kc.KargoError as exc:
        if TRANSIENT_RE.search(str(exc)):
            raise Transient(str(exc)) from exc
        raise


def projects_in_scope(inst, args):
    p = args.project or str(inst[3].get("default_project") or "")
    if p:
        return [kc.check_name(p, "project")]
    return [kv.name(x) for x in kget(inst, ["get", "projects"])][: max(1, min(args.max_projects, 100))]


def url(inst, project, kind, name):
    return f"{inst[1]}/project/{project}/{kind}/{name}"


def run_new_freight(inst, project, args, st, first):
    extra = [f"--warehouse={kc.check_name(args.warehouse, 'warehouse')}"] if args.warehouse else []
    items = kv.newest_first(kget(inst, ["get", "freight", f"--project={project}", *extra]))[: args.limit]
    seen = set(st.get("seen", []))
    out = []
    for f in items:
        s = kv.freight_summary(f)
        if not first and s["name"] not in seen:
            out.append(f"📦 new freight {project}/{s['warehouse']}: {s['alias'] or s['name'][:12]} "
                       f"{'; '.join(s['artifacts'][:3])} {url(inst, project, 'freight', s['name'])}")
    st["seen"] = sorted(seen | {f_["name"] for f_ in map(kv.freight_summary, items)})[-KEEP:]
    return out


def run_promotion_result(inst, project, args, st, first):
    extra = [f"--stage={kc.check_name(args.stage, 'stage')}"] if args.stage else []
    items = kv.newest_first(kget(inst, ["get", "promotions", f"--project={project}", *extra]))[: args.limit]
    reported = set(st.get("reported", []))
    out = []
    for p in items:
        s = kv.promotion_summary(p)
        if s["phase"] not in kv.TERMINAL_PROMOTION or s["name"] in reported:
            continue
        reported.add(s["name"])
        if first or (args.only_failed and s["phase"] == "Succeeded"):
            continue
        out.append(f"{kv.PROMO_EMOJI.get(s['phase'], '❔')} promotion {project}/{s['stage']} {s['phase']}: {s['name']} "
                   f"{s['message'][:args.max_chars]} {url(inst, project, 'promotion', s['name'])}".rstrip())
    st["reported"] = sorted(reported)[-KEEP:]
    return out


def run_stage_health(inst, project, args, st, first):
    prev, cur, out = st.get("health", {}), {}, []
    for s in map(kv.stage_summary, kget(inst, ["get", "stages", f"--project={project}"])):
        cur[s["name"]] = s["health"]
        old = prev.get(s["name"])
        if first or old == s["health"]:
            continue
        if old is None and s["health"] in ("Healthy", "Unknown", ""):
            continue
        line = f"{kv.HEALTH_EMOJI.get(s['health'], '❔')} stage {project}/{s['name']} health {old or 'new'} → {s['health'] or 'Unknown'}"
        out.append(f"{line} {url(inst, project, 'stage', s['name'])}")
    st["health"] = cur
    return out


def run_verification_failed(inst, project, args, st, first):
    reported, out = set(st.get("reported", [])), []
    for st_obj in kget(inst, ["get", "stages", f"--project={project}"]):
        s = kv.stage_summary(st_obj)
        v = s["verification"]
        if not v or v["phase"] not in kv.BAD_VERIFICATION:
            continue
        key = f"{project}/{s['name']}/{v['id']}"
        if key in reported:
            continue
        reported.add(key)
        if not first:
            out.append(f"🧪 verification {v['phase']} on {project}/{s['name']} (freight {', '.join(s['freight'])}) "
                       f"{v['message'][:args.max_chars]} analysis run {v['analysis_run']} "
                       f"{url(inst, project, 'stage', s['name'])}")
    st["reported"] = sorted(reported)[-KEEP:]
    return out


MODES = {"new-freight": run_new_freight, "promotion-result": run_promotion_result,
         "stage-health": run_stage_health, "verification-failed": run_verification_failed}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("mode", choices=sorted(MODES))
    ap.add_argument("--project")
    ap.add_argument("--warehouse", help="new-freight: only this warehouse")
    ap.add_argument("--stage", help="promotion-result: only this stage")
    ap.add_argument("--only-failed", action="store_true", help="promotion-result: skip Succeeded")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--max-projects", type=int, default=20)
    ap.add_argument("--max-chars", type=int, default=200)
    ap.add_argument("--instance", default=os.environ.get("KARGO_INSTANCE"))
    args = ap.parse_args(argv)
    args.limit = max(1, min(args.limit, 200))
    lines = []
    try:
        inst = kc.load_instance(args.instance)
        for project in projects_in_scope(inst, args):
            path = state_path(inst[0], args.mode, project)
            st = load_state(path)
            first = st is None
            st = st or {}
            lines += MODES[args.mode](inst, project, args, st, first)
            save_state(path, st)
    except Transient as exc:
        print(f"transient Kargo error, skipping this tick: {kc.redact(str(exc))[:200]}", file=sys.stderr)
        return 0
    except (kc.InstanceError, kc.KargoError, ValueError) as exc:
        print(f"kargo_watch {args.mode} failed: {kc.redact(str(exc))[:300]}", file=sys.stderr)
        return 1
    if lines:
        print("\n".join(lines))
        return RC_CHANGED
    return 0


if __name__ == "__main__":
    sys.exit(main())
