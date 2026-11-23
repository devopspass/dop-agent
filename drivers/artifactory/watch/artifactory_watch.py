#!/usr/bin/env python3
"""Artifactory watcher (read-only). Script-watch exit codes: 0 = nothing new (silent), 200 = changed (notice
printed), 1 = error (bad args / auth rejected / unknown instance).

Modes (first arg):
  files         new (and, unless --new-only, overwritten) files below --path REPO/DIR (--depth N, default 1;
                --name GLOB filters file names, repeatable). Overwrites are detected by sha1/size/lastModified change.
  token-expiry  the instance token (access_token / sso_token) expires within --days D (default 30); once per token.
  health        the server stops / resumes answering /api/system/ping (state change only).

Instance: ARTIFACTORY_INSTANCE (injected from the pinned integration). State: $WATCH_STATE_DIR/artifactory-<mode>-<key>.json
(file uris + checksums only). The first run records a baseline (exit 0). 5xx/429/network errors => silent skip
(except in `health`, where they are the signal). 401/403 => exit 1.
Backend: the driver's cli/client.py (GET only).
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

CLI_DIR = Path(__file__).resolve().parents[1] / "cli"
sys.path.insert(0, str(CLI_DIR))

import afconfig  # noqa: E402
from client import ApiError, Client, clean_path  # noqa: E402

RC_CHANGED = 200
MAX_TRACKED = 5000


def state_file(mode: str, key: str) -> Path:
    d = Path(os.environ.get("WATCH_STATE_DIR") or Path.home() / ".dop-agent" / "watch-state" / "artifactory")
    d.mkdir(parents=True, exist_ok=True)
    return d / f"artifactory-{mode}-{hashlib.sha1(key.encode()).hexdigest()[:12]}.json"


def load_state(p: Path):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def save_state(p: Path, data) -> None:
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".st")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, p)


def _fingerprint(f: dict) -> str:
    return f"{f.get('sha1') or ''}|{f.get('size')}|{f.get('lastModified') or ''}"


def run_files(c: Client, a) -> int:
    path = clean_path(a.path)
    sf = state_file("files", f"{c.inst.name}|{path}|{a.depth}|{sorted(a.name or [])}")
    files = c.list_files(path, a.depth, MAX_TRACKED)
    if a.name:
        files = [f for f in files if any(fnmatch.fnmatch(f["uri"].rsplit("/", 1)[-1], g) for g in a.name)]
    cur = {f["uri"]: _fingerprint(f) for f in files}
    old = load_state(sf)
    save_state(sf, cur)
    if old is None:
        return 0
    new = [u for u in cur if u not in old]
    changed = [] if a.new_only else [u for u in cur if u in old and old[u] != cur[u]]
    if not new and not changed:
        return 0
    meta = {f["uri"]: f for f in files}
    print(f"Artifactory {c.inst.name}: {len(new)} new, {len(changed)} overwritten under {path}")
    for label, uris in (("new", new), ("OVERWRITTEN", changed)):
        for u in sorted(uris)[: a.limit]:
            print(f"  {label}: {path}{u} ({meta[u].get('size', '?')} bytes, {str(meta[u].get('lastModified', ''))[:19]})")
        if len(uris) > a.limit:
            print(f"  ... +{len(uris) - a.limit} more {label}")
    return RC_CHANGED


def run_token_expiry(c: Client, a) -> int:
    if not c.inst.token:
        raise ApiError(f"instance {c.inst.name} has no token (auth.type {c.inst.auth_type}); nothing to watch")
    info = c.token_info()
    if not info or not info.get("valid"):
        raise ApiError(f"token rejected by the server (HTTP {(info or {}).get('http', '?')}): renew it")
    exp = info.get("expiry")
    if not exp:
        return 0
    sf = state_file("token-expiry", c.inst.name)
    st = load_state(sf) or {}
    days = int((exp - time.time()) / 86400)
    if days > a.days or st.get("notified_expiry") == exp:
        return 0
    save_state(sf, {"notified_expiry": exp})
    print(f"Artifactory {c.inst.name}: token expires in {days} days ({time.strftime('%Y-%m-%d', time.localtime(exp))}); renew it")
    return RC_CHANGED


def run_health(c: Client, a) -> int:
    try:
        r = c.ping()
        up = r.status_code == 200 and r.text.strip().upper() == "OK"
        detail = f"HTTP {r.status_code}"
    except ApiError as exc:
        up, detail = False, str(exc)
    sf = state_file("health", c.inst.name)
    old = load_state(sf)
    save_state(sf, {"up": up})
    if old is None or old.get("up") == up:
        return 0
    print(f"Artifactory {c.inst.name} is {'UP again' if up else 'DOWN'} ({detail})")
    return RC_CHANGED


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("mode", choices=["files", "token-expiry", "health"])
    ap.add_argument("--instance", default=os.environ.get("ARTIFACTORY_INSTANCE"))
    ap.add_argument("--path", help="files: REPO/DIR to watch")
    ap.add_argument("--name", action="append", help="files: file-name glob (repeatable)")
    ap.add_argument("--depth", type=int, default=1)
    ap.add_argument("--new-only", action="store_true", help="files: ignore overwrites")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--days", type=int, default=30)
    a = ap.parse_args(argv)
    try:
        c = Client(afconfig.load_instance(a.instance))
        if a.mode == "files":
            if not a.path:
                raise ApiError("files mode needs --path REPO/DIR")
            return run_files(c, a)
        return run_token_expiry(c, a) if a.mode == "token-expiry" else run_health(c, a)
    except afconfig.InstanceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ApiError as exc:
        msg = str(exc)
        if any(f"HTTP {code}" in msg for code in (401, 403, 404)) or "invalid repo path" in msg or "needs" in msg \
                or "no token" in msg or "rejected" in msg:
            print(f"error: {msg}", file=sys.stderr)
            return 1
        print(f"transient, skipped: {msg}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
