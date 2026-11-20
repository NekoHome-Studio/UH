"""M2-1 插件驱动基础闭环。"""
import asyncio
import base64
import json
import os
import shutil
import time
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient

from kernel.plugins import (
    PluginDriver,
    PluginError,
    PluginSecurityConfig,
    _canonical_digest,
)
from kernel.main import create_app
from kernel.sandbox import SandboxRunner


def _make_package(path: Path, *, name: str = "hello", version: str = "1.0.0", signature: bool = True, extra_entries: dict[str, bytes] | None = None, signing_key: Ed25519PrivateKey | None = None) -> bytes:
    entries = {
        "manifest.yaml": (
            f"name: {name}\nversion: {version}\ntype: tool\ndescription: test plugin\n"
            "author: test\nlicense: MIT\nentry: main.py\npermissions: []\n"
            "risk_level: read-only\nconfig:\n  timeout_ms: 1000\n  memory_limit_mb: 16\n"
        ).encode(),
        "main.py": b"print('hello')\n",
    }
    entries.update(extra_entries or {})
    digest = _canonical_digest(sorted(entries.items()))
    private = signing_key or Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    with zipfile.ZipFile(path, "w") as archive:
        for name_, content in entries.items():
            archive.writestr(name_, content)
        if signature:
            archive.writestr(
                "SIGNATURE",
                json.dumps(
                    {
                        "algorithm": "ed25519",
                        "key_id": "test",
                        "digest": digest,
                        "signature": base64.b64encode(private.sign(bytes.fromhex(digest))).decode(),
                    }
                ),
            )
    return public


def test_signed_audit_hook_records_a_real_route_and_unsubscribes(monkeypatch, tmp_path):
    template = Path(__file__).resolve().parents[2] / "examples/plugins/official/audit-log"
    package = tmp_path / "audit.uhpkg"
    entries = {name: (template / name).read_bytes() for name in ("manifest.yaml", "main.py")}
    signing_key = Ed25519PrivateKey.generate()
    public = _make_package(package, extra_entries=entries, signing_key=signing_key)
    invalid = tmp_path / "invalid.uhpkg"
    _make_package(invalid, signing_key=signing_key, extra_entries={
        **entries,
        "manifest.yaml": entries["manifest.yaml"].replace(b"kernel.route.decided", b"unknown.hook.event"),
    })
    trust_file = tmp_path / "trusted.json"
    trust_file.write_text(json.dumps({"test": base64.b64encode(public).decode()}), encoding="utf-8")
    plugin_root = tmp_path / "plugins"
    monkeypatch.setenv("UH_PLUGIN_ROOT", str(plugin_root))
    monkeypatch.setenv("UH_PLUGIN_REGISTRY", str(tmp_path / "plugins.json"))
    monkeypatch.setenv("UH_PLUGIN_TRUSTED_KEYS_FILE", str(trust_file))
    monkeypatch.setenv("UH_LOCAL_MODE", "1")
    app = create_app()
    if os.name != "nt":
        # CI verifies the Hook protocol on a trusted fixture. Windows also
        # exercises Job Object launch. bwrap integration needs its own host.
        runner = app.state.plugin_driver.loaders["hook"].runner
        runner.enforce = False
        monkeypatch.setattr("kernel.sandbox.shutil.which", lambda _: None)

    with TestClient(app) as client:
        assert client.post("/api/v1/plugins", json={"source": str(invalid)}).status_code == 400
        installed = client.post("/api/v1/plugins", json={"source": str(package)})
        assert installed.status_code == 201, installed.text
        log_path = plugin_root / "audit-log" / "0.1.0" / "audit.jsonl"
        session = client.post("/api/v1/sessions").json()["data"]["session_id"]
        task = client.post(f"/api/v1/sessions/{session}/messages", json={"content": "private audit fixture"}).json()["data"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if log_path.exists() and (logged := log_path.read_text(encoding="utf-8")).endswith("\n"):
                break
            time.sleep(0.02)
        else:
            pytest.fail("audit hook did not record a route decision")

        entry = json.loads(logged.splitlines()[0])
        assert entry["event"] == "kernel.route.decided"
        assert entry["trace_id"] == task["trace_id"]
        assert entry["event_id"] > 0
        route_event = app.state.tasks.tasks[task["task_id"]]["event_buffer"][0]["event"]
        assert entry["span_id"] == route_event["span_id"]
        assert entry["payload"]["task_type"] == "chat"
        assert "private audit fixture" not in logged
        assert client.delete("/api/v1/plugins/audit-log").status_code == 200
        assert app.state.hooks.names == ()
        assert not log_path.exists()


def test_signed_install_persists_and_uninstalls_without_residue(tmp_path):
    package = tmp_path / "hello.uhpkg"
    public = _make_package(package)
    driver = PluginDriver(
        root=tmp_path / "plugins",
        registry_path=tmp_path / "plugins.json",
        security=PluginSecurityConfig(trusted_keys={"test": public}),
    )
    installed = asyncio.run(driver.install(package))
    assert installed["status"] == "active"
    assert driver.get("hello")["version"] == "1.0.0"
    restored = PluginDriver(
        root=tmp_path / "plugins",
        registry_path=tmp_path / "plugins.json",
        security=PluginSecurityConfig(trusted_keys={"test": public}),
    )
    assert restored.get("hello")["status"] == "installed"
    result = asyncio.run(restored.uninstall("hello"))
    assert result["status"] == "uninstalled"
    assert not (tmp_path / "plugins" / "hello").exists()


def test_duplicate_install_is_idempotent_but_different_digest_is_rejected(tmp_path):
    package = tmp_path / "hello.uhpkg"
    signing_key = Ed25519PrivateKey.generate()
    public = _make_package(package, signing_key=signing_key)
    driver = PluginDriver(
        root=tmp_path / "plugins",
        registry_path=tmp_path / "plugins.json",
        security=PluginSecurityConfig(trusted_keys={"test": public}),
    )
    first = asyncio.run(driver.install(package))
    second = asyncio.run(driver.install(package))
    assert second["package_sha256"] == first["package_sha256"]
    other = tmp_path / "other.uhpkg"
    _make_package(other, extra_entries={"README.md": b"different"}, signing_key=signing_key)
    with pytest.raises(PluginError) as raised:
        asyncio.run(driver.install(other))
    assert raised.value.code == "UH-USR-1003"


def test_no_verify_requires_local_explicit_bypass(tmp_path):
    package = tmp_path / "unsigned.uhpkg"
    _make_package(package, signature=False)
    strict = PluginDriver(root=tmp_path / "strict", registry_path=tmp_path / "strict.json")
    with pytest.raises(PluginError):
        strict.validate(package, no_verify=True)
    dev = PluginDriver(
        root=tmp_path / "dev",
        registry_path=tmp_path / "dev.json",
        security=PluginSecurityConfig(allow_unverified=True),
    )
    assert dev.validate(package, no_verify=True)["status"] == "validated"


def test_trusted_keys_file_is_loaded_for_production(monkeypatch, tmp_path):
    key = Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    key_file = tmp_path / "trusted-keys.json"
    key_file.write_text(json.dumps({"official": base64.b64encode(key).decode()}), encoding="utf-8")
    monkeypatch.setenv("UH_AUTH_REQUIRED", "1")
    monkeypatch.setenv("UH_LOCAL_MODE", "0")
    monkeypatch.setenv("UH_PLUGIN_TRUSTED_KEYS_FILE", str(key_file))
    config = PluginSecurityConfig.from_env()
    assert "official" in config.trusted_keys


def test_market_source_resolves_from_local_market_root(monkeypatch, tmp_path):
    package = tmp_path / "hello.uhpkg"
    _make_package(package)
    market = tmp_path / "market"
    market.mkdir()
    shutil.copy2(package, market / "hello@1.0.0.uhpkg")
    monkeypatch.setenv("UH_PLUGIN_MARKET_ROOT", str(market))
    driver = PluginDriver(security=PluginSecurityConfig(allow_unverified=True))
    result = driver.validate("market://hello@1.0.0", no_verify=True)
    assert result["name"] == "hello"


def test_git_source_host_allowlist_is_enforced(tmp_path):
    driver = PluginDriver(security=PluginSecurityConfig(allow_unverified=True))
    with pytest.raises(PluginError) as raised:
        driver.validate("git+https://evil.example/uh/plugin.git", no_verify=True)
    assert raised.value.code == "UH-KRN-2010"


def test_plugin_crashes_restart_three_times_then_quarantine(tmp_path):
    package = tmp_path / "hello.uhpkg"
    public = _make_package(package, extra_entries={"main.py": b"import time; time.sleep(0.1)\n"})
    driver = PluginDriver(
        root=tmp_path / "plugins",
        registry_path=tmp_path / "plugins.json",
        security=PluginSecurityConfig(trusted_keys={"test": public}),
        sandbox_runner=SandboxRunner(enforce=False, root=tmp_path / "plugins"),
    )
    async def exercise():
        await driver.install(package)
        # 轮询直到隔离（确定性条件，替代固定 sleep 撞崩溃重启时序——M2 验收 flaky 修复）
        for _ in range(150):
            if driver.records["hello"].status == "quarantined":
                break
            await asyncio.sleep(0.1)

    asyncio.run(exercise())
    assert driver.records["hello"].crash_count >= 3
    assert driver.records["hello"].status == "quarantined"


def test_signature_mismatch_is_rejected_before_extraction(tmp_path):
    package = tmp_path / "hello.uhpkg"
    _make_package(package)
    driver = PluginDriver(
        root=tmp_path / "plugins",
        registry_path=tmp_path / "plugins.json",
        security=PluginSecurityConfig(trusted_keys={}),
    )
    with pytest.raises(PluginError) as raised:
        driver.validate(package)
    assert raised.value.code == "UH-KRN-2010"
    assert not (tmp_path / "plugins").exists()


def test_static_scan_rejects_dangerous_exec(tmp_path):
    # SBX-02：危险 exec 调用静态扫描拒绝
    package = tmp_path / "evil.uhpkg"
    public = _make_package(package, name="evil", extra_entries={"main.py": b"import os\nos.system('rm -rf /')\n"})
    driver = PluginDriver(root=tmp_path / "plugins", registry_path=tmp_path / "plugins.json", security=PluginSecurityConfig(trusted_keys={"test": public}))
    with pytest.raises(PluginError) as raised:
        driver.validate(package)
    assert raised.value.detail.get("violation") == "static_scan.exec"


def test_static_scan_flags_network_import(tmp_path):
    # SBX-02：出网导入告警（不拒绝，需 allow_hosts 声明）
    package = tmp_path / "net.uhpkg"
    public = _make_package(package, name="net", extra_entries={"main.py": b"import socket\nprint('hi')\n"})
    driver = PluginDriver(root=tmp_path / "plugins", registry_path=tmp_path / "plugins.json", security=PluginSecurityConfig(trusted_keys={"test": public}))
    info = driver.validate(package)
    assert any(f["kind"] == "network" for f in info["static_scan"])


def test_package_path_traversal_is_rejected(tmp_path):
    package = tmp_path / "bad.uhpkg"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("manifest.yaml", b"name: bad\n")
        archive.writestr("../escape.py", b"bad")
    driver = PluginDriver(
        root=tmp_path / "plugins",
        registry_path=tmp_path / "plugins.json",
        security=PluginSecurityConfig(allow_unverified=True),
    )
    with pytest.raises(PluginError) as raised:
        driver.validate(package, no_verify=True)
    assert raised.value.code == "UH-KRN-2010" or raised.value.code == "UH-USR-1003"


def test_activation_failure_rolls_back_registry_and_files(tmp_path):
    package = tmp_path / "hello.uhpkg"
    public = _make_package(package)

    class FailingLoader:
        async def load(self, manifest, package_dir):
            return None

        async def activate(self, manifest, package_dir):
            raise RuntimeError("boom")

        async def deactivate(self, manifest, package_dir):
            return None

        async def uninstall(self, manifest, package_dir):
            return None

        def force_kill(self, manifest, package_dir):
            return None

    driver = PluginDriver(
        root=tmp_path / "plugins",
        registry_path=tmp_path / "plugins.json",
        security=PluginSecurityConfig(trusted_keys={"test": public}),
        loaders={"tool": FailingLoader()},
    )
    with pytest.raises(PluginError) as raised:
        asyncio.run(driver.install(package))
    assert raised.value.code == "UH-KRN-2010"
    assert driver.list() == []
    assert not (tmp_path / "plugins" / "hello").exists()


def test_deactivate_timeout_force_kills_and_quarantines(tmp_path, monkeypatch):
    package = tmp_path / "hello.uhpkg"
    public = _make_package(package)
    killed = []

    class SlowLoader:
        async def load(self, manifest, package_dir):
            return None

        async def activate(self, manifest, package_dir):
            return None

        async def deactivate(self, manifest, package_dir):
            await asyncio.sleep(0.05)

        async def uninstall(self, manifest, package_dir):
            return None

        def force_kill(self, manifest, package_dir):
            killed.append(manifest.name)

    monkeypatch.setattr("kernel.plugins.DEACTIVATE_TIMEOUT_S", 0.001)
    driver = PluginDriver(
        root=tmp_path / "plugins",
        registry_path=tmp_path / "plugins.json",
        security=PluginSecurityConfig(trusted_keys={"test": public}),
        loaders={"tool": SlowLoader()},
    )
    asyncio.run(driver.install(package))
    result = asyncio.run(driver.deactivate("hello"))
    assert result["status"] == "quarantined"
    assert killed == ["hello"]


def test_plugin_rest_install_list_config_uninstall(tmp_path):
    package = tmp_path / "hello.uhpkg"
    public = _make_package(package)
    app = create_app()
    app.state.plugin_driver = PluginDriver(
        root=tmp_path / "plugins",
        registry_path=tmp_path / "plugins.json",
        security=PluginSecurityConfig(trusted_keys={"test": public}),
    )
    with TestClient(app) as client:
        installed = client.post("/plugins", json={"source": str(package)})
        assert installed.status_code == 201
        assert client.get("/plugins").json()["data"]["total"] == 1
        updated = client.patch("/plugins/hello/config", json={"config": {"timeout_ms": 2000}})
        assert updated.status_code == 200
        restored = PluginDriver(
            root=tmp_path / "plugins",
            registry_path=tmp_path / "plugins.json",
            security=PluginSecurityConfig(trusted_keys={"test": public}),
        )
        assert restored.records["hello"].manifest.config["timeout_ms"] == 2000
        removed = client.delete("/plugins/hello")
        assert removed.status_code == 200
        assert client.get("/plugins").json()["data"]["total"] == 0

