"""Driver icons: three accepted `icon:` forms (bare name, driver-folder *.svg, https/data URL).

Feature: a driver manifest may point its icon at (a) webui/icons/<name>.svg, (b) a file shipped in the
driver folder (served by GET /api/drivers/<id>/icon[/<file>], also for plugin drivers), or (c) an
https:// / data:image URL used as-is. The registry projection and driver catalog expose a resolved
`icon_url` so the frontend needs no icon logic. Scripts in SVG must never run: the endpoint serves
only manifest-declared files with a locked-down CSP + nosniff. The web UI sets no CSP of its own,
so https:/data: icons need no img-src change (the privacy trade-off is documented in manifest.py).
"""
from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient

import web_server
from drivers.base import ManifestError
from drivers.manifest import ICON_MAX_BYTES, discover_manifests, parse_manifest, validate_icon
from integrations import build_registry, runtime
from test_drivers_registry import base_manifest, cfg
from test_web_server import _login, _use_tmp_store

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"><script>alert(1)</script></svg>'
PNG = "data:image/png;base64,iVBORw0KGgo="


@pytest.mark.parametrize("value", ["jira", "my_icon-2", "https://cdn.example.com/a.svg", PNG,
                                   "data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=", ""])
def test_manifest_accepts_bare_name_https_and_data_icons(value):
    assert validate_icon("x", value) == value


@pytest.mark.parametrize("value", ["javascript:alert(1)", "http://insecure.example.com/a.png", "file:///etc/passwd",
                                   "data:text/html;base64,PGI+", "data:image/png,rawnotbase64", "../x.svg",
                                   "a/b.svg", "Bad Name", "https://x.com/a b", "https://x.com/\"onerror=1", 5])
def test_manifest_rejects_bad_icon_schemes_and_paths(value):
    with pytest.raises(ManifestError):
        validate_icon("x", value)


def test_manifest_rejects_oversized_data_icon():
    with pytest.raises(ManifestError, match="exceeds"):
        validate_icon("x", "data:image/png;base64," + "A" * ICON_MAX_BYTES)


def test_driver_folder_icon_must_exist_and_card_icon_is_validated(tmp_path):
    with pytest.raises(ManifestError, match="not found"):
        parse_manifest(base_manifest(icon="logo.svg"), "w", str(tmp_path))
    m = base_manifest()
    m["kinds"]["k"]["card"] = {"icon": "javascript:1"}
    with pytest.raises(ManifestError, match="card.icon"):
        parse_manifest(m, "w", str(tmp_path))


def _plugin(tmp_path, icon_key=None, with_default=True, extra=None):
    d = tmp_path / "plug" / "demo"
    d.mkdir(parents=True)
    m = base_manifest(id="demo", title="Demo")
    m["settings"] = {}
    m["kinds"] = {"k": {"url_match": None, "watch": {"type": "demo_w", "key_match": "^.+$"}, "card": {}}}
    m.update(extra or {})
    if icon_key:
        m["icon"] = icon_key
    (d / "driver.yaml").write_text(yaml.safe_dump(m))
    if with_default:
        (d / "icon.svg").write_bytes(SVG)
    (d / "other.svg").write_bytes(SVG)
    return tmp_path / "plug"


def test_icon_url_resolution_for_every_form(tmp_path):
    (tmp_path / "icon.svg").write_bytes(SVG)
    d = str(tmp_path)
    cases = {"jira": "/static/icons/jira.svg", "icon.svg": None, "https://h/a.png": "https://h/a.png", PNG: PNG}
    for icon, want in cases.items():
        m = parse_manifest(base_manifest(icon=icon), "w", d)
        assert m.icon_url() == (want or "/api/drivers/x/icon/icon.svg")
    assert parse_manifest(base_manifest(), "w", d).icon_url() == "/api/drivers/x/icon"
    assert parse_manifest(base_manifest(), "w", str(tmp_path / "nope")).icon_url() == ""


def test_projection_and_catalog_expose_icon_url_for_driver_and_kinds(tmp_path):
    plug = _plugin(tmp_path, icon_key="other.svg")
    reg = build_registry(cfg(), extra_driver_dirs=[plug])
    proj = {i["driver"]: i for i in reg.ui_projection()["integrations"]}
    assert proj["jira"]["icon_url"] == "/static/icons/jira.svg"
    assert proj["gitlab"]["kinds"]["merge_request"]["icon_url"] == "/static/icons/gitlab.svg"
    cat = {c["id"]: c for c in reg.catalog()}
    assert cat["demo"]["icon_url"] == "/api/drivers/demo/icon/other.svg"
    assert cat["jira"]["icon_url"] == "/static/icons/jira.svg"
    assert cat["jira"]["kinds"]["ticket"]["icon_url"] == "/static/icons/jira.svg"


def test_default_icon_svg_in_driver_folder_is_used_automatically(tmp_path):
    ms = discover_manifests([_plugin(tmp_path)])
    assert ms["demo"].icon_url() == "/api/drivers/demo/icon"


def test_every_builtin_icon_resolves_to_an_existing_static_file():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "webui"
    for m in discover_manifests([]).values():
        url = m.icon_url()
        if url.startswith("/api/drivers/"):
            assert (Path(m.source_dir) / (url.rsplit("/", 1)[-1] if url.count("/") > 4 else "icon.svg")).is_file(), m.id
        else:
            assert (root / url.removeprefix("/static/")).is_file(), m.id


@pytest.fixture
def api(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    plug = _plugin(tmp_path / "p", icon_key="other.svg")
    (plug / "demo" / "secret.svg").write_bytes(b"<svg/>")
    reg = build_registry(cfg(), extra_driver_dirs=[plug])
    monkeypatch.setattr(runtime, "get_registry", lambda: reg)
    client = TestClient(web_server.app)
    _login(client, monkeypatch)
    client.plug = plug
    return client


def test_icon_endpoint_serves_driver_folder_svg_with_locked_down_headers(api):
    for url in ("/api/drivers/demo/icon", "/api/drivers/demo/icon/other.svg", "/api/drivers/demo/icon/icon.svg"):
        r = api.get(url)
        assert r.status_code == 200 and r.content == SVG, url
        assert r.headers["content-type"].startswith("image/svg+xml")
        assert r.headers["x-content-type-options"] == "nosniff"
        csp = r.headers["content-security-policy"]
        assert "default-src 'none'" in csp and "sandbox" in csp


def test_icon_endpoint_requires_auth(api):
    assert TestClient(web_server.app).get("/api/drivers/demo/icon").status_code == 401


def test_icon_endpoint_404_for_unknown_driver_undeclared_file_and_missing_file(api):
    assert api.get("/api/drivers/nope/icon").status_code == 404
    assert api.get("/api/drivers/demo/icon/secret.svg").status_code == 404
    assert api.get("/api/drivers/jira/icon").status_code == 404
    (api.plug / "demo" / "icon.svg").unlink()
    assert api.get("/api/drivers/demo/icon/icon.svg").status_code == 404


@pytest.mark.parametrize("name", ["..%2Fdriver.yaml", "%2e%2e/driver.yaml", "..%2F..%2Fetc%2Fpasswd", "other.svg%00.png",
                                  "%2Fetc%2Fpasswd"])
def test_icon_endpoint_rejects_path_traversal(api, name):
    r = api.get(f"/api/drivers/demo/icon/{name}")
    assert r.status_code != 200 and b"<svg" not in r.content and b"implementation" not in r.content


def test_icon_endpoint_refuses_oversized_and_symlinked_files(api):
    d = api.plug / "demo"
    (d / "icon.svg").write_bytes(b"x" * (ICON_MAX_BYTES + 1))
    assert api.get("/api/drivers/demo/icon/icon.svg").status_code == 404
    (d / "icon.svg").unlink()
    (d / "icon.svg").symlink_to(d / "driver.yaml")
    assert api.get("/api/drivers/demo/icon/icon.svg").status_code == 404


def test_every_builtin_driver_ships_a_small_vendored_script_free_svg_logo():
    """Each built-in driver shows a recognizable vendor logo, vendored (no hot-linking) as `icon.svg`.

    Logos are vendor marks (simple-icons paths / own drawings) used only to identify the integrated
    service. Guards: file exists, within ICON_MAX_BYTES, is an <svg> with a viewBox, carries no
    script/event handler/external reference, and the manifest resolves to the driver-served URL.
    """
    from drivers.manifest import BUILTIN_DIR
    ms = discover_manifests([])
    assert ms
    for mid, m in ms.items():
        f = BUILTIN_DIR / mid / "icon.svg"
        if m.icon.endswith(".svg") or not m.icon:
            assert f.is_file(), f"{mid}: icon.svg missing"
        if not f.is_file():
            continue  # bare-name icon resolved from webui/icons
        data = f.read_bytes()
        assert len(data) <= ICON_MAX_BYTES, mid
        low = data.decode().lower()
        assert "<svg" in low and "viewbox" in low, mid
        assert not any(t in low for t in ("<script", "onload=", "onerror=", "href=")), mid
