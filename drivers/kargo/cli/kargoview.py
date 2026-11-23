"""Compact, defensive summaries of Kargo objects (`kargo get ... -o json`), shared by the CLI and the watcher.

Kargo's status schema moves between releases, so every getter tolerates missing keys and returns '' / [] / {}.
"""
from __future__ import annotations

TERMINAL_PROMOTION = {"Succeeded", "Failed", "Errored", "Aborted"}
BAD_VERIFICATION = {"Failed", "Error", "Inconclusive", "Aborted"}
PROMO_EMOJI = {"Succeeded": "✅", "Failed": "❌", "Errored": "❌", "Aborted": "🚫", "Running": "🔄",
               "Pending": "⏳"}
HEALTH_EMOJI = {"Healthy": "💚", "Unhealthy": "❤️‍🔥", "Progressing": "🔄", "Unknown": "❔", "": "❔"}


def _d(x):
    return x if isinstance(x, dict) else {}


def _l(x):
    return x if isinstance(x, list) else []


def meta(o: dict) -> dict:
    return _d(o.get("metadata"))


def name(o: dict) -> str:
    return str(meta(o).get("name") or "")


def created(o: dict) -> str:
    return str(meta(o).get("creationTimestamp") or "")


def freight_alias(f: dict) -> str:
    return str(f.get("alias") or _d(meta(f).get("labels")).get("kargo.akuity.io/alias") or "")


def freight_warehouse(f: dict) -> str:
    return str(_d(f.get("origin")).get("name") or "")


def artifacts(f: dict) -> list[str]:
    out = []
    for i in _l(f.get("images")):
        out.append(f"image {i.get('repoURL', '')}:{i.get('tag', '')}")
    for c in _l(f.get("commits")):
        ref = c.get("tag") or c.get("branch") or ""
        out.append(f"commit {c.get('repoURL', '')}@{str(c.get('id', ''))[:8]}" + (f" ({ref})" if ref else ""))
    for h in _l(f.get("charts")):
        out.append(f"chart {h.get('repoURL', '')}/{h.get('name', '')}:{h.get('version', '')}")
    return out


def freight_summary(f: dict) -> dict:
    st = _d(f.get("status"))
    return {"name": name(f), "alias": freight_alias(f), "warehouse": freight_warehouse(f), "created": created(f),
            "artifacts": artifacts(f), "verified_in": sorted(_d(st.get("verifiedIn"))),
            "approved_for": sorted(_d(st.get("approvedFor")))}


def _current_freight(st: dict) -> list[str]:
    hist = _l(st.get("freightHistory"))
    out = []
    if hist:
        for ref in _d(_d(hist[0]).get("items")).values():
            ref = _d(ref)
            out.append(freight_alias(ref) or str(ref.get("name") or "")[:12])
    return out


def last_verification(st: dict) -> dict:
    hist = _l(st.get("freightHistory"))
    vh = _l(_d(hist[0]).get("verificationHistory")) if hist else []
    return _d(vh[0]) if vh else {}


def verification_summary(v: dict) -> dict:
    if not v:
        return {}
    ar = _d(v.get("analysisRun"))
    return {"id": v.get("id", ""), "phase": v.get("phase", ""), "message": str(v.get("message") or "")[:300],
            "started": v.get("startTime", ""), "finished": v.get("finishTime", ""),
            "analysis_run": ar.get("name", ""), "analysis_phase": ar.get("phase", "")}


def stage_summary(s: dict) -> dict:
    st = _d(s.get("status"))
    health = _d(st.get("health"))
    lp = _d(st.get("lastPromotion"))
    return {"name": name(s), "phase": st.get("phase", ""), "health": health.get("status", ""),
            "freight": _current_freight(st) or ([st["freightSummary"]] if st.get("freightSummary") else []),
            "last_promotion": {"name": lp.get("name", ""), "phase": _d(lp.get("status")).get("phase", ""),
                               "finished": lp.get("finishedAt", "")} if lp else {},
            "verification": verification_summary(last_verification(st)),
            "requested": [_d(r.get("origin")).get("name", "") for r in _l(_d(s.get("spec")).get("requestedFreight"))]}


def promotion_summary(p: dict) -> dict:
    st = _d(p.get("status"))
    spec = _d(p.get("spec"))
    return {"name": name(p), "stage": spec.get("stage", ""), "phase": st.get("phase", ""),
            "freight": spec.get("freight", ""),
            "message": str(st.get("message") or "")[:300], "created": created(p),
            "finished": st.get("finishedAt", "")}


def promotion_steps(p: dict) -> list[dict]:
    return [{"alias": m.get("alias", ""), "status": m.get("status", ""), "message": str(m.get("message") or "")[:200],
             "started": m.get("startedAt", ""), "finished": m.get("finishedAt", "")}
            for m in map(_d, _l(_d(p.get("status")).get("stepExecutionMetadata")))]


def newest_first(items: list[dict]) -> list[dict]:
    return sorted(items, key=created, reverse=True)
