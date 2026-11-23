#!/usr/bin/env python3
"""Power BI dataset refresh watcher (read-only). Script-watch exit codes: 0 = nothing new (silent),
200 = changed (notice printed), 1 = error (login expired / bad args).

Modes (first arg):
  refresh-failed     a refresh of one of --dataset reached status Failed (or Cancelled with --include-cancelled)
  refresh-completed  a refresh of one of --dataset reached status Completed

--dataset WS:DS (repeatable, required): WS = workspace GUID or "me" (My workspace), DS = dataset GUID.
--limit N=10 lines (rest as "+N more"), --history N=10 refreshes inspected per dataset (max 50).

Account / browser: --account / POWERBI_ACCOUNT, POWERBI_CDP_URL. State: $WATCH_STATE_DIR/powerbi-<mode>.json holds
only refresh request ids that were already seen as finished. The first run records a baseline silently (old failures
are not reported). In-progress refreshes (status Unknown) are not recorded until they finish, so they fire once.
Backend: the powerbi driver's cli/api.py (GET only). 401/403/auth => exit 1; 429/5xx => silent skip.
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
KEEP_IDS = 500
MODES = ["refresh-failed", "refresh-completed"]


class Transient(Exception):
    pass


class AuthError(Exception):
    pass


def _api(account):
    if str(CLI_DIR) not in sys.path:
        sys.path.insert(0, str(CLI_DIR))
    import api
    api.configure(None, account)
    return api


def pbi_get(path, account=None):
    """One read-only GET through the driver's api module. The only network seam (tests patch it)."""
    try:
        d = _api(account).request(path)
    except Exception as exc:
        out = str(exc)
        if re.search(r"\b(429|500|502|503|504)\b", out):
            raise Transient(out[:200]) from exc
        if re.search(r"\b(401|403)\b|expired|Unauthorized|not signed in|No unexpired token|CDP endpoint|No tab", out, re.I):
            raise AuthError(f"Power BI auth failed for account {account or 'default'} (tab signed out / no access): "
                            + out[:200]) from exc
        raise RuntimeError(out[:300]) from exc
    if not isinstance(d, dict):
        raise RuntimeError("unexpected Power BI reply")
    return d


def state_path(mode):
    d = Path(os.environ.get("WATCH_STATE_DIR") or Path.home() / ".dop-agent/watch-state/powerbi-default")
    d.mkdir(parents=True, exist_ok=True)
    return d / f"powerbi-{mode}.json"


def load_state(mode):
    p = state_path(mode)
    return json.loads(p.read_text()) if p.exists() else None


def save_state(mode, st):
    p = state_path(mode)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.name)
    with os.fdopen(fd, "w") as f:
        json.dump(st, f)
    os.replace(tmp, p)


def parse_dataset(spec):
    ws, _, ds = spec.partition(":")
    if not ds:
        ws, ds = "me", ws
    guid = re.compile(r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
    if (ws.lower() != "me" and not guid.match(ws)) or not guid.match(ds):
        raise ValueError(f"--dataset must be WORKSPACE_GUID:DATASET_GUID (or me:DATASET_GUID), got {spec[:60]!r}")
    return ws.lower(), ds.lower()


def _err(r):
    raw = r.get("serviceExceptionJson")
    if not raw:
        return ""
    try:
        e = json.loads(raw)
        txt = f"{e.get('errorCode', '')} {e.get('errorDescription', '')}".strip()
    except ValueError:
        txt = str(raw)
    return " - " + txt.replace("\n", " ")[:160]


def run(args, st, first):
    want = {"refresh-failed": {"Failed"} | ({"Cancelled"} if args.include_cancelled else set()),
            "refresh-completed": {"Completed"}}[args.mode]
    seen = set(st.get("seen", []))
    lines, new_seen = [], []
    for spec in args.dataset:
        ws, ds = parse_dataset(spec)
        prefix = "" if ws == "me" else f"/groups/{ws}"
        top = min(max(args.history, 1), 50)
        rows = pbi_get(f"{prefix}/datasets/{ds}/refreshes?$top={top}", args.account).get("value") or []
        for r in rows:
            status, rid = r.get("status"), r.get("requestId") or f"{ds}:{r.get('startTime')}"
            if status in (None, "Unknown"):  # still running: decide when finished
                continue
            if rid in seen:
                continue
            new_seen.append(rid)
            if not first and status in want:
                lines.append(f"{r.get('endTime') or r.get('startTime')} dataset {ds[:8]} (ws {ws[:8]}) refresh "
                             f"{status} [{r.get('refreshType', '')}]{_err(r) if status != 'Completed' else ''}")
    st["seen"] = (list(st.get("seen", [])) + new_seen)[-KEEP_IDS:]
    if first or not lines:
        return 0
    for line in lines[:args.limit]:
        print(line)
    if len(lines) > args.limit:
        print(f"+{len(lines) - args.limit} more")
    return RC_CHANGED


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("mode", choices=MODES)
    ap.add_argument("--account", default=os.environ.get("POWERBI_ACCOUNT"))
    ap.add_argument("--dataset", action="append", required=True, help="WORKSPACE_GUID:DATASET_GUID (repeatable)")
    ap.add_argument("--history", type=int, default=10)
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--include-cancelled", action="store_true")
    args = ap.parse_args(argv)
    st = load_state(args.mode)
    first = st is None
    st = st or {}
    try:
        rc = run(args, st, first)
    except Transient as exc:
        print(f"transient, skipped: {exc}", file=sys.stderr)
        return 0
    except (AuthError, ValueError, RuntimeError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    save_state(args.mode, st)
    return rc


if __name__ == "__main__":
    sys.exit(main())
