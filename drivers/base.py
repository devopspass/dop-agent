"""Driver ABC + dataclasses shared by the drivers catalog and integrations registry.

Layers (stable contract for watchers / unfurl / settings code):
  driver.yaml   -> `Manifest`  (GENERAL per-driver knowledge, loaded by drivers/manifest.py)
  config dict   -> `Integration` (one INSTANCE: base_url, auth, options, per-kind overrides)
  Driver subclass (drivers/<id>/__init__.py) -> network behaviour: fetch / poll / test_connection

Drivers are SYNCHRONOUS and blocking (HTTP / subprocess); async callers run them
via `asyncio.to_thread`. One client per Integration instance, created lazily by
`Driver.client` and shared by fetch and poll.

Poll contract (`Driver.poll`): pure w.r.t. persistence. It receives the watch's
previous `last_state` dict and returns a `PollResult`; the CALLER persists
`result.state` (when not None), injects `result.events` in order, then applies
`result.done` / `result.broken`. `last_state` layouts are identical to the ones
already stored in watchers.db by the legacy `_poll_*_watch` functions.
"""
from __future__ import annotations

import abc
import re
from dataclasses import dataclass, field
from typing import Any


class DriverError(RuntimeError):
    """Base error for drivers / registry."""


class ManifestError(DriverError):
    """driver.yaml is invalid."""


class IntegrationConfigError(DriverError):
    """An integration instance's config is invalid."""


class UnsupportedOperation(DriverError):
    """Kind has no fetch (e.g. teams chat) or the driver lacks the capability."""


class NotFound(DriverError):
    """The remote resource definitely does not exist (permanent, e.g. HTTP 404)."""


# ---- manifest (static, per driver) ---------------------------------------

@dataclass
class AuthField:
    name: str
    secret: bool = False
    required: bool = False


@dataclass
class AuthType:
    name: str
    title: str = ""
    fields: dict[str, AuthField] = field(default_factory=dict)


@dataclass
class OptionSpec:
    name: str
    type: str = "string"  # string | int | float | bool | list
    default: Any = None
    help: str = ""
    required: bool = False
    pattern: str = ""  # full-match regex a string value must satisfy (package drivers: keeps values safe for skills/argv)


@dataclass
class WatchSpec:
    type: str
    key_match: str
    poll_interval_s: float = 60
    events: list[str] = field(default_factory=list)
    emoji: str = ""
    slack_emoji: str = ""
    coalesced: bool = False
    extra: dict[str, Any] = field(default_factory=dict)  # driver-specific keys (e.g. bot_note_authors)


@dataclass
class WatcherSpec:
    """A named script-watch recipe of a package driver (icon + script), created via `meta.watcher=<id>`."""
    id: str
    script: str
    emoji: str
    slack_emoji: str = ""
    title: str = ""
    description: str = ""
    args: list[str] = field(default_factory=list)   # leading script args that define the recipe (mode first)


@dataclass
class KindSpec:
    name: str
    url_match: str | None = None
    url_template: str | None = None
    status_set: str | None = None
    card: dict[str, Any] = field(default_factory=dict)
    watch: WatchSpec | None = None


@dataclass
class Manifest:
    id: str
    title: str
    icon: str = ""
    implementation: str = ""
    auth_types: dict[str, AuthType] = field(default_factory=dict)
    settings: dict[str, dict[str, Any]] = field(default_factory=dict)
    options: dict[str, OptionSpec] = field(default_factory=dict)
    status_sets: dict[str, dict[str, str]] = field(default_factory=dict)
    kinds: dict[str, KindSpec] = field(default_factory=dict)
    source_dir: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    # package drivers (type: package): CLI + watcher scripts + skill template, no poll()
    type: str = "api"                       # api | package
    watchers: dict[str, WatcherSpec] = field(default_factory=dict)
    skill: dict[str, Any] = field(default_factory=dict)   # {template, name}
    cli: dict[str, Any] = field(default_factory=dict)     # {entry, global_args[], fetch[], test[]}
    env: dict[str, str] = field(default_factory=dict)     # env var -> `${{...}}` template (watch scripts, CLI)
    tenant: dict[str, Any] = field(default_factory=dict)  # {option} instance option holding the AD tenant id

    @property
    def is_package(self) -> bool:
        return self.type == "package"

    def icon_files(self) -> set[str]:
        """Driver-folder icon files this manifest may serve (declared ones + the icon.svg default)."""
        names = {"icon.svg"}
        for v in [self.icon, *((k.card or {}).get("icon") for k in self.kinds.values())]:
            if isinstance(v, str) and v.endswith(".svg"):
                names.add(v)
        return names

    def icon_url(self, icon: str | None = None) -> str:
        from .manifest import resolve_icon_url
        return resolve_icon_url(self.id, self.source_dir, self.icon if icon is None else icon)

    def to_dict(self) -> dict:
        """Secret-free catalog view (no instance values) for the settings UI."""
        return {
            "id": self.id, "title": self.title, "icon": self.icon, "icon_url": self.icon_url(),
            "auth_types": {
                n: {"title": a.title,
                    "fields": {f: {"secret": x.secret, "required": x.required} for f, x in a.fields.items()}}
                for n, a in self.auth_types.items()},
            "settings": self.settings,
            "type": self.type,
            "options": {n: {"type": o.type, "default": o.default, "help": o.help, "required": o.required}
                        for n, o in self.options.items()},
            "watchers": {n: {"emoji": w.emoji, "title": w.title, "script": w.script, "args": list(w.args)}
                         for n, w in self.watchers.items()},
            "kinds": {n: {"url": bool(k.url_match), "icon_url": self.icon_url((k.card or {}).get("icon") or self.icon), "card_fields": list((k.card or {}).get("fields", [])),
                          "watch_type": k.watch.type if k.watch else None,
                          "poll_interval_s": k.watch.poll_interval_s if k.watch else None,
                          "events": k.watch.events if k.watch else []}
                      for n, k in self.kinds.items()},
        }


# ---- instance (runtime) ----------------------------------------------------

@dataclass
class KindRuntime:
    name: str
    spec: KindSpec
    enabled: bool = True
    poll_interval_s: float | None = None
    url_re: re.Pattern | None = None
    key_re: re.Pattern | None = None

    @property
    def watch_type(self) -> str | None:
        return self.spec.watch.type if self.spec.watch else None


@dataclass
class Integration:
    name: str
    manifest: Manifest
    enabled: bool = True
    base_url: str = ""
    auth: dict[str, Any] = field(default_factory=dict)  # resolved secrets live ONLY here; never projected
    options: dict[str, Any] = field(default_factory=dict)
    kinds: dict[str, KindRuntime] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    _driver: "Driver | None" = field(default=None, repr=False)

    @property
    def driver_id(self) -> str:
        return self.manifest.id

    @property
    def valid(self) -> bool:
        return not self.errors

    @property
    def active(self) -> bool:
        return self.enabled and self.valid

    def driver(self) -> "Driver":
        if self._driver is None:
            from .manifest import load_driver_class
            self._driver = load_driver_class(self.manifest)(self)
        return self._driver

    def status_emoji(self, kind: str, status: str) -> str:
        k = self.kinds.get(kind)
        sset = (self.manifest.status_sets.get(k.spec.status_set) if k and k.spec.status_set else None) or {}
        return sset.get(status or "", "")

    def __repr__(self) -> str:  # never leak auth
        return f"Integration({self.name!r}, driver={self.driver_id!r}, enabled={self.enabled}, errors={self.errors})"


# ---- driver outputs --------------------------------------------------------

@dataclass
class Resource:
    """Normalized remote object (card / watch preview)."""
    integration: str
    driver: str
    kind: str
    ident: dict[str, str]
    title: str = ""
    url: str = ""
    status: str = ""
    status_emoji: str = ""
    fields: dict[str, Any] = field(default_factory=dict)
    legacy: dict[str, Any] = field(default_factory=dict)  # extra /api/unfurl payload keys (incl. `type`), merged by furl_api

    @property
    def project(self) -> str:
        return self.ident.get("project", "")

    @property
    def key(self) -> str:
        return self.ident.get("key", "")

    def to_dict(self) -> dict:
        return {"integration": self.integration, "driver": self.driver, "kind": self.kind,
                "ident": self.ident, "title": self.title, "url": self.url, "status": self.status,
                "status_emoji": self.status_emoji, "fields": self.fields,
                "project": self.project, "key": self.key}


@dataclass
class Event:
    kind: str                 # note | pipeline_terminal | terminal | comment | status_change | fields_changed | message | done
    text: str                 # ready-to-inject message (Slack-style :emoji: markup)
    data: dict[str, Any] = field(default_factory=dict)  # structured payload (e.g. project/pipeline for rich enrichment)


@dataclass
class PollResult:
    events: list[Event] = field(default_factory=list)
    state: dict | None = None    # new last_state to persist (None = leave unchanged)
    done: bool = False           # watch finished itself (caller sets status=done AFTER injecting events)
    broken: str | None = None    # permanent defect (bad id / 404): caller marks watch broken with this reason
    error: str | None = None     # transient failure, already logged; caller just retries next tick
    network_error: bool = False  # `error` was a connectivity failure (feeds the caller's outage tracker)


class Driver(abc.ABC):
    tracks_outage = False        # caller keeps a blind-window/outage notice for this driver's watches
    outage_label = ""            # e.g. "MR/pipeline/job" in the "polling normally" recovery notice
    poll_timeout_s: float | None = None  # per-poll wall-clock bound override (None = caller default)

    def __init__(self, integration: Integration):
        self.integration = integration
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = self.make_client()
        return self._client

    @abc.abstractmethod
    def make_client(self): ...

    @abc.abstractmethod
    def fetch(self, kind: str, ident: dict) -> Resource: ...

    @abc.abstractmethod
    def poll(self, kind: str, ident: dict, last_state: dict, meta: dict | None = None,
             created_at: str | None = None, label: str | None = None) -> PollResult: ...

    def test_connection(self) -> dict:
        return {"ok": False, "message": "test_connection not implemented"}

    def _resource(self, kind: str, ident: dict, **kw) -> Resource:
        return Resource(integration=self.integration.name, driver=self.integration.driver_id,
                        kind=kind, ident=dict(ident), **kw)
