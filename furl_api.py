"""Link-hover status cards (FURLS) on top of the integration registry.

    unfurl(url)          -> card payload for a URL of ANY configured instance, or {"type": None}
    watch_preview(watch) -> same payload for a watch's underlying resource, or {"type": None}
    ttl()                -> cache TTL seconds (config unfurl.cache_ttl_s)

Payload = Resource.to_dict() (integration, driver, kind, ident, title, url, status, status_emoji,
fields, project, key) + `type` (the driver's type name when it provides one, else
"<driver>_<kind>") + the driver's extra keys (`Resource.legacy`) for existing consumers. Failures
(unrecognised URL, 404, auth, network) never raise: {"type": None} = "no card".
"""
from __future__ import annotations

import logging

import config
from integrations.runtime import get_registry

log = logging.getLogger("web_server")

NO_CARD: dict = {"type": None}


def ttl() -> float:
    try:
        return float(config.value("unfurl.cache_ttl_s"))
    except Exception:
        return 60.0


def _payload(res) -> dict:
    d = res.to_dict()
    legacy = dict(res.legacy or {})
    d["type"] = legacy.get("type") or f"{res.driver}_{res.kind}"
    for k, v in legacy.items():
        d.setdefault(k, v)
    return d


def unfurl(url: str) -> dict:
    try:
        res = get_registry().fetch_url(url)
        return _payload(res) if res else dict(NO_CARD)
    except Exception:
        log.warning("[unfurl] failed to resolve %s", url, exc_info=True)
        return dict(NO_CARD)


def watch_preview(watch: dict) -> dict:
    type_, key = watch.get("type"), (watch.get("key") or "").strip()
    if not type_ or not key:
        return dict(NO_CARD)
    try:
        res = get_registry().fetch_watch(type_, key)
        return _payload(res) if res else dict(NO_CARD)
    except Exception:
        log.debug("[watches] preview lookup failed for watch #%s (%s/%s)", watch.get("id"), type_, key,
                  exc_info=True)
        return dict(NO_CARD)
