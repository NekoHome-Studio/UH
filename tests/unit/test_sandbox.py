"""M2-2 沙箱策略与运行计划。"""
import pytest
import asyncio

from kernel.plugins import PluginManifest
from kernel.sandbox import ProcessPluginLoader, SandboxError, SandboxRunner
from kernel.main import create_app


def _manifest(*, permissions=(), hosts=()):
    network = {"allow_hosts": list(hosts)} if hosts else None
    return PluginManifest(
        name="sandbox-test",
        version="1.0.0",
        type="tool",
        description="test",
        author="test",
        license="MIT",
        permissions=tuple(permissions),
        entry="main.py",
        config={"timeout_ms": 1000, "memory_limit_mb": 32},
        network=network,
        risk_level="read-only",
        raw={},
    )


def test_policy_rejects_undeclared_filesystem_and_network_access(tmp_path):
    policy = SandboxRunner(root=tmp_path).policy(_manifest(), tmp_path / "sandbox-test")
    with pytest.raises(SandboxError) as file_error:
        policy.check_path(tmp_path / "outside.txt", write=True)
    assert file_error.value.code == "UH-KRN-2010"
    with pytest.raises(SandboxError) as network_error:
        policy.check_host("https://example.com")
    assert network_error.value.code == "UH-KRN-2010"


def test_policy_enforces_declared_permissions_and_hosts(tmp_path):
    plugin_dir = tmp_path / "sandbox-test"
    plugin_dir.mkdir()
    policy = SandboxRunner(root=tmp_path).policy(
        _manifest(permissions=("fs.sandbox.read", "fs.sandbox.write", "network.outbound"), hosts=("api.example.com", "*.trusted.test")),
        plugin_dir,
    )
    assert policy.check_path(plugin_dir / "data.txt", write=True) == (plugin_dir / "data.txt").resolve()
    assert policy.check_host("https://api.example.com/v1") == "api.example.com"
    assert policy.check_host("worker.trusted.test") == "worker.trusted.test"
    with pytest.raises(SandboxError):
        policy.check_host("evil.example.com")


def test_build_plan_contains_runtime_limits_and_network_policy(tmp_path):
    plugin_dir = tmp_path / "sandbox-test"
    plugin_dir.mkdir()
    (plugin_dir / "main.py").write_text("print('ok')\n", encoding="utf-8")
    runner = SandboxRunner(enforce=False, root=tmp_path)
    plan = runner.build_plan(_manifest(permissions=("fs.sandbox.read",), hosts=()), plugin_dir)
    assert plan.cwd == plugin_dir.resolve()
    assert plan.env["UH_SANDBOX_NETWORK"] == "deny"
    assert plan.env["UH_SANDBOX_TIMEOUT_MS"] == "1000"
    assert plan.env["UH_SANDBOX_MEMORY_LIMIT_MB"] == "32"


def test_process_plugin_loader_starts_and_stops_isolated_process(tmp_path):
    plugin_dir = tmp_path / "sandbox-test"
    plugin_dir.mkdir()
    (plugin_dir / "main.py").write_text("import time; time.sleep(30)\n", encoding="utf-8")
    manifest = _manifest(permissions=("fs.sandbox.read",))
    loader = ProcessPluginLoader(SandboxRunner(enforce=False, root=tmp_path))

    async def exercise():
        await loader.load(manifest, plugin_dir)
        await loader.activate(manifest, plugin_dir)
        process = loader.processes[manifest.name]
        assert process.process.poll() is None
        await loader.deactivate(manifest, plugin_dir)
        assert process.process.poll() is not None

    asyncio.run(exercise())


def test_kernel_plugin_driver_uses_process_loader():
    app = create_app()
    assert isinstance(app.state.plugin_driver.loaders["tool"], ProcessPluginLoader)
    assert isinstance(app.state.plugin_driver.loaders["capability"], ProcessPluginLoader)
    assert isinstance(app.state.plugin_driver.loaders["hook"], ProcessPluginLoader)

