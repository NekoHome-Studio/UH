import base64
import json
import secrets
import shutil
import struct
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from zlib import compress, crc32

import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from kernel.cli import main as uh_cli
from kernel.main import create_app
from kernel.plugins import PluginDriver, PluginSecurityConfig, PluginValidationError, _canonical_digest


ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples/plugins/official/theme-light/manifest.yaml"


def _png():
    def chunk(kind, content):
        return struct.pack(">I", len(content)) + kind + content + struct.pack(">I", crc32(kind + content))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", compress(b"\x00\xff\xff\xff\xff"))
        + chunk(b"IEND", b"")
    )


def _package(path, config=None, assets=None, signing_key=None):
    manifest = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    if config is not None:
        manifest["config"] = config
    entries = {"manifest.yaml": yaml.safe_dump(manifest).encode(), **(assets or {})}
    if signing_key is not None:
        digest = _canonical_digest(list(entries.items()))
        entries["SIGNATURE"] = json.dumps({
            "algorithm": "ed25519", "key_id": "theme-test", "digest": digest,
            "signature": base64.b64encode(signing_key.sign(bytes.fromhex(digest))).decode(),
        }).encode()
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return path


@pytest.fixture
def driver(tmp_path):
    return PluginDriver(
        root=tmp_path / "plugins", registry_path=tmp_path / "plugins.json",
        security=PluginSecurityConfig(signature_required=False),
    )


@pytest.mark.parametrize("name, value", [
    ("--outside", "red"), ("--uh-bg-primary", "url(https://example.test/bg)"),
    ("--uh-bg-primary", "expression(alert(1))"), ("--uh-bg-primary", "javascript:alert(1)"),
    ("--uh-bg-primary", "red; background: blue"), ("--uh-radius", "1px 2px"),
])
def test_theme_rejects_unsafe_css(driver, tmp_path, name, value):
    package = _package(tmp_path / "theme.uhpkg", {"css_variables": {name: value}})
    with pytest.raises(PluginValidationError):
        driver.validate(package)


def test_theme_rejects_unsafe_fonts(driver, tmp_path):
    package = _package(tmp_path / "theme.uhpkg", {
        "css_variables": {"--uh-bg-primary": "red"},
        "font_stack": ['font" onload="alert(1)'],
    })
    with pytest.raises(PluginValidationError):
        driver.validate(package)


@pytest.mark.parametrize("name, content", [
    ("main.js", b"alert(1)"), ("assets/preview.svg", b'<svg onload="alert(1)"/>'),
    ("assets/preview.png", b'<img onerror="alert(1)">'),
    ("assets/preview.png", _png() + b"<script>"),
    ("assets/preview.png", b"x" * (2 * 1024 * 1024 + 1)),
    ("assets/fonts/font.woff2", b"wOF2"),
])
def test_theme_rejects_unapproved_assets(driver, tmp_path, name, content):
    package = _package(tmp_path / "theme.uhpkg", assets={name: content})
    with pytest.raises(PluginValidationError):
        driver.validate(package)


def test_theme_selection_and_assets_survive_restart(monkeypatch, tmp_path):
    monkeypatch.setenv("UH_PLUGIN_ROOT", str(tmp_path / "plugins"))
    monkeypatch.setenv("UH_PLUGIN_REGISTRY", str(tmp_path / "plugins.json"))
    monkeypatch.setenv("UH_LOCAL_MODE", "1")
    key = Ed25519PrivateKey.generate()
    trust_file = tmp_path / "trusted.json"
    trust_file.write_text(json.dumps({"theme-test": base64.b64encode(key.public_key().public_bytes_raw()).decode()}), encoding="utf-8")
    monkeypatch.setenv("UH_PLUGIN_TRUSTED_KEYS_FILE", str(trust_file))
    monkeypatch.setenv("UH_PLUGIN_SIGNATURE_REQUIRED", "1")
    package = _package(tmp_path / "theme.uhpkg", assets={"assets/preview.png": _png()}, signing_key=key)

    with TestClient(create_app()) as client:
        selection = "/api/v1/themes/active"
        assert client.put(selection, json={"name": "theme-light"}).status_code == 404
        assert client.put(selection, json={}).status_code == 400
        response = client.post("/api/v1/plugins", json={"source": str(package)})
        assert response.status_code == 201, response.text
        assert client.put(selection, json={"name": "theme-light"}).json()["data"] == {"active": "theme-light"}
        response = client.get("/api/v1/themes/theme-light/assets/preview.png")
        assert response.content == _png()
        assert response.headers["content-type"] == "image/png"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert client.get("/api/v1/themes/theme-light/assets/manifest.yaml").status_code == 404
        bad_config = {"config": {"css_variables": {"--uh-bg-primary": "url(https://example.test/bg)"}}}
        assert client.patch("/api/v1/plugins/theme-light/config", json=bad_config).status_code == 400

    with TestClient(create_app()) as client:
        catalog = client.get("/api/v1/themes").json()["data"]
        assert catalog["active"] == "theme-light"
        assert catalog["themes"][0]["css_variables"]["--uh-bg-primary"] == "#f8fafc"
        assert client.put(selection, json={"name": None}).json()["data"] == {"active": None}
        assert client.put(selection, json={"name": "theme-light"}).status_code == 200
        assert client.delete("/api/v1/plugins/theme-light").status_code == 200
        assert client.get("/api/v1/themes").json()["data"] == {"active": None, "themes": []}


def test_container_cli_hot_installs_signed_theme(monkeypatch, tmp_path, capsys):
    key = Ed25519PrivateKey.generate()
    trust_file = tmp_path / "trusted.json"
    trust_file.write_text(json.dumps({"theme-test": base64.b64encode(key.public_key().public_bytes_raw()).decode()}), encoding="utf-8")
    token_file = tmp_path / "bearer"
    token_file.write_text(secrets.token_urlsafe(), encoding="utf-8")
    monkeypatch.delenv("UH_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("UH_WS_TOKEN", raising=False)
    monkeypatch.delenv("UH_WS_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("UH_LOCAL_MODE", "0")
    monkeypatch.setenv("UH_AUTH_REQUIRED", "1")
    monkeypatch.setenv("UH_AUTH_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("UH_PLUGIN_INSTALL_VIA_KERNEL", "1")
    monkeypatch.setenv("UH_PLUGIN_ROOT", str(tmp_path / "plugins"))
    monkeypatch.setenv("UH_PLUGIN_REGISTRY", str(tmp_path / "plugins.json"))
    monkeypatch.setenv("UH_PLUGIN_TRUSTED_KEYS_FILE", str(trust_file))
    package = _package(tmp_path / "theme.uhpkg", signing_key=key)
    app = create_app()
    with TestClient(app) as client:
        assert client.post("/api/v1/plugins", json={"source": str(package)}).status_code == 401

        connection = Mock()

        def send(method, path, *, body, headers):
            response = client.request(method, path, content=body, headers=headers)
            connection.getresponse.return_value = SimpleNamespace(status=response.status_code, read=lambda: response.content)

        connection.request.side_effect = send
        factory = Mock(return_value=connection)
        monkeypatch.setattr("kernel.cli.http.client.HTTPConnection", factory)
        assert uh_cli(["plugin", "install", str(package)]) == 0
        factory.assert_called_once_with("127.0.0.1", 8080, timeout=120)
        assert json.loads(capsys.readouterr().out)["status"] == "active"
        assert app.state.plugin_driver.get("theme-light")["status"] == "active"
    assert (tmp_path / "plugins/theme-light/0.1.0/manifest.yaml").is_file()
    with TestClient(create_app()) as client:
        assert client.app.state.plugin_driver.get("theme-light")["status"] == "active"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js required for the theme UI unit test")
def test_theme_apply_updates_and_clears_256_variables():
    module = json.dumps(str(ROOT / "ui/themes.js"))
    subprocess.run(["node", "-e", f"""
        const assert = require('node:assert/strict');
        const {{ createThemeApplier }} = require({module});
        const style = new Map();
        const apply = createThemeApplier({{
          setProperty: (name, value) => style.set(name, value),
          removeProperty: name => style.delete(name),
        }});
        const css_variables = Object.fromEntries(Array.from({{ length: 256 }},
          (_, i) => [`--uh-test-${{i}}`, '#fff']));
        const start = performance.now();
        apply({{ css_variables, font_stack: ['UH Sans'] }});
        assert.ok(performance.now() - start < 200);
        assert.equal(style.size, 257);
        assert.equal(style.get('--uh-font-family'), '"UH Sans", system-ui, sans-serif');
        apply({{ css_variables: {{ '--uh-radius': '8px' }} }});
        assert.deepEqual([...style], [['--uh-radius', '8px']]);
        apply(null);
        assert.equal(style.size, 0);
    """], check=True, capture_output=True, text=True)
