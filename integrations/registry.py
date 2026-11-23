"""Integrations registry: driver catalog + configured instances -> lookup/fetch/poll API.

    reg = build_registry(cfg["integrations"])          # plain dict, secrets already resolved
    reg.errors                                         # list[str] of invalid manifests/instances (never raises)

Resolution (only enabled+valid instances and enabled kinds participate; config order wins ties):
    resolve_url(url)  -> (Integration, kind, ident) | None   fragment/query are ignored
    resolve_watch(type, key, integration=None) -> (Integration, kind, ident) | None
        `integration` pins one instance (two instances of one driver accept the same key shape;
        without it the first matching instance in config order wins)
    watch_types() -> set[str]                          registry-provided watch types
    watch_type_meta(type) -> dict | None               {type, integration, driver, kind, interval_s, events,
                                                        emoji, slack_emoji, key_match, key_match_js, coalesced, has_link, extra}
    link_for(type, key, integration=None) -> str | None   browser URL via url_template ('@ref' keys -> None)

Data (BLOCKING; async callers use asyncio.to_thread; one client per instance, shared):
    fetch(integration, kind, ident) -> Resource        Resource.to_dict() is the normalized card/preview
    poll(integration, kind, ident, last_state, meta=None, created_at=None, label=None) -> PollResult
        returns PollResult (events + new state + done/broken/error), not a bare list[Event] — the
        caller needs the new last_state. See drivers/base.py for the persistence contract.
    fetch_url(url) / fetch_watch(type, key) / poll_watch(type, key, last_state, ...) are resolve+call shortcuts
        (return None when nothing resolves).

Package drivers (manifest `type: package`): package_instances(), watchers(), resolve_watcher(id, integration),
    script_env(integration) -> env for watcher scripts. resolve_url picks the instance by tenant id (no probing).

UI / settings:
    ui_projection() -> dict   secret-free, JSON-serialisable (never includes `auth`); regexes are JS-syntax
    catalog() -> list[dict]   driver manifests (secret flags only, no values)
    integrations_summary() -> list[dict]   instances: name, driver, enabled, valid, errors, base_url, kinds
    test_connection(name) -> {"ok","message"}
"""
from __future__ import annotations

import copy
import logging
import re
from typing import Any
from urllib.parse import unquote

from drivers.base import (Integration, IntegrationConfigError, KindRuntime, Manifest, PollResult, Resource,
                          UnsupportedOperation)
from drivers.manifest import (coerce_option, discover_manifests, substitute_base_url, template_fields, to_js_regex)

log = logging.getLogger(__name__)


_TENANT_RE = re.compile(r"(?i)(?:tenant_?id|tid)[\"']?\s*[=:]\s*[\"']?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


def url_tenant(url: str) -> str | None:
    """AD tenant GUID carried by a link (`tenantId=<guid>` or a `"Tid":"<guid>"` context blob), lowercased."""
    m = _TENANT_RE.search(unquote(unquote(url or "")))
    return m.group(1).lower() if m else None


def _clean_ident(m: re.Match) -> dict[str, str]:
    return {k: v for k, v in m.groupdict().items() if v is not None}


def _build_integration(name: str, cfg: Any, manifests: dict[str, Manifest]) -> Integration | None:
    if not isinstance(cfg, dict):
        raise IntegrationConfigError(f"integration {name!r}: must be a mapping")
    drv = cfg.get("driver")
    if drv not in manifests:
        raise IntegrationConfigError(f"integration {name!r}: unknown driver {drv!r} (known: {sorted(manifests)})")
    man = manifests[drv]
    it = Integration(name=name, manifest=man, enabled=bool(cfg.get("enabled", True)),
                     base_url=str(cfg.get("base_url") or "").rstrip("/"), auth=dict(cfg.get("auth") or {}))
    err = it.errors.append

    base_spec = man.settings.get("base_url")
    if base_spec is not None and base_spec.get("required") and not it.base_url:
        err("base_url is required")
    elif it.base_url and not re.match(r"^https?://[^\s/]+", it.base_url):
        err(f"base_url {it.base_url!r} must start with http(s)://")

    if man.auth_types:
        atype = it.auth.get("type")
        if atype not in man.auth_types:
            err(f"auth.type {atype!r} not in {sorted(man.auth_types)}")
        else:
            spec = man.auth_types[atype]
            for fname, f in spec.fields.items():
                if f.required and not it.auth.get(fname):
                    err(f"auth.{fname} is required for auth type {atype!r}")
            unknown = set(it.auth) - set(spec.fields) - {"type"}
            if unknown:
                err(f"unknown auth fields {sorted(unknown)} for auth type {atype!r}")

    raw_opts = cfg.get("options") or {}
    for oname in raw_opts:
        if oname not in man.options:
            err(f"unknown option {oname!r} (known: {sorted(man.options)})")
    for oname, spec in man.options.items():
        val = raw_opts.get(oname, spec.default)
        if val is None or (spec.required and val == ""):
            if spec.required:
                err(f"option {oname} is required")
            it.options[oname] = None
            continue
        try:
            it.options[oname] = coerce_option(spec, val)
        except ValueError as exc:
            err(f"option {oname}: {exc}")

    kcfg = cfg.get("kinds") or {}
    for kname in kcfg:
        if kname not in man.kinds:
            err(f"unknown kind {kname!r} (known: {sorted(man.kinds)})")
    for kname, spec in man.kinds.items():
        kc = kcfg.get(kname) or {}
        interval = (kc.get("watch") or {}).get("poll_interval_s")
        if interval is not None and (isinstance(interval, bool) or not isinstance(interval, (int, float)) or interval <= 0):
            err(f"kinds.{kname}.watch.poll_interval_s must be a positive number")
            interval = None
        kr = KindRuntime(kname, spec, enabled=bool(kc.get("enabled", True)), poll_interval_s=interval)
        if spec.url_match:
            kr.url_re = re.compile(substitute_base_url(spec.url_match, it.base_url))
        if spec.watch:
            kr.key_re = re.compile(spec.watch.key_match)
        it.kinds[kname] = kr
    return it


class Registry:
    def __init__(self, manifests: dict[str, Manifest], integrations: dict[str, Integration], errors: list[str]):
        self.manifests = manifests
        self.integrations = integrations
        self.errors = errors

    def _active(self):
        return [i for i in self.integrations.values() if i.active]

    def get(self, name: str) -> Integration | None:
        return self.integrations.get(name)

    # ---- resolution ----
    def resolve_url(self, url: str):
        """First matching (Integration, kind, ident). `api` drivers: config order wins. `package` drivers
        (several instances can share one URL shape, e.g. two Teams tenants): the instance whose tenant option
        equals the tenant id found in the URL; else the only active instance of the driver; else None
        (never probes instances - a wrong guess could hit the wrong tenant)."""
        raw = (url or "").strip().split("#", 1)[0]
        clean = raw.split("?", 1)[0]
        matches = []
        for it in self._active():
            for kr in it.kinds.values():
                if kr.enabled and kr.url_re and (m := kr.url_re.match(clean)):
                    matches.append((it, kr.name, _clean_ident(m)))
        if not matches:
            return None
        first = matches[0]
        if not first[0].manifest.is_package:
            return first
        same = [x for x in matches if x[0].driver_id == first[0].driver_id and x[1] == first[1]]
        tid = url_tenant(raw)
        if len(same) == 1:
            opt = same[0][0].manifest.tenant.get("option")
            own = str(same[0][0].options.get(opt) or "").lower() if opt else ""
            return None if tid and own and tid != own else same[0]
        if tid:
            opt = first[0].manifest.tenant.get("option")
            hit = [x for x in same if opt and str(x[0].options.get(opt) or "").lower() == tid]
            if len(hit) == 1:
                return hit[0]
        return None

    def _watch_candidates(self, type_: str, integration: str | None):
        for it in self._active():
            if integration and it.name != integration:
                continue
            for kr in it.kinds.values():
                if kr.enabled and kr.spec.watch and kr.spec.watch.type == type_:
                    yield it, kr

    def resolve_watch(self, type_: str, key: str, integration: str | None = None):
        key = (key or "").strip()
        for it, kr in self._watch_candidates(type_, integration):
            if m := kr.key_re.match(key):
                return it, kr.name, _clean_ident(m)
        return None

    def watch_types(self) -> set[str]:
        return {kr.spec.watch.type for it in self._active() for kr in it.kinds.values()
                if kr.enabled and kr.spec.watch}

    def watch_type_meta(self, type_: str, integration: str | None = None) -> dict | None:
        for it, kr in self._watch_candidates(type_, integration):
            w = kr.spec.watch
            return {"type": w.type, "integration": it.name, "driver": it.driver_id, "kind": kr.name,
                    "interval_s": kr.poll_interval_s if kr.poll_interval_s is not None else w.poll_interval_s,
                    "events": list(w.events), "emoji": w.emoji, "slack_emoji": w.slack_emoji,
                    "key_match": w.key_match, "key_match_js": to_js_regex(w.key_match),
                    "coalesced": w.coalesced, "has_link": bool(kr.spec.url_template),
                    "extra": dict(w.extra)}
        return None

    def link_for(self, type_: str, key: str, integration: str | None = None) -> str | None:
        r = self.resolve_watch(type_, key, integration)
        if not r:
            return None
        it, kind, ident = r
        tpl = it.kinds[kind].spec.url_template
        if not tpl:
            return None
        vals = {**ident, "base_url": it.base_url}
        if template_fields(tpl) - set(vals):
            return None
        return tpl.format(**vals)

    # ---- package drivers ----
    def package_instances(self) -> list[Integration]:
        return [i for i in self._active() if i.manifest.is_package]

    def watchers(self) -> list[dict]:
        """Watcher recipes of every active package instance (for the UI legend/list and watch_script validation)."""
        return [{"id": w.id, "script": w.script, "emoji": w.emoji, "slack_emoji": w.slack_emoji, "title": w.title,
                 "description": w.description, "integration": it.name, "driver": it.driver_id}
                for it in self.package_instances() for w in it.manifest.watchers.values()]

    def resolve_watcher(self, watcher: str, integration: str | None = None) -> Integration:
        """Instance that owns recipe `watcher`; raises UnsupportedOperation with a fix hint."""
        cands = [i for i in self.package_instances() if watcher in i.manifest.watchers
                 and (not integration or i.name == integration)]
        if not cands:
            known = sorted({f"{w['id']}@{w['integration']}" for w in self.watchers()})
            raise UnsupportedOperation(
                f"no enabled integration provides watcher {watcher!r}"
                + (f" (integration {integration!r})" if integration else "") + f". Known: {known or 'none'}. "
                "Enable the integration in Settings -> Integrations.")
        if len(cands) > 1:
            raise UnsupportedOperation(f"watcher {watcher!r} exists in several integrations "
                                       f"{sorted(i.name for i in cands)}: pass meta.integration to pick one.")
        return cands[0]

    def script_env(self, integration: str | None) -> dict[str, str]:
        """Env for a watcher script of package instance `integration` (manifest `env:` templates)."""
        it = self.integrations.get(integration or "")
        if it is None or not it.active or not it.manifest.is_package:
            return {}
        from drivers.package import template_context
        from drivers.templating import render
        ctx = template_context(it)
        return {k: render(v, ctx) for k, v in it.manifest.env.items()}

    # ---- data ----
    def fetch(self, integration: Integration | str, kind: str, ident: dict) -> Resource:
        it = self._it(integration)
        return it.driver().fetch(kind, ident)

    def poll(self, integration: Integration | str, kind: str, ident: dict, last_state: dict | None,
             meta: dict | None = None, created_at: str | None = None, label: str | None = None) -> PollResult:
        it = self._it(integration)
        return it.driver().poll(kind, ident, last_state or {}, meta, created_at, label)

    def fetch_url(self, url: str) -> Resource | None:
        r = self.resolve_url(url)
        return self.fetch(*r) if r else None

    def fetch_watch(self, type_: str, key: str, integration: str | None = None) -> Resource | None:
        r = self.resolve_watch(type_, key, integration)
        return self.fetch(*r) if r else None

    def poll_watch(self, type_: str, key: str, last_state: dict | None, meta: dict | None = None,
                   created_at: str | None = None, label: str | None = None,
                   integration: str | None = None) -> PollResult | None:
        r = self.resolve_watch(type_, key, integration)
        return self.poll(*r, last_state, meta, created_at, label) if r else None

    def _it(self, integration) -> Integration:
        it = integration if isinstance(integration, Integration) else self.integrations.get(integration)
        if it is None:
            raise UnsupportedOperation(f"unknown integration {integration!r}")
        if not it.active:
            raise UnsupportedOperation(f"integration {it.name!r} is disabled or invalid: {it.errors}")
        return it

    def test_connection(self, name: str) -> dict:
        it = self.integrations.get(name)
        if it is None:
            return {"ok": False, "message": f"unknown integration {name!r}"}
        if not it.valid:
            return {"ok": False, "message": "; ".join(it.errors)}
        try:
            return it.driver().test_connection()
        except Exception as exc:
            return {"ok": False, "message": str(exc)[:300]}

    # ---- UI ----
    def catalog(self) -> list[dict]:
        return [m.to_dict() for m in self.manifests.values()]

    def integrations_summary(self) -> list[dict]:
        out = []
        for i in self.integrations.values():
            at = i.manifest.auth_types.get(i.auth.get("type"))
            secrets = [f for f, spec in (at.fields.items() if at else []) if spec.secret and i.auth.get(f)]
            out.append({"name": i.name, "driver": i.driver_id, "title": i.manifest.title, "enabled": i.enabled,
                        "valid": i.valid, "errors": list(i.errors), "base_url": i.base_url,
                        "auth_type": i.auth.get("type"), "configured_secrets": secrets,
                        "kinds": {k: {"enabled": kr.enabled, "poll_interval_s": kr.poll_interval_s}
                                  for k, kr in i.kinds.items()}})
        return out

    def ui_projection(self) -> dict:
        """Everything the browser needs to furl links / label watches. Contains no auth data."""
        integrations, watch_types = [], {}
        for it in self._active():
            kinds = {}
            for kr in it.kinds.values():
                if not kr.enabled:
                    continue
                s = kr.spec
                kinds[kr.name] = {
                    "url_match": to_js_regex(substitute_base_url(s.url_match, it.base_url)) if s.url_match else None,
                    "url_template": s.url_template.replace("{base_url}", it.base_url) if s.url_template else None,
                    "card_fields": list((s.card or {}).get("fields", [])),
                    "card_label": (s.card or {}).get("label"),
                    "icon": (s.card or {}).get("icon") or it.manifest.icon,
                    "icon_url": it.manifest.icon_url((s.card or {}).get("icon") or it.manifest.icon),
                    "status_emoji": dict(it.manifest.status_sets.get(s.status_set, {})) if s.status_set else {},
                    "watch_type": s.watch.type if s.watch else None,
                }
                if s.watch:
                    meta = self.watch_type_meta(s.watch.type, it.name)
                    watch_types.setdefault(s.watch.type, {**meta, "integrations": []})["integrations"].append(it.name)
            integrations.append({"name": it.name, "driver": it.driver_id, "title": it.manifest.title,
                                 "icon": it.manifest.icon, "icon_url": it.manifest.icon_url(),
                                 "base_url": it.base_url, "kinds": kinds})
        return {"version": 1, "integrations": integrations, "watch_types": watch_types,
                "watchers": {f"{w['id']}@{w['integration']}": {"id": w["id"], "emoji": w["emoji"], "title": w["title"],
                                                               "integration": w["integration"]}
                             for w in self.watchers()}}


def default_integrations() -> dict:
    """Zero-config registry input: empty. No host, tenant or credential is built in; every instance
    (all drivers) comes from `integrations:` in config.yaml."""
    return {}


def build_registry(integrations_cfg: dict | None, extra_driver_dirs=None) -> Registry:
    errors: list[str] = []
    manifests = discover_manifests(extra_driver_dirs, errors) if extra_driver_dirs is not None \
        else discover_manifests(errors=errors)
    integrations: dict[str, Integration] = {}
    for name, cfg in (copy.copy(integrations_cfg) or {}).items():
        try:
            it = _build_integration(name, cfg, manifests)
        except IntegrationConfigError as exc:
            errors.append(str(exc))
            continue
        if it.manifest.is_package and it.manifest.skill:
            from drivers.package import skill_suffix
            suffix = skill_suffix(name)
            twin = next((o.name for o in integrations.values() if o.manifest.is_package and o.manifest.skill
                         and o.manifest.id == it.manifest.id and skill_suffix(o.name) == suffix), None)
            if twin:
                it.errors.append(f"name collides with integration {twin!r} (both map to skill suffix {suffix!r}); rename one")
        for e in it.errors:
            errors.append(f"integration {name!r}: {e}")
        integrations[name] = it
    for e in errors:
        log.warning("[registry] %s", e)
    return Registry(manifests, integrations, errors)
