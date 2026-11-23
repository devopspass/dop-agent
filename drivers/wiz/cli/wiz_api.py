"""Wiz vulnerability-findings queries and snapshots (read-only GraphQL via auth.graphql).

Scope: every query is limited to the assets carrying ALL tags of the instance's `scope_tags` (key=value list, e.g.
`application=billing`), plus - when `--env` is given - the tag `<env_tag_key>=<env>` (default key `environment`).

Wiz GraphQL quirks (live-verified):
- `vulnerabilityFindings.totalCount` is 0 when no `severity` filter is sent (cost guard) although `nodes` page fine:
  never read an unfiltered totalCount of 0 as "no findings"; `count_findings()` always passes a severity.
- An explicit `"severity": null` / `"status": null` behaves worse than omitting the key: `build_filter()` only adds
  keys that have a value.
- Unknown-field schema errors come back as HTTP 400 with a normal JSON error body (handled in auth.graphql).
- The severity enum's lowest bucket is NONE (not INFORMATIONAL).
"""
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone

import wiz_auth as auth

DEFAULT_SEVERITIES = ["CRITICAL", "HIGH"]
SEVERITY_ORDER = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "NONE"]

scope_tags: list[tuple[str, str]] = []
env_tag_key = "environment"

FINDINGS_QUERY = """
query GetFindings($filterBy: VulnerabilityFindingFilters, $first: Int, $after: String) {
  vulnerabilityFindings(first: $first, after: $after, filterBy: $filterBy) {
    totalCount
    pageInfo { hasNextPage endCursor }
    nodes {
      id name severity fixedVersion version status portalUrl detailedName
      postureIssuesV2 { id }
      locationPath artifactPackagePath
      vulnerableAsset {
        __typename
        ... on VulnerableAssetVirtualMachine { id name providerUniqueId tags }
      }
    }
  }
}
"""


def parse_tags(text):
    """'k=v,k2=v2' (or 'none'/'') -> [(k, v), ...]."""
    out = []
    for part in (text or "").split(","):
        part = part.strip()
        if not part or part.lower() == "none":
            continue
        k, sep, v = part.partition("=")
        if not sep or not k or not v:
            raise ValueError(f"bad tag {part!r}: expected key=value")
        out.append((k, v))
    return out


def configure(scope=None, env_key=None):
    global scope_tags, env_tag_key
    scope_tags = parse_tags(scope) if isinstance(scope, str) else list(scope or [])
    if env_key:
        env_tag_key = env_key


def _split(text, upper=False):
    vals = [s.strip() for s in (text or "").split(",") if s.strip()]
    return [v.upper() for v in vals] if upper else vals


def build_filter(env=None, extra_tags=(), severities=None, status="OPEN"):
    tags = list(scope_tags) + ([(env_tag_key, env)] if env else []) + list(extra_tags)
    filter_by = {}
    if tags:
        filter_by["assetTags"] = {"containsAll": [{"key": k, "value": v} for k, v in tags]}
    if status:
        filter_by["status"] = [status]
    if severities:
        filter_by["severity"] = list(severities)
    return filter_by


def _check(result):
    if result.get("errors"):
        raise auth.WizAuthError("GraphQL errors: " + json.dumps(result["errors"])[:600])
    return result["data"]["vulnerabilityFindings"]


def fetch_findings(env=None, extra_tags=(), severities=None, status="OPEN", ticket_map=None,
                   page_size=500, max_pages=20):
    """Page through findings. Returns (nodes, api_total_count, uuid_to_key, truncated); `truncated` is True (and a
    warning goes to stderr) when max_pages ran out while more pages existed, i.e. `nodes` is known-incomplete."""
    filter_by = build_filter(env, extra_tags, severities, status)
    uuid_to_key = {}
    if ticket_map:
        uuid_to_key = {v["uuid"]: k for k, v in ticket_map.items() if v.get("uuid")}
        filter_by["relatedPostureIssue"] = {"id": {"equals": list(uuid_to_key)}}
    nodes, after, total, truncated = [], None, None, False
    for _ in range(max_pages):
        data = _check(auth.graphql(FINDINGS_QUERY, {"filterBy": filter_by, "first": page_size, "after": after}))
        total = data["totalCount"]
        nodes.extend(data["nodes"])
        info = data.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            break
        after = info.get("endCursor")
    else:
        truncated = True
        print(f"WARNING: hit max_pages={max_pages} (page_size={page_size}) with more pages available - results are "
              "TRUNCATED. Narrow the filters (env/severity/host/tag).", file=sys.stderr)
    return nodes, total, uuid_to_key, truncated


def count_findings(env, severity, extra_tags=(), status="OPEN"):
    """Cheap exact count for one severity (first:1, totalCount is accurate with a severity filter)."""
    f = build_filter(env, extra_tags, [severity], status)
    return _check(auth.graphql(FINDINGS_QUERY, {"filterBy": f, "first": 1, "after": None}))["totalCount"]


def rows_from_nodes(nodes, uuid_to_key=None):
    """Dedupe + flatten VM findings into report rows (key: asset, detection, package/cve, ticket keys)."""
    uuid_to_key = uuid_to_key or {}
    seen = {}
    for n in nodes:
        a = n.get("vulnerableAsset") or {}
        if a.get("__typename") != "VulnerableAssetVirtualMachine":
            continue
        keys = tuple(sorted(uuid_to_key.get(pi["id"], "?") for pi in (n.get("postureIssuesV2") or [])))
        seen.setdefault((a.get("name"), (a.get("providerUniqueId") or "").split("/")[-1],
                         n.get("detailedName"), n.get("name"), keys), n)
    rows = []
    for (name, iid, pkg, cve, keys), n in seen.items():
        rows.append({"id": n.get("id"), "host": name, "instance_id": iid,
                     "asset_id": (n.get("vulnerableAsset") or {}).get("id"), "package": pkg, "cve": cve,
                     "severity": n.get("severity"), "version": n.get("version"), "fixed_version": n.get("fixedVersion"),
                     "tickets": ",".join(k for k in keys if k != "?"), "url": n.get("portalUrl")})
    rows.sort(key=lambda r: (r["host"] or "", SEVERITY_ORDER.index(r["severity"]) if r["severity"] in SEVERITY_ORDER else 99,
                             r["package"] or ""))
    return rows


def find_rows(envs, host=None, severities=None, status="OPEN", extra_tags=(), ticket_map=None, max_pages=20):
    """Rows over several env values ([None] = no env tag filter), optional host substring filter."""
    out = []
    for env in envs or [None]:
        nodes, _, u2k, _ = fetch_findings(env, extra_tags, severities, status, ticket_map, max_pages=max_pages)
        rows = rows_from_nodes(nodes, u2k)
        if host:
            rows = [r for r in rows if host.lower() in (r["host"] or "").lower()]
        out.extend(rows)
    return out


def count_by_path(envs, path, severities, status, extra_tags=(), max_pages=20):
    """{host: {total, matched, paths}} - findings whose locationPath/artifactPackagePath contains `path`."""
    needle = path.lower()
    by = defaultdict(lambda: {"total": 0, "matched": 0, "paths": []})
    for env in envs or [None]:
        nodes, _, _, _ = fetch_findings(env, extra_tags, severities, status, max_pages=max_pages)
        for n in nodes:
            name = (n.get("vulnerableAsset") or {}).get("name") or "unknown"
            by[name]["total"] += 1
            paths = [p for p in (n.get("locationPath"), n.get("artifactPackagePath")) if p]
            if any(needle in p.lower() for p in paths):
                by[name]["matched"] += 1
                by[name]["paths"].extend(paths)
    return by


def collect_snapshot(envs, asset_severities, extra_tags=()):
    """Snapshot: exact per-severity counts (cheap) + per-asset breakdown for the actionable severities only."""
    snap = {"generated_at": datetime.now(timezone.utc).isoformat(), "scope_tags": [f"{k}={v}" for k, v in scope_tags],
            "env_tag_key": env_tag_key, "asset_breakdown_severities": asset_severities, "envs": {}}
    for env in envs or [None]:
        by_sev = {s: count_findings(env, s, extra_tags) for s in SEVERITY_ORDER}
        by_asset = defaultdict(lambda: defaultdict(int))
        nodes, _, _, _ = fetch_findings(env, extra_tags, asset_severities)
        for n in nodes:
            by_asset[(n.get("vulnerableAsset") or {}).get("name") or "unknown"][n.get("severity") or "UNKNOWN"] += 1
        snap["envs"][env or "all"] = {"total_count": sum(by_sev.values()), "by_severity": by_sev,
                                      "assets": {k: dict(v) for k, v in by_asset.items()}}
    return snap


def _delta(b, a):
    return {s: a.get(s, 0) - b.get(s, 0) for s in sorted(set(a) | set(b)) if a.get(s, 0) != b.get(s, 0)}


def diff_snapshots(before, after, per_asset=False):
    """Text lines describing the change between two snapshots."""
    lines = [f"{'ENV':<14} {'BEFORE':<8} {'AFTER':<8} {'DELTA':<8} BY SEVERITY"]
    envs = sorted(set(before.get("envs", {})) | set(after.get("envs", {})))
    for env in envs:
        b, a = before.get("envs", {}).get(env, {}), after.get("envs", {}).get(env, {})
        bt, at = b.get("total_count", 0), a.get("total_count", 0)
        sev = ", ".join(f"{k}:{v:+d}" for k, v in _delta(b.get("by_severity", {}), a.get("by_severity", {})).items())
        lines.append(f"{env:<14} {bt:<8} {at:<8} {at - bt:+d}".ljust(46) + f" {sev}")
    if per_asset:
        lines.append("\nPer-asset changes:")
        n0 = len(lines)
        for env in envs:
            ba = before.get("envs", {}).get(env, {}).get("assets", {})
            aa = after.get("envs", {}).get(env, {}).get("assets", {})
            for asset in sorted(set(ba) | set(aa)):
                d = _delta(ba.get(asset, {}), aa.get(asset, {}))
                if asset not in ba:
                    lines.append(f"  {env}/{asset}: NEW ({dict(aa[asset])})")
                elif asset not in aa:
                    lines.append(f"  {env}/{asset}: REMOVED (was {dict(ba[asset])})")
                elif d:
                    lines.append(f"  {env}/{asset}: " + ", ".join(f"{k}:{v:+d}" for k, v in d.items()))
        if len(lines) == n0:
            lines.append("  (no per-asset changes)")
    return lines
