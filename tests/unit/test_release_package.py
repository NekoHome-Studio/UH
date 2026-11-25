"""R-3 发布包：打包 / 签名 / 可安装性（#39）。"""
import asyncio
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _build(tmp_path, key_name="k.pem", out_name="rel"):
    key = tmp_path / key_name
    out = tmp_path / out_name
    result = subprocess.run(
        [sys.executable, "scripts/build-release.py", "--generate-key",
         "--key-file", str(key), "--out-dir", str(out)],
        cwd=ROOT, capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stderr
    manifest = json.loads((out / "release-manifest.json").read_text(encoding="utf-8"))
    return out, manifest


def test_build_release_script_produces_signed_packages(tmp_path):
    out, manifest = _build(tmp_path)
    assert manifest["release"] == "v0.1.0"
    assert len(manifest["packages"]) >= 5
    assert (out / "trusted-keys.json").exists()
    # 私钥不得出现在产物目录
    assert not list(out.glob("*.pem"))
    for pkg in manifest["packages"]:
        path = out / pkg["file"]
        assert path.exists()
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            assert "manifest.yaml" in names
            assert "SIGNATURE" in names
            sig = json.loads(zf.read("SIGNATURE"))
            assert sig["algorithm"] == "ed25519"
            assert sig["digest"] == pkg["digest"]


def test_signed_packages_install_with_trusted_key(tmp_path):
    from kernel.plugins import PluginDriver, PluginSecurityConfig

    out, manifest = _build(tmp_path, "k1.pem", "rel1")
    keys = {manifest["key_id"]: manifest["public_key_base64"]}

    async def scenario():
        driver = PluginDriver(root=tmp_path / "plugins",
                              security=PluginSecurityConfig(trusted_keys=keys))
        installed = 0
        for pkg in manifest["packages"]:
            await driver.install(str(out / pkg["file"]))
            installed += 1
        listed = driver.list()
        await driver.shutdown()
        return installed, len(listed)

    installed, listed = asyncio.run(scenario())
    assert installed == len(manifest["packages"])
    assert listed == installed


def test_untrusted_key_rejects_package(tmp_path):
    from kernel.plugins import PluginDriver, PluginPolicyError, PluginSecurityConfig

    out, manifest = _build(tmp_path, "k2.pem", "rel2")
    first = out / manifest["packages"][0]["file"]

    async def scenario():
        driver = PluginDriver(root=tmp_path / "p2", security=PluginSecurityConfig(trusted_keys={}))
        with pytest.raises(PluginPolicyError):
            await driver.install(str(first))
        await driver.shutdown()

    asyncio.run(scenario())


def test_release_doc_exists_and_mentions_key_mechanics():
    doc = ROOT / "docs" / "deployment" / "release-package.md"
    assert doc.exists()
    text = doc.read_text(encoding="utf-8")
    assert "Ed25519" in text
    assert "bytes.fromhex" in text
    assert "buildx" in text
