"""M2-1 插件驱动基础闭环。"""
import asyncio
import base64
import json
import os
import shutil
import ssl
import subprocess
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
from fastapi.testclient import TestClient

from kernel.cli import main as uh_cli
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


@pytest.mark.parametrize("src", [
    b"import subprocess as sp\nsp.Popen(['x'])\n",
    b"from os import system\nsystem('whoami')\n",
    b"import os\ngetattr(os, 'system')('whoami')\n",
    b"import importlib\nimportlib.import_module('socket')\n",
    b"import os as o\no.system('x')\n",
])
def test_static_scan_rejects_bypass_variants(tmp_path, src):
    # R-1：别名/from-import/getattr/importlib 绕过均拒绝
    package = tmp_path / "bypass.uhpkg"
    public = _make_package(package, name="bypass", extra_entries={"main.py": src})
    driver = PluginDriver(root=tmp_path / "plugins", registry_path=tmp_path / "plugins.json", security=PluginSecurityConfig(trusted_keys={"test": public}))
    with pytest.raises(PluginError) as raised:
        driver.validate(package)
    assert raised.value.detail.get("violation") == "static_scan.exec"


def test_static_scan_rejects_js_entry(tmp_path):
    # R-1：JS 入口危险调用纳入扫描
    package = tmp_path / "js.uhpkg"
    public = _make_package(package, name="js", extra_entries={"main.mjs": b"require('child_process').execSync('x')\n"})
    driver = PluginDriver(root=tmp_path / "plugins", registry_path=tmp_path / "plugins.json", security=PluginSecurityConfig(trusted_keys={"test": public}))
    with pytest.raises(PluginError) as raised:
        driver.validate(package)
    assert raised.value.detail.get("violation") == "static_scan.exec"


def test_tool_subprocess_env_strips_credentials(tmp_path, monkeypatch):
    # R-2：tool 子进程环境剥离内核凭据
    monkeypatch.setenv("UH_AUTH_TOKEN", "SUPER-SECRET-PROBE")
    monkeypatch.setenv("UH_AUTH_TOKEN_FILE", "/secret/token.txt")
    from kernel.sandbox import SandboxRunner
    from kernel.plugins import PluginManifest
    runner = SandboxRunner(enforce=False, root=tmp_path)
    manifest = PluginManifest.from_dict({"name": "t", "version": "1.0.0", "type": "tool", "description": "x", "author": "a", "license": "MIT", "entry": "main.py", "permissions": [], "risk_level": "read-only", "config": {"timeout_ms": 1000, "memory_limit_mb": 16}})
    plan = runner.build_plan(manifest, tmp_path)
    assert "UH_AUTH_TOKEN" not in plan.env
    assert "UH_AUTH_TOKEN_FILE" not in plan.env


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


@pytest.fixture
def runtime_plugins(monkeypatch, tmp_path):
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    trust_file = tmp_path / "trusted.json"
    trust_file.write_text(json.dumps({"test": base64.b64encode(public).decode()}), encoding="utf-8")
    monkeypatch.setenv("UH_PLUGIN_TRUSTED_KEYS_FILE", str(trust_file))
    monkeypatch.setenv("UH_PLUGIN_ROOT", str(tmp_path / "plugins"))
    monkeypatch.setenv("UH_PLUGIN_REGISTRY", str(tmp_path / "registry.json"))
    monkeypatch.setenv("UH_CHECKPOINT_SQLITE_PATH", str(tmp_path / "checkpoints.sqlite3"))
    monkeypatch.setenv("UH_LONG_TERM_STORE", str(tmp_path / "memory.json"))
    monkeypatch.setenv("UH_RETRIEVAL_STORE", str(tmp_path / "retrieval.json"))
    monkeypatch.setenv("UH_LOCAL_MODE", "1")
    monkeypatch.setenv("UH_DEV_MODE", "1")
    monkeypatch.delenv("PYTHONPATH", raising=False)
    if os.name != "nt":
        original_app_factory = create_app

        def create_runtime_app():
            app = original_app_factory()
            app.state.plugin_driver.loaders["tool"].runner.enforce = False
            return app

        monkeypatch.setattr("kernel.sandbox.shutil.which", lambda _: None)
    else:
        create_runtime_app = create_app
    return key, create_runtime_app


def _runtime_package(path, key, *, name="brief-tool"):
    manifest = (
        f"name: {name}\nversion: 1.0.0\ntype: tool\ndescription: SDK fixture\n"
        "author: test\nlicense: MIT\nentry: main.py\npermissions: []\n"
        "risk_level: read-only\nconfig:\n  timeout_ms: 5000\n  memory_limit_mb: 128\n"
    )
    script = '''from pathlib import Path

import yaml
from kernel.plugin_sdk import ToolPlugin, run_stdio
from kernel.plugins import PluginManifest

root = Path(__file__).resolve().parent
plugin = ToolPlugin(PluginManifest.from_dict(yaml.safe_load((root / "manifest.yaml").read_text())), package_dir=root)

@plugin.tool("brief_echo", description="Echo a value")
def echo(args, context):
    return "plugin:" + args["text"]

@plugin.tool("brief_read", description="Read a file")
def read(args, context):
    return context.read_text(args["path"])

@plugin.tool("brief_network", description="Check an outbound destination")
def network(args, context):
    return context.allow_host(args["url"])

@plugin.tool("brief_confirm", description="Require user confirmation", risk_level="irreversible")
def confirm(args, context):
    return "confirmed tool ran"

@plugin.tool("brief_crash", description="Exit the fixture process")
def crash(args, context):
    raise SystemExit(1)

run_stdio(plugin)
'''
    _make_package(path, signing_key=key, extra_entries={"manifest.yaml": manifest.encode(), "main.py": script.encode()})


def _task_events(client, response):
    assert response.status_code == 202, response.text
    task = response.json()["data"]
    stream = client.get(task["sse_url"])
    assert stream.status_code == 200, stream.text
    streamed = [json.loads(line[6:]) for line in stream.text.splitlines() if line.startswith("data: ")]
    events = [entry["event"] for entry in client.app.state.tasks.tasks[task["task_id"]]["event_buffer"]]
    assert streamed[-1] == events[-1]
    return task, events


def _tool_task(client, session, name, arguments):
    return _task_events(client, client.post("/api/v1/dev/tool-task", json={
        "session_id": session, "tool_name": name, "risk_level": "read-only", "arguments": arguments,
    }))


def _workflow_task(client, session, name, arguments):
    return _task_events(client, client.post(f"/api/v1/sessions/{session}/messages", json={
        "content": "Run the installed tool", "mode": "workflow",
        "context": {"memory_recall": False, "tool": {"name": name, "arguments": arguments}},
    }))


def test_signed_tool_is_discovered_and_called_from_dev_and_workflow(runtime_plugins, tmp_path):
    key, app_factory = runtime_plugins
    package = tmp_path / "tool.uhpkg"
    _runtime_package(package, key)
    app = app_factory()
    app.state.orchestration.llm = None
    with TestClient(app) as client:
        installed = client.post("/api/v1/plugins", json={"source": str(package)})
        assert installed.status_code == 201, installed.text
        assert installed.json()["data"]["status"] == "active"
        assert app.state.rpc.is_available("mcp.tool.brief_echo")
        registry = app.state.plugin_driver
        transport = registry.tool_host("brief_echo").transport
        process = transport.process
        assert process is not None and process.returncode is None

        session = client.post("/api/v1/sessions").json()["data"]["session_id"]
        for submit in (_tool_task, _workflow_task):
            task, events = submit(client, session, "brief_echo", {"text": "SDK 子进程"})
            result = next(event for event in events if event["event"] == "tool.result")
            assert json.loads(result["payload"]["content_preview"])[0]["text"] == "plugin:SDK 子进程"
            assert result["trace_id"] == task["trace_id"]
            assert events[-1]["event"] == "task.finished"
            assert "plugin:SDK 子进程" in events[-1]["payload"]["final_result"]["content"]
        assert registry.records["brief-tool"].crash_count == 0
        assert transport.process is process and process.returncode is None

        _, denied = _workflow_task(client, session, "brief_confirm", {})
        assert denied[-1]["payload"]["error"]["code"] == "UH-USR-1002"
        assert next(event for event in denied if event["event"] == "tool.result")["payload"]["status"] == "denied"

        conflicting = tmp_path / "conflicting.uhpkg"
        _runtime_package(conflicting, key, name="other-tool")
        rejected = client.post("/api/v1/plugins", json={"source": str(conflicting)})
        assert rejected.status_code == 403, rejected.text
        assert rejected.json()["error"]["detail"]["violation"] == "tool_name_conflict"
        assert app.state.rpc.is_available("mcp.tool.brief_echo")
        assert registry.get("other-tool") is None
        assert not (registry.root / "other-tool").exists()

        assert client.delete("/api/v1/plugins/brief-tool").status_code == 200
        assert process.returncode is not None
        assert not app.state.rpc.is_registered("mcp.tool.brief_echo")
        assert registry.tool_host("brief_echo") is None


def test_kernel_restart_restores_signed_tool_and_hook(runtime_plugins, tmp_path):
    key, app_factory = runtime_plugins
    tool_package = tmp_path / "tool.uhpkg"
    _runtime_package(tool_package, key)
    hook_package = tmp_path / "hook.uhpkg"
    template = Path(__file__).resolve().parents[2] / "examples/plugins/official/audit-log"
    _make_package(hook_package, signing_key=key, extra_entries={name: (template / name).read_bytes() for name in ("manifest.yaml", "main.py")})
    first = app_factory()
    with TestClient(first) as client:
        for package in (tool_package, hook_package):
            installed = client.post("/api/v1/plugins", json={"source": str(package)})
            assert installed.status_code == 201, installed.text
        first_process = first.state.plugin_driver.tool_host("brief_echo").transport.process
        assert first.state.hooks.names == ("audit-log",)
    assert first_process.returncode is not None
    assert first.state.hooks.names == ()
    assert all(row["status"] == "active" for row in json.loads((tmp_path / "registry.json").read_text(encoding="utf-8")).values())

    restarted = app_factory()
    with TestClient(restarted) as client:
        statuses = {item["name"]: item["status"] for item in client.get("/api/v1/plugins").json()["data"]["plugins"]}
        assert statuses == {"audit-log": "active", "brief-tool": "active"}
        assert restarted.state.hooks.names == ("audit-log",)
        second_process = restarted.state.plugin_driver.tool_host("brief_echo").transport.process
        assert second_process is not first_process and second_process.returncode is None
        session = client.post("/api/v1/sessions").json()["data"]["session_id"]
        _, events = _tool_task(client, session, "brief_echo", {"text": "restarted"})
        assert events[-1]["event"] == "task.finished"
        handle = client.post(f"/api/v1/sessions/{session}/messages", json={"content": "hook after restart"}).json()["data"]
        audit = tmp_path / "plugins" / "audit-log" / "0.1.0" / "audit.jsonl"
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if audit.exists() and (data := audit.read_text(encoding="utf-8")).endswith("\n"):
                if any(json.loads(line)["trace_id"] == handle["trace_id"] for line in data.splitlines()):
                    break
            time.sleep(0.02)
        else:
            pytest.fail("restored hook did not audit the route decision")


def test_mcp_tool_removes_capabilities_after_three_child_crashes(runtime_plugins, tmp_path):
    key, app_factory = runtime_plugins
    package = tmp_path / "tool.uhpkg"
    _runtime_package(package, key)
    app = app_factory()
    driver = app.state.plugin_driver
    with TestClient(app) as client:
        installed = client.post("/api/v1/plugins", json={"source": str(package)})
        assert installed.status_code == 201, installed.text
        session = client.post("/api/v1/sessions").json()["data"]["session_id"]
        for crash_count in (1, 2, 3):
            host = driver.tool_host("brief_crash")
            assert host is not None
            _, events = _tool_task(client, session, "brief_crash", {})
            assert events[-1]["event"] == "task.failed"
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                current = driver.tool_host("brief_crash")
                if crash_count < 3 and current is not None and current is not host:
                    break
                if crash_count == 3 and driver.get("brief-tool")["status"] == "quarantined":
                    break
                time.sleep(0.02)
            else:
                pytest.fail(f"MCP process failed to recover or quarantine after crash {crash_count}")
            assert driver.records["brief-tool"].crash_count == crash_count
        assert driver.tool_host("brief_crash") is None
        assert not app.state.rpc.is_registered("mcp.tool.brief_echo")
        assert client.get("/health").status_code == 200


def test_sandbox_denials_return_code_and_persist_correlated_audit(runtime_plugins, tmp_path):
    key, app_factory = runtime_plugins
    package = tmp_path / "tool.uhpkg"
    _runtime_package(package, key)
    outside = tmp_path / "unshared-file.txt"
    outside.write_text("private test data", encoding="utf-8")
    app = app_factory()
    app.state.orchestration.backend = "fallback"
    mcp_results = {}

    async def record_mcp_result(envelope):
        mcp_results[envelope.trace_id] = envelope.payload

    app.state.bus.subscribe("mcp.tool.result", record_mcp_result)
    with TestClient(app) as client:
        installed = client.post("/api/v1/plugins", json={"source": str(package)})
        assert installed.status_code == 201, installed.text
        session = client.post("/api/v1/sessions").json()["data"]["session_id"]
        cases = (
            (_tool_task, "brief_read", {"path": str(outside)}, "fs.host.read", "filesystem"),
            (_workflow_task, "brief_network", {"url": "https://example.invalid"}, "network.outbound", "network.outbound"),
        )
        expected = []
        for submit, name, arguments, permission, violation in cases:
            task, events = submit(client, session, name, arguments)
            assert mcp_results[task["trace_id"]]["error"]["code"] == "UH-KRN-2010"
            assert events[-1]["event"] == "task.failed"
            assert events[-1]["payload"]["error"]["code"] == "UH-KRN-2010"
            expected.append({"event": "plugin.sandbox.denied", "plugin": "brief-tool", "permission": permission,
                             "violation": violation, "trace_id": task["trace_id"]})
    audit = (tmp_path / "plugin-sandbox-audit.jsonl").read_text(encoding="utf-8")
    assert [json.loads(line) for line in audit.splitlines()] == expected
    assert str(outside) not in audit and "example.invalid" not in audit


@pytest.fixture
def trusted_git_source(monkeypatch, tmp_path):
    def git(*args):
        result = subprocess.run(["git", *args], capture_output=True, timeout=15)
        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")

    class GitHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.serve_git()

        def do_POST(self):
            self.serve_git()

        def serve_git(self):
            url = urlsplit(self.path)
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            completed = subprocess.run(["git", "http-backend"], input=body, capture_output=True, check=True, timeout=15, env={
                **os.environ, "GIT_PROJECT_ROOT": str(tmp_path), "GIT_HTTP_EXPORT_ALL": "1",
                "REQUEST_METHOD": self.command, "PATH_INFO": url.path, "QUERY_STRING": url.query,
                "CONTENT_TYPE": self.headers.get("Content-Type", ""), "CONTENT_LENGTH": str(len(body)),
                "REMOTE_ADDR": self.client_address[0],
                "HTTP_GIT_PROTOCOL": self.headers.get("Git-Protocol", ""),
            })
            headers, _, response = completed.stdout.partition(b"\r\n\r\n")
            self.send_response(200)
            for line in headers.decode("ascii").splitlines():
                name, value = line.split(":", 1)
                self.send_header(name, value.strip())
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

    def publish(directory):
        git("-C", str(directory), "init", "-q")
        git("-C", str(directory), "-c", "core.autocrlf=false", "add", ".")
        git("-C", str(directory), "-c", "user.name=UH Tests", "-c", "user.email=uh-tests@example.invalid", "commit", "-qm", "Signed fixture")
        git("clone", "--bare", str(directory), str(tmp_path / "fixture.git"))
        return f"git+https://localhost:{server.server_port}/fixture.git"

    key = generate_private_key(public_exponent=65537, key_size=2048)
    identity = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder().subject_name(identity).issuer_name(identity).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1)).add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    tls = tmp_path / "fixture.pem"
    tls.write_bytes(certificate.public_bytes(Encoding.PEM) + key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    ca = tmp_path / "fixture-ca.pem"
    ca.write_bytes(certificate.public_bytes(Encoding.PEM))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(tls)
    monkeypatch.setenv("UH_PLUGIN_GIT_HOSTS", "localhost")
    monkeypatch.setenv("GIT_SSL_CAINFO", str(ca))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    if os.name == "nt":
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "http.sslBackend")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "openssl")
    with ThreadingHTTPServer(("127.0.0.1", 0), GitHandler) as server:
        server.socket = context.wrap_socket(server.socket, server_side=True)
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        try:
            yield publish
        finally:
            server.shutdown()
            serving.join()


@pytest.mark.parametrize("kind", ["directory", "market", "git"])
def test_signed_install_sources_activate_and_execute(kind, request, runtime_plugins, monkeypatch, tmp_path, capsys):
    key, app_factory = runtime_plugins
    package = tmp_path / "brief-tool@1.0.0.uhpkg"
    _runtime_package(package, key)
    if kind == "market":
        monkeypatch.setenv("UH_PLUGIN_MARKET_ROOT", str(tmp_path))
        source = "market://brief-tool@1.0.0"
    else:
        directory = tmp_path / "source"
        with zipfile.ZipFile(package) as archive:
            archive.extractall(directory)
        source = str(directory) if kind == "directory" else request.getfixturevalue("trusted_git_source")(directory)

    if os.name != "nt":
        monkeypatch.setattr("kernel.cli.SandboxRunner", lambda: SandboxRunner(enforce=False))
    assert uh_cli(["plugin", "install", source]) == 0
    assert '"status": "active"' in capsys.readouterr().out
    registry = json.loads((tmp_path / "registry.json").read_text(encoding="utf-8"))
    assert registry["brief-tool"]["source"] == source
    assert registry["brief-tool"]["status"] == "active"
    app = app_factory()
    with TestClient(app) as client:
        record, = client.get("/api/v1/plugins").json()["data"]["plugins"]
        assert record["status"] == "active"
        assert not (Path(record["install_path"]) / ".git").exists()
        session = client.post("/api/v1/sessions").json()["data"]["session_id"]
        _, events = _tool_task(client, session, "brief_echo", {"text": kind})
        assert events[-1]["event"] == "task.finished"
        assert f"plugin:{kind}" in events[-1]["payload"]["final_result"]["content"]


def test_install_applies_validate_checks_before_extracting_or_launching(runtime_plugins, tmp_path):
    key, app_factory = runtime_plugins
    dangerous = tmp_path / "dangerous.uhpkg"
    _make_package(dangerous, signing_key=key, extra_entries={"main.py": b"eval('1 + 1')\n"})
    missing_allowlist = tmp_path / "invalid-permission.uhpkg"
    escaped_entry = tmp_path / "escaped-entry.uhpkg"
    _runtime_package(missing_allowlist, key)
    with zipfile.ZipFile(missing_allowlist) as archive:
        entries = {name: archive.read(name) for name in ("manifest.yaml", "main.py")}
    for package, valid_field, invalid_field in (
        (missing_allowlist, b"permissions: []", b"permissions: [network.outbound]"),
        (escaped_entry, b"entry: main.py", b"entry: ../outside.py"),
    ):
        _make_package(package, signing_key=key, extra_entries={
            **entries, "manifest.yaml": entries["manifest.yaml"].replace(valid_field, invalid_field),
        })
    app = app_factory()
    with TestClient(app) as client:
        for package, code, field in (
            (dangerous, "UH-KRN-2010", "violation"),
            (missing_allowlist, "UH-USR-1003", "field"),
            (escaped_entry, "UH-USR-1003", "field"),
        ):
            with pytest.raises(PluginError) as validation:
                app.state.plugin_driver.validate(package)
            response = client.post("/api/v1/plugins", json={"source": str(package)})
            assert response.status_code in (400, 403), response.text
            error = response.json()["error"]
            assert error["code"] == validation.value.code == code
            assert error["detail"][field] == validation.value.detail[field]
        assert app.state.plugin_driver.list() == []
        assert not (tmp_path / "plugins").exists()

