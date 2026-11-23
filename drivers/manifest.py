"""Load + validate driver manifests (drivers/<id>/driver.yaml) and resolve driver classes.

Discovery: built-in `dop-agent/drivers/*/driver.yaml`, then the optional plugin dir
`~/.dop-agent/drivers/*/driver.yaml` (plugin id must not collide with a built-in).
`implementation: "module:Class"` is imported normally for built-ins; for plugins the
module is loaded from `<plugin dir>/__init__.py` or `driver.py`.

Regex helpers:
  substitute_base_url(pattern, base_url) -> str   `{base_url}` -> escaped base url
  to_js_regex(pattern) -> str                     Python `(?P<n>` -> JS `(?<n>`; rejects unsupported syntax

Icons (`icon:` at driver level, `card.icon` per kind) take one of three forms:
  name           bare name        -> webui/icons/<name>.svg
  file.svg       driver-folder file (`drivers/<id>/file.svg`, also plugins) -> GET /api/drivers/<id>/icon/<file>;
                 an unset driver icon falls back to `icon.svg` in the driver folder when present
  https://...    or data:image/(png|svg+xml|jpeg|webp);base64,...  -> used as-is by the browser
                 (https icons make the browser contact that host: a privacy trade-off; the web UI sets no CSP)
Logos are vendored (never hot-linked) as small script-free SVGs, used only to identify the integrated service
(vendor marks stay property of their owners); a missing image falls back to the default icon in the UI.
Anything else (javascript:, http:, file:, ...) is rejected. `resolve_icon_url()` yields the ready-to-use URL.
"""
from __future__ import annotations

import importlib
import importlib.util
import re
import sys
from pathlib import Path
from typing import Any

import yaml

from . import templating
from .base import (AuthField, AuthType, KindSpec, Manifest, ManifestError, OptionSpec, WatcherSpec, WatchSpec)

BUILTIN_DIR = Path(__file__).resolve().parent
PLUGIN_DIR = Path.home() / ".dop-agent" / "drivers"
OPTION_TYPES = {"string", "int", "float", "bool", "list"}
_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_NAMED_GROUP_RE = re.compile(r"\(\?P<([A-Za-z_][A-Za-z0-9_]*)>")
_ICON_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_ICON_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*\.svg$")
_ICON_HTTPS_RE = re.compile(r"^https://[^\s'\"<>]+$")
_ICON_DATA_RE = re.compile(r"^data:image/(png|svg\+xml|jpeg|webp);base64,[A-Za-z0-9+/=]+$")
ICON_MAX_BYTES = 64 * 1024
DEFAULT_ICON_FILE = "icon.svg"
DRIVER_TYPES = {"api", "package"}
PACKAGE_IMPLEMENTATION = "drivers.package:PackageDriver"
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_WATCH_KNOWN = {"type", "key_match", "poll_interval_s", "events", "emoji", "slack_emoji", "coalesced"}


def _escape_base(base_url: str) -> str:
    return re.sub(r"([.^$*+?()\[\]{}|\\/])", r"\\\1", base_url.rstrip("/"))


def substitute_base_url(pattern: str, base_url: str) -> str:
    return pattern.replace("{base_url}", _escape_base(base_url))


def template_fields(template: str) -> set[str]:
    return set(re.findall(r"{(\w+)}", template))


def to_js_regex(pattern: str) -> str:
    """Convert a Python regex (as used in manifests) into a JS-compatible source string."""
    out = _NAMED_GROUP_RE.sub(lambda m: f"(?<{m.group(1)}>", pattern)
    out = re.sub(r"\(\?P=([A-Za-z_]\w*)\)", r"\\k<\1>", out)
    if re.search(r"\(\?[aiLmsux-]+[:)]", out) or "\\Z" in out or "\\A" in out:
        raise ManifestError(f"regex {pattern!r} uses syntax not convertible to JS (inline flags/\\A/\\Z)")
    return out


def validate_icon(where: str, value: Any, source_dir: str = "") -> str:
    """Return the icon value if it is one of the three allowed forms, else raise ManifestError."""
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise _err(where, f"icon must be a string, got {type(value).__name__}")
    if _ICON_NAME_RE.match(value) or _ICON_HTTPS_RE.match(value):
        return value
    if _ICON_DATA_RE.match(value):
        if len(value) > ICON_MAX_BYTES:
            raise _err(where, f"data: icon exceeds {ICON_MAX_BYTES} bytes")
        return value
    if _ICON_FILE_RE.match(value):
        if source_dir:
            f = Path(source_dir) / value
            if not f.is_file():
                raise _err(where, f"icon file {value!r} not found in driver folder")
            if f.stat().st_size > ICON_MAX_BYTES:
                raise _err(where, f"icon file {value!r} exceeds {ICON_MAX_BYTES} bytes")
        return value
    raise _err(where, "icon must be a bare name, a driver-folder *.svg file, an https:// URL or a "
                      "data:image/(png|svg+xml|jpeg|webp);base64 URL")


def resolve_icon_url(driver_id: str, source_dir: str, icon: str) -> str:
    """Ready-to-use browser URL for an (already validated) icon value; '' when there is none."""
    if not icon:
        if source_dir and (Path(source_dir) / DEFAULT_ICON_FILE).is_file():
            return f"/api/drivers/{driver_id}/icon"
        return ""
    if icon.startswith(("https://", "data:")):
        return icon
    if icon.endswith(".svg"):
        return f"/api/drivers/{driver_id}/icon/{icon}"
    return f"/static/icons/{icon}.svg"


def _err(where: str, msg: str) -> ManifestError:
    return ManifestError(f"{where}: {msg}")


def _compile(where: str, pattern: str, base_url: str = "https://example.invalid") -> re.Pattern:
    try:
        return re.compile(substitute_base_url(pattern, base_url))
    except re.error as exc:
        raise _err(where, f"invalid regex {pattern!r}: {exc}") from exc


def _req_dict(where: str, v: Any, allow_none=False) -> dict:
    if v is None and allow_none:
        return {}
    if not isinstance(v, dict):
        raise _err(where, f"must be a mapping, got {type(v).__name__}")
    return v


def parse_manifest(data: Any, source: str = "<manifest>", source_dir: str = "") -> Manifest:
    where = source
    d = _req_dict(where, data)
    mid = d.get("id")
    if not isinstance(mid, str) or not _ID_RE.match(mid):
        raise _err(where, f"'id' must match {_ID_RE.pattern}, got {mid!r}")
    where = f"{source} [{mid}]"
    if not d.get("title"):
        d = {**d, "title": mid}
    dtype = d.get("type", "api")
    if dtype not in DRIVER_TYPES:
        raise _err(where, f"'type' must be one of {sorted(DRIVER_TYPES)}, got {dtype!r}")
    impl = d.get("implementation") or (PACKAGE_IMPLEMENTATION if dtype == "package" else None)
    if not isinstance(impl, str) or impl.count(":") != 1:
        raise _err(where, "'implementation' must be 'module:Class'")

    auth_types: dict[str, AuthType] = {}
    for tname, tspec in _req_dict(where + " auth.types", (d.get("auth") or {}).get("types"), True).items():
        tspec = _req_dict(f"{where} auth.types.{tname}", tspec, True)
        fields = {}
        for fname, fspec in _req_dict(f"{where} auth.types.{tname}.fields", tspec.get("fields"), True).items():
            fspec = _req_dict(f"{where} auth.types.{tname}.fields.{fname}", fspec, True)
            fields[fname] = AuthField(fname, bool(fspec.get("secret", False)), bool(fspec.get("required", False)))
        auth_types[tname] = AuthType(tname, tspec.get("title", tname), fields)

    options: dict[str, OptionSpec] = {}
    for oname, ospec in _req_dict(where + " options", d.get("options"), True).items():
        ospec = _req_dict(f"{where} options.{oname}", ospec, True)
        otype = ospec.get("type", "string")
        if otype not in OPTION_TYPES:
            raise _err(f"{where} options.{oname}", f"type {otype!r} not in {sorted(OPTION_TYPES)}")
        opt = OptionSpec(oname, otype, ospec.get("default"), ospec.get("help", ""), bool(ospec.get("required", False)),
                         str(ospec.get("pattern") or ""))
        if opt.pattern:
            try:
                re.compile(opt.pattern)
            except re.error as exc:
                raise _err(f"{where} options.{oname}", f"invalid pattern: {exc}") from None
        if opt.default is not None:
            try:
                coerce_option(opt, opt.default)
            except ValueError as exc:
                raise _err(f"{where} options.{oname}", f"default: {exc}") from exc
        options[oname] = opt

    status_sets: dict[str, dict[str, str]] = {}
    for sname, sset in _req_dict(where + " status_sets", d.get("status_sets"), True).items():
        status_sets[sname] = {str(k): str(v) for k, v in _req_dict(f"{where} status_sets.{sname}", sset, True).items()}

    settings = _req_dict(where + " settings", d.get("settings"), True)
    has_base = "base_url" in settings

    kinds: dict[str, KindSpec] = {}
    watch_types: dict[str, str] = {}
    for kname, kspec in _req_dict(where + " kinds", d.get("kinds"), True).items():
        kw = f"{where} kinds.{kname}"
        kspec = _req_dict(kw, kspec, True)
        url_match = kspec.get("url_match")
        url_template = kspec.get("url_template")
        if url_match is not None:
            if "{base_url}" in url_match and not has_base:
                raise _err(kw, "url_match uses {base_url} but driver has no settings.base_url")
            groups = set(_NAMED_GROUP_RE.findall(url_match))
            if not groups:
                raise _err(kw, "url_match needs at least one named group (?P<name>...)")
            _compile(f"{kw}.url_match", url_match)
            to_js_regex(url_match)
            if url_template:
                unknown = template_fields(url_template) - groups - {"base_url"}
                if unknown:
                    raise _err(kw, f"url_template uses {sorted(unknown)} not captured by url_match")
        elif url_template:
            raise _err(kw, "url_template without url_match")
        sset = kspec.get("status_set")
        if sset is not None and sset not in status_sets:
            raise _err(kw, f"status_set {sset!r} is not defined in status_sets")
        card = _req_dict(f"{kw}.card", kspec.get("card"), True)
        validate_icon(f"{kw}.card.icon", card.get("icon"), source_dir)
        if "fields" in card and not isinstance(card["fields"], list):
            raise _err(kw, "card.fields must be a list")
        watch = None
        if kspec.get("watch") is not None:
            w = _req_dict(f"{kw}.watch", kspec["watch"])
            wtype, km = w.get("type"), w.get("key_match")
            if not isinstance(wtype, str) or not wtype:
                raise _err(f"{kw}.watch", "'type' is required")
            if wtype in watch_types:
                raise _err(f"{kw}.watch", f"watch type {wtype!r} already used by kind {watch_types[wtype]!r}")
            watch_types[wtype] = kname
            if not isinstance(km, str):
                raise _err(f"{kw}.watch", "'key_match' is required")
            _compile(f"{kw}.watch.key_match", km)
            to_js_regex(km)
            interval = w.get("poll_interval_s", 60)
            if isinstance(interval, bool) or not isinstance(interval, (int, float)) or interval <= 0:
                raise _err(f"{kw}.watch", f"poll_interval_s must be a positive number, got {interval!r}")
            events = w.get("events", [])
            if not isinstance(events, list):
                raise _err(f"{kw}.watch", "events must be a list")
            watch = WatchSpec(wtype, km, interval, [str(e) for e in events], str(w.get("emoji", "")),
                              str(w.get("slack_emoji", "")), bool(w.get("coalesced", False)),
                              {k: v for k, v in w.items() if k not in _WATCH_KNOWN})
        if not url_match and not watch:
            raise _err(kw, "kind needs url_match and/or watch")
        kinds[kname] = KindSpec(kname, url_match, url_template, sset, card, watch)
    if not kinds and dtype != "package":
        raise _err(where, "at least one kind is required")

    package = _parse_package(where, d, dtype, options, source_dir)
    return Manifest(id=mid, title=str(d["title"]), icon=validate_icon(where + " icon", d.get("icon"), source_dir), implementation=impl,
                    auth_types=auth_types, settings=settings, options=options, status_sets=status_sets,
                    kinds=kinds, source_dir=source_dir, raw=d, type=dtype, **package)


def _parse_package(where: str, d: dict, dtype: str, options: dict[str, OptionSpec], source_dir: str) -> dict:
    """Package-only sections: watchers, skill, cli, env, tenant. All empty for `api` drivers."""
    out: dict[str, Any] = {"watchers": {}, "skill": {}, "cli": {}, "env": {}, "tenant": {}}
    if dtype != "package":
        for k in out:
            if d.get(k):
                raise _err(where, f"'{k}' is only valid for type: package")
        return out
    base = Path(source_dir) if source_dir else None

    def _file(w: str, rel: Any) -> str:
        if not isinstance(rel, str) or not rel or rel.startswith("/") or ".." in Path(rel).parts:
            raise _err(w, f"must be a relative path inside the driver folder, got {rel!r}")
        if base is not None:
            target = (base / rel).resolve()
            if not target.is_file() or not target.is_relative_to(base.resolve()):
                raise _err(w, f"file {rel!r} not found in driver folder (symlinks out of it are not allowed)")
        return rel

    for wid, wspec in _req_dict(where + " watchers", d.get("watchers"), True).items():
        ww = f"{where} watchers.{wid}"
        if not isinstance(wid, str) or not _ICON_NAME_RE.match(wid):
            raise _err(ww, "watcher id must match [a-z][a-z0-9_-]*")
        wspec = _req_dict(ww, wspec)
        script = wspec.get("script")
        if not isinstance(script, str) or not script or "/" in script:
            raise _err(ww, "'script' (basename in the driver's watch/ folder) is required")
        if base is not None:
            target = (base / "watch" / script).resolve()
            if not target.is_file() or not target.is_relative_to((base / "watch").resolve()):
                raise _err(ww, f"script {script!r} not found in watch/")
        emoji = wspec.get("emoji")
        if not isinstance(emoji, str) or not emoji.strip():
            raise _err(ww, "'emoji' is required (shown in the watchers list)")
        wargs = wspec.get("args", [])
        if not isinstance(wargs, list) or not all(isinstance(a, str) for a in wargs):
            raise _err(ww, "'args' must be a list of strings")
        out["watchers"][wid] = WatcherSpec(wid, script, emoji, str(wspec.get("slack_emoji", "")),
                                           str(wspec.get("title", wid)), str(wspec.get("description", "")),
                                           list(wargs))

    skill = _req_dict(where + " skill", d.get("skill"), True)
    if skill:
        tpl = _file(f"{where} skill.template", skill.get("template"))
        name = skill.get("name")
        if not isinstance(name, str) or not _ICON_NAME_RE.match(name):
            raise _err(f"{where} skill", "'name' must match [a-z][a-z0-9_-]*")
        out["skill"] = {"template": tpl, "name": name}

    cli = _req_dict(where + " cli", d.get("cli"), True)
    if cli:
        entry = _file(f"{where} cli.entry", cli.get("entry"))
        for k in ("global_args", "fetch", "test"):
            v = cli.get(k, [])
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                raise _err(f"{where} cli.{k}", "must be a list of strings")
        out["cli"] = {"entry": entry, "global_args": list(cli.get("global_args", [])),
                      "fetch": list(cli.get("fetch", ["fetch"])), "test": list(cli.get("test", ["test"]))}

    env = _req_dict(where + " env", d.get("env"), True)
    for name, tpl in env.items():
        if not _ENV_NAME_RE.match(str(name)) or not isinstance(tpl, str):
            raise _err(f"{where} env.{name}", "env var name must be UPPER_SNAKE and value a template string")
    out["env"] = {str(k): v for k, v in env.items()}

    tenant = _req_dict(where + " tenant", d.get("tenant"), True)
    if tenant:
        if tenant.get("option") not in options:
            raise _err(f"{where} tenant", f"option {tenant.get('option')!r} is not declared in options")
        out["tenant"] = {"option": tenant["option"]}

    known = {f"config.{o}" for o in options} | {"config.base_url", "instance.name", "driver.id", "driver.dir", "driver.cli_dir",
                                                  "driver.cli", "driver.python", "driver.watch_dir", "skill.name"}
    texts = [*(cli.get("global_args", [])), *out["env"].values()]
    if out["skill"] and base is not None:
        texts.append((base / out["skill"]["template"]).read_text())
    for t in texts:
        unknown = {f for f in templating.fields(t) if f not in known}
        if unknown:
            raise _err(where, f"templates reference unknown values {sorted(unknown)} (known: {sorted(known)})")
    return out


def load_manifest(path: Path) -> Manifest:
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ManifestError(f"{path}: invalid YAML: {exc}") from exc
    m = parse_manifest(data, str(path), str(path.parent))
    return m


def discover_manifests(extra_dirs: list[Path] | None = None, errors: list[str] | None = None) -> dict[str, Manifest]:
    """id -> Manifest. Invalid manifests are skipped and reported in `errors` (never raise)."""
    found: dict[str, Manifest] = {}
    dirs = [BUILTIN_DIR, *(extra_dirs if extra_dirs is not None else [PLUGIN_DIR])]
    for base in dirs:
        if not Path(base).is_dir():
            continue
        for f in sorted(Path(base).glob("*/driver.yaml")):
            try:
                m = load_manifest(f)
            except ManifestError as exc:
                if errors is not None:
                    errors.append(str(exc))
                continue
            if m.id in found:
                if errors is not None:
                    errors.append(f"{f}: duplicate driver id {m.id!r} (already loaded from {found[m.id].source_dir})")
                continue
            found[m.id] = m
    return found


def load_driver_class(manifest: Manifest):
    modname, clsname = manifest.implementation.split(":")
    mod = None
    try:
        if Path(manifest.source_dir).resolve().parent == BUILTIN_DIR:
            mod = importlib.import_module(modname)
        else:
            raise ImportError
    except ImportError:
        d = Path(manifest.source_dir)
        for cand in ("driver.py", "__init__.py"):
            if (d / cand).exists():
                name = f"dop_driver_plugin_{manifest.id}"
                spec = importlib.util.spec_from_file_location(
                    name, d / cand, submodule_search_locations=[str(d)] if cand == "__init__.py" else None)
                mod = importlib.util.module_from_spec(spec)
                sys.modules[name] = mod
                spec.loader.exec_module(mod)
                break
        if mod is None:
            raise ManifestError(f"{manifest.id}: cannot import implementation {manifest.implementation!r}")
    try:
        return getattr(mod, clsname)
    except AttributeError as exc:
        raise ManifestError(f"{manifest.id}: {modname} has no class {clsname!r}") from exc


def coerce_option(spec: OptionSpec, value: Any) -> Any:
    t = spec.type
    try:
        if t == "string":
            if not isinstance(value, (str, int, float)) or isinstance(value, bool):
                raise ValueError
            text = str(value)
            if any(c in text for c in "\n\r\0"):
                raise ValueError("must be a single line")
            if spec.pattern and not re.fullmatch(spec.pattern, text):
                raise ValueError(f"does not match the allowed format {spec.pattern}")
            return text
        if t == "int":
            if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
                raise ValueError
            return int(value)
        if t == "float":
            if isinstance(value, bool):
                raise ValueError
            return float(value)
        if t == "bool":
            if isinstance(value, bool):
                return value
            if str(value).lower() in ("true", "1", "yes", "on"):
                return True
            if str(value).lower() in ("false", "0", "no", "off"):
                return False
            raise ValueError
        if t == "list":
            if not isinstance(value, list):
                raise ValueError
            return value
    except (ValueError, TypeError) as exc:
        if t == "string" and str(exc):
            raise ValueError(f"{exc} (got {value!r})") from None
        raise ValueError(f"expected {t}, got {value!r}") from None
    raise ValueError(f"unknown type {t}")
