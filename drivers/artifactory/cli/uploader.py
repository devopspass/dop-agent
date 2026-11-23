"""Uploads. Write actions: the CLI only calls these with --yes (otherwise it prints the plan).

mode `rest`        PUT <api>/<repo/path/name> (standard deploy; needs write permission on the target repo).
Refuses to overwrite an existing artifact unless force=True.
"""
from __future__ import annotations

from pathlib import Path

from client import ApiError, Client, clean_path, sha256_of


def plan(client: Client, src: Path, target: str, name: str | None, force: bool) -> dict:
    if not src.is_file():
        raise ApiError(f"no such file: {src}")
    target = clean_path(target)
    name = name or src.name
    if "/" in name or name in (".", ".."):
        raise ApiError(f"invalid --name {name!r}")
    dest = f"{target}/{name}"
    existing = client.storage(dest)
    exists = existing.status_code == 200
    if exists and not force:
        raise ApiError(f"REFUSING: {dest} already exists (size {existing.json().get('size')}); pick a new versioned "
                       f"name with --name or pass --force to overwrite")
    return {"dest": dest, "target": target, "name": name, "size": src.stat().st_size,
            "existing": existing.json() if exists else None}


def upload_rest(client: Client, src: Path, p: dict) -> dict:
    with open(src, "rb") as f:
        r = client.request("PUT", p["dest"], data=f, timeout=max(300, p["size"] // (1 << 20) * 2),
                           headers={"X-Checksum-Sha256": sha256_of(src)})
    if r.status_code not in (200, 201):
        raise ApiError(f"HTTP {r.status_code}: {r.text[:200]}"
                       + (" (no write permission on that repo)" if r.status_code in (401, 403) else ""))
    return {"ok": True, "dest": p["dest"], "size": p["size"], "sha256_match": _verify(client, src, p["dest"])}


def _verify(client: Client, src: Path, dest: str) -> bool:
    return client.storage(dest).json().get("checksums", {}).get("sha256") == sha256_of(src)

