#!/usr/bin/env python3
"""Vault watcher (read-only, metadata only). Script-watch exit codes: 0 = nothing new (silent), 200 = changed
(notice printed), 1 = error (bad args / auth rejected / unknown instance).

Modes (first arg):
  health        the server turns sealed / uninitialized / unreachable, or recovers (state change only; the first run
                records a baseline, but starting in a bad state is reported at once).
  token-expiry  the instance token expires within --days D (default 7); once per token accessor. Non-expiring
                (root/ttl 0) tokens never fire.
  lease-expiry  leases below --prefix expiring within --hours H (default 24), at most --limit (default 100)
                looked up; each lease id is reported once. Needs a token with sys/leases read/list.

Instance: VAULT_INSTANCE (injected from the pinned integration). State: $WATCH_STATE_DIR/vault-<mode>-<key>.json
(states, accessors and lease ids only, never tokens or secrets). Backend: the driver's cli/client.py (guarded,
metadata-only). 5xx/network errors are silent skips except in `health`, where they are the signal.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cli"))

import vaultconfig  # noqa: E402
from client import HEALTH_STATES, ApiError, Client, lease_rows, parse_time, redact, token_info  # noqa: E402

RC_CHANGED = 200
MAX_TRACKED = 2000


def state_file(mode: str, key: str) -> Path:
    d = Path(os.environ.get("WATCH_STATE_DIR") or Path.home() / ".dop-agent" / "watch-state" / "vault")
    d.mkdir(parents=True, exist_ok=True)
    return d / f"vault-{mode}-{hashlib.sha1(key.encode()).hexdigest()[:12]}.json"


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


def mode_health(inst, c, a) -> int:
    try:
        status, d = c.health()
        cur = HEALTH_STATES.get(status, f"http-{status}")
    except ApiError as exc:
        cur, d = "unreachable", {"error": redact(str(exc))[:150]}
    sf = state_file("health", inst.name)
    prev = load_state(sf)
    save_state(sf, {"state": cur})
    healthy = cur in ("active", "standby", "performance-standby", "dr-secondary")
    if prev is None:
        if healthy:
            return 0
    elif prev.get("state") == cur:
        return 0
    print(f"Vault {inst.name} ({inst.base_url}): {(prev or {}).get('state', 'unknown')} -> {cur}"
          + (f" ({d['error']})" if d.get("error") else f", version {d.get('version', '?')}"))
    return RC_CHANGED


def mode_token(inst, c, a) -> int:
    info = token_info(c)
    exp = parse_time(info.get("expire_time"))
    if exp is None:
        return 0
    left = (exp - datetime.now(timezone.utc)).total_seconds()
    if left > a.days * 86400:
        return 0
    sf = state_file("token-expiry", inst.name)
    st = load_state(sf) or {}
    acc = str(info.get("accessor") or "")
    if st.get("accessor") == acc:
        return 0
    save_state(sf, {"accessor": acc})
    print(f"Vault {inst.name}: token (accessor {acc[:8]}..., policies {info.get('policies')}) expires "
          f"{info.get('expire_time')} in {max(left, 0) / 86400:.1f} days; renewable={info.get('renewable')}")
    return RC_CHANGED


def mode_leases(inst, c, a) -> int:
    rows = lease_rows(c, a.prefix, a.limit, a.hours)
    sf = state_file("lease-expiry", f"{inst.name}|{a.prefix}")
    seen = set((load_state(sf) or {}).get("ids") or [])
    new = [r for r in rows if r[0] not in seen]
    if not new:
        return 0
    save_state(sf, {"ids": sorted(seen | {r[0] for r in new})[-MAX_TRACKED:]})
    print(f"Vault {inst.name}: {len(new)} lease(s) under '{a.prefix}' expire within {a.hours:g}h:")
    for lid, ttl, exp, ren in new[:30]:
        print(f"- {lid} ttl={ttl}s expires={exp} renewable={ren}")
    return RC_CHANGED


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=["health", "token-expiry", "lease-expiry"])
    p.add_argument("--instance")
    p.add_argument("--days", type=float, default=7)
    p.add_argument("--hours", type=float, default=24)
    p.add_argument("--prefix", default="")
    p.add_argument("--limit", type=int, default=100)
    a = p.parse_args(argv)
    try:
        inst = vaultconfig.load_instance(a.instance)
        c = Client(inst)
        if a.mode == "health":
            return mode_health(inst, c, a)
        if a.mode == "lease-expiry" and not a.prefix.strip("/"):
            print("ERROR: lease-expiry needs --prefix (a mount path, e.g. aws or database)", file=sys.stderr)
            return 1
        return (mode_token if a.mode == "token-expiry" else mode_leases)(inst, c, a)
    except vaultconfig.InstanceError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except ApiError as exc:
        msg = str(exc)
        if "HTTP 401" in msg or "HTTP 403" in msg or "refused" in msg:
            print(f"ERROR: {redact(msg)[:300]}", file=sys.stderr)
            return 1
        return 0


if __name__ == "__main__":
    sys.exit(main())
