#!/usr/bin/env python3
"""Argo CD watcher (read-only). Script-watch exit codes: 0 = nothing new (silent), 200 = changed (notice
printed), 1 = error (instance/CLI/login failure).

Modes (first arg); all use ONE bounded `argocd app list` per run (instance scope project/app_selector applies):
  out-of-sync   apps whose sync status turned OutOfSync (or new apps that are OutOfSync).
  health        apps whose health status changed (Healthy/Progressing/Degraded/Missing/...);
                --only-degraded reports only transitions into Degraded/Missing.
  sync          apps whose last sync operation finished (Succeeded/Failed/Error), once per operation.
Flags: --app NAME (watch one app; name or namespace/name), --project P (narrow within the instance scope),
--selector S, --limit N=200 (apps reported per run).

Instance: --instance or the ARGOCD_INSTANCE env (set by the driver). State:
$WATCH_STATE_DIR/argocd-<instance>-<mode>[-<app>].json. The first run only records a baseline (exit 0).
No credentials involved: auth is the user's `argocd login` session (an expired login is an error, exit 1).
"""
import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

CLI_DIR = Path(__file__).resolve().parents[1] / "cli"
RC_CHANGED = 200
TERMINAL_OPS = {"Succeeded", "Failed", "Error"}
BAD_HEALTH = {"Degraded", "Missing"}


def _client(instance):
    sys.path.insert(0, str(CLI_DIR))
    import argocdconfig
    from argocdclient import ArgoClient
    name, base, opts = argocdconfig.load_instance(instance)
    return name, ArgoClient(base, opts)


def state_path(instance, mode, suffix=""):
    d = Path(os.environ.get("WATCH_STATE_DIR") or Path.home() / ".dop-agent" / "watch-state" / "argocd-default")
    d.mkdir(parents=True, exist_ok=True)
    safe = lambda s: re.sub(r"[^A-Za-z0-9_.-]+", "_", s)  # noqa: E731
    return d / f"argocd-{safe(instance)}-{mode}{'-' + safe(suffix) if suffix else ''}.json"


def load_state(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def save_state(path, st):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".argocd.", suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        json.dump(st, fh)
    os.replace(tmp, path)


def snapshot(app):
    md, spec, st = app.get("metadata") or {}, app.get("spec") or {}, app.get("status") or {}
    op = st.get("operationState") or {}
    return {"name": md.get("name", ""), "project": spec.get("project", ""),
            "sync": (st.get("sync") or {}).get("status", "Unknown"), "health": (st.get("health") or {}).get("status", "Unknown"),
            "rev": ((st.get("sync") or {}).get("revision") or "")[:10], "op": op.get("phase", ""),
            "finished": op.get("finishedAt", ""), "opmsg": " ".join(str(op.get("message", "")).split())[:200]}


def list_apps(c, args):
    from argocdclient import valid_name, valid_selector
    cmd = ["app", "list"]
    proj = args.project or c.project
    if c.project and args.project and args.project != c.project:
        raise ValueError(f"project {args.project!r} is outside this instance's scope")
    if proj:
        cmd += ["-p", valid_name(proj, "project")]
    sel = ",".join(x for x in (c.selector, args.selector) if x)
    if sel:
        cmd += ["-l", valid_selector(sel)]
    snaps = {s["name"]: s for s in map(snapshot, c.json(cmd) or [])}
    if args.app:
        name = valid_name(args.app.rpartition("/")[2], "application name")
        snaps = {k: v for k, v in snaps.items() if k == name}
        if not snaps:
            raise ValueError(f"app {args.app!r} not found (or outside this instance's scope)")
    return snaps


def run(mode, snaps, st, first, args):
    """Compare the snapshot with the stored state; returns notice lines. Mutates st."""
    prev = st.get("apps") or {}
    out = []
    for name, s in sorted(snaps.items()):
        old = prev.get(name)
        if mode == "out-of-sync":
            if s["sync"] == "OutOfSync" and (old is None or old.get("sync") != "OutOfSync"):
                out.append(f"🔄 {name} [{s['project']}] became OutOfSync (health {s['health']}, rev {s['rev'] or '-'})")
            keep = {"sync": s["sync"]}
        elif mode == "health":
            keep = {"health": s["health"]}
            if old is not None and old.get("health") != s["health"] and (not args.only_degraded or s["health"] in BAD_HEALTH):
                out.append(f"{'💔' if s['health'] in BAD_HEALTH else '💚' if s['health'] == 'Healthy' else '🔄'} {name} [{s['project']}] health {old.get('health')} -> {s['health']} (sync {s['sync']})")
        else:
            keep = {"finished": s["finished"]}
            if s["op"] in TERMINAL_OPS and s["finished"] and (old or {}).get("finished") != s["finished"] and (old is not None or not first):
                out.append(f"{'🚀' if s['op'] == 'Succeeded' else '❌'} {name} [{s['project']}] sync {s['op']} at {s['finished']}"
                           + (f": {s['opmsg']}" if s["op"] != "Succeeded" and s["opmsg"] else ""))
        prev[name] = keep
    for gone in set(prev) - set(snaps):
        del prev[gone]
    st["apps"] = prev
    return out[: args.limit]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("mode", choices=["out-of-sync", "health", "sync"])
    ap.add_argument("--app")
    ap.add_argument("--project")
    ap.add_argument("--selector")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--only-degraded", action="store_true")
    ap.add_argument("--instance", default=os.environ.get("ARGOCD_INSTANCE"))
    args = ap.parse_args(argv)
    args.limit = max(1, min(args.limit, 500))
    try:
        name, c = _client(args.instance)
        path = state_path(name, args.mode, args.app or "")
        st = load_state(path)
        first = st is None
        st = st or {}
        lines = run(args.mode, list_apps(c, args), st, first, args)
    except Exception as exc:
        print(f"argocd_watch {args.mode} failed: {str(exc)[:300]}", file=sys.stderr)
        return 1
    save_state(path, st)
    if lines and not first:
        print("\n".join(lines))
        return RC_CHANGED
    return 0


if __name__ == "__main__":
    sys.exit(main())
