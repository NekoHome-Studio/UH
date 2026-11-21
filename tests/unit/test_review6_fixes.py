"""二轮复检（DeepSeek F1/F2/F3/F9）安全与 SDK 修复回归。"""
import json

import pytest

from kernel.plugins import PluginManifest
from kernel.sandbox import SandboxRunner, SandboxError


def _manifest(type_: str = "tool") -> PluginManifest:
    return PluginManifest.from_dict({
        "name": "t", "version": "1.0.0", "type": type_, "description": "x",
        "author": "a", "license": "MIT", "entry": "main.py", "permissions": [],
        "risk_level": "read-only", "config": {"timeout_ms": 1000, "memory_limit_mb": 16},
    })


# --- F1：env 白名单 passthrough，任何 UH_* 都不进 tool 子进程 ---
def test_tool_env_whitelist_blocks_all_uh_vars(tmp_path, monkeypatch):
    for key in ("UH_AUTH_TOKEN", "UH_AUTH_TOKEN_FILE", "UH_CLIENT_ID_SECRET", "UH_CLIENT_ID_SECRET_FILE",
                "UH_MEMORY_MASTER_KEY", "UH_MEMORY_EXPORT_TOKEN", "UH_CHECKPOINT_POSTGRES_URL",
                "UH_LLM_API_KEY", "UH_REDIS_URL", "UH_PLUGIN_TRUSTED_KEYS_FILE", "UH_FUTURE_SECRET"):
        monkeypatch.setenv(key, "sentinel-" + key)
    runner = SandboxRunner(enforce=False, root=tmp_path)
    plan = runner.build_plan(_manifest(), tmp_path)
    leaked = [k for k in plan.env if k.startswith("UH_") and not k.startswith("UH_SANDBOX_")]
    assert leaked == [], f"凭据泄漏: {leaked}"


def test_hook_env_whitelist_blocks_all_uh_vars(tmp_path, monkeypatch):
    monkeypatch.setenv("UH_MEMORY_MASTER_KEY", "sentinel")
    runner = SandboxRunner(enforce=False, root=tmp_path)
    plan = runner.build_plan(_manifest("hook"), tmp_path)
    assert not [k for k in plan.env if k.startswith("UH_") and not k.startswith("UH_SANDBOX_")]


# --- F2：fs.host.read 不得读凭据/运行态 ---
@pytest.mark.parametrize("path", [
    "D:/uh/.auth-token", "/home/u/.ssh/id_ed25519", "/srv/app/.env",
    "C:/secrets/api-token.txt", "D:/uh/.uh/long_term_memory.json", "/etc/trusted_keys.json",
])
def test_host_read_denies_sensitive_paths(tmp_path, path):
    runner = SandboxRunner(enforce=False, root=tmp_path)
    policy = runner.policy(PluginManifest.from_dict({
        "name": "t", "version": "1.0.0", "type": "tool", "description": "x", "author": "a",
        "license": "MIT", "entry": "main.py", "permissions": ["fs.host.read"],
        "risk_level": "read-only", "config": {"timeout_ms": 1000, "memory_limit_mb": 16},
    }), tmp_path)
    with pytest.raises(SandboxError) as ei:
        policy.check_path(path)
    assert ei.value.code == "UH-KRN-2010"


def test_host_read_allows_ordinary_path(tmp_path):
    target = tmp_path / "notes.txt"
    target.write_text("ok", encoding="utf-8")
    runner = SandboxRunner(enforce=False, root=tmp_path / "sandbox")
    policy = runner.policy(PluginManifest.from_dict({
        "name": "t", "version": "1.0.0", "type": "tool", "description": "x", "author": "a",
        "license": "MIT", "entry": "main.py", "permissions": ["fs.host.read"],
        "risk_level": "read-only", "config": {"timeout_ms": 1000, "memory_limit_mb": 16},
    }), tmp_path / "sandbox")
    assert policy.check_path(str(target)).name == "notes.txt"


# --- F9：结构化返回序列化为 JSON（非 Python repr） ---
def test_sdk_dict_result_is_json():
    import asyncio
    from kernel.plugin_sdk import ToolPlugin

    plugin = ToolPlugin(_manifest(), package_dir=".")

    @plugin.tool("echo", description="echo")
    def echo(args, ctx):
        return {"greeting": "hello " + args.get("name", "uh")}

    content = asyncio.run(plugin.call_tool("echo", {"name": "chain"}))
    text = content[0]["text"]
    assert json.loads(text) == {"greeting": "hello chain"}  # 单引号 repr 会在此失败


# --- F3 关联：handler 缺参数不误报 unknown tool ---
def test_sdk_missing_arg_not_unknown_tool():
    import asyncio
    from kernel.plugin_sdk import ToolPlugin, MCPPluginServer, UnknownToolError

    plugin = ToolPlugin(_manifest(), package_dir=".")

    @plugin.tool("need", description="need path")
    def need(args, ctx):
        return args["path"]

    server = MCPPluginServer(plugin)
    resp = asyncio.run(server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "need", "arguments": {}}}))
    assert "error" not in resp and resp["result"]["isError"] is True
    assert "missing required argument" in resp["result"]["content"][0]["text"]
    with pytest.raises(UnknownToolError):
        asyncio.run(plugin.call_tool("nope", {}))


# --- F5/F6/F7/F8：静态扫描覆盖与解压体积 ---
def _pkg(tmp_path, entries: dict, name: str = "scan"):
    import base64, json as _json, zipfile
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from kernel.plugins import _canonical_digest, SIGNATURE_FILE
    manifest = (
        f"name: {name}\nversion: 1.0.0\ntype: tool\ndescription: x\nauthor: a\nlicense: MIT\n"
        "entry: main.py\npermissions: []\nrisk_level: read-only\nconfig:\n  timeout_ms: 1000\n  memory_limit_mb: 16\n"
    ).encode()
    all_entries = {"manifest.yaml": manifest, **entries}
    digest = _canonical_digest(sorted(all_entries.items()))
    priv = Ed25519PrivateKey.generate()
    pub = base64.b64encode(priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    sig = base64.b64encode(priv.sign(bytes.fromhex(digest))).decode()  # 签摘要字节，非 hex 字符串
    path = tmp_path / f"{name}.uhpkg"
    with zipfile.ZipFile(path, "w") as z:
        for n, c in all_entries.items():
            z.writestr(n, c)
        z.writestr(SIGNATURE_FILE, _json.dumps({"algorithm": "ed25519", "key_id": "k", "digest": digest, "signature": sig}))
    return path, pub


def _driver(tmp_path, pub):
    from kernel.plugins import PluginDriver, PluginSecurityConfig
    return PluginDriver(root=tmp_path / "p", registry_path=tmp_path / "p.json", security=PluginSecurityConfig(trusted_keys={"k": pub}))


def test_scan_rejects_non_utf8_source(tmp_path):
    # F5：latin-1 coding cookie 混淆不得静默跳过
    from kernel.plugins import PluginError
    src = b"# -*- coding: latin-1 -*-\nimport os\nos.system('id')\n# \xe9\n"
    path, pub = _pkg(tmp_path, {"main.py": src}, name="enc")
    with pytest.raises(PluginError) as ei:
        _driver(tmp_path, pub).validate(path)
    assert ei.value.detail.get("violation") == "static_scan.exec"


@pytest.mark.parametrize("entry_name", ["payload.exe", "run.ps1", "go.sh", "x.bat"])
def test_scan_rejects_native_script_entry(tmp_path, entry_name):
    # F6：原生二进制/脚本入口不可静态验证 → 拒绝
    from kernel.plugins import PluginError
    path, pub = _pkg(tmp_path, {"main.py": b"print(1)\n", entry_name: b"MZ\x90\x00"}, name="nat")
    with pytest.raises(PluginError) as ei:
        _driver(tmp_path, pub).validate(path)
    assert ei.value.detail.get("violation") == "static_scan.exec"


def test_scan_flags_from_urllib_import_request(tmp_path):
    # F7：from urllib import request 也算出网导入
    path, pub = _pkg(tmp_path, {"main.py": b"from urllib import request\nprint(1)\n"}, name="net2")
    info = _driver(tmp_path, pub).validate(path)
    assert any(f["kind"] == "network" for f in info["static_scan"])


def test_zip_bomb_precheck_rejects_oversized(tmp_path):
    # F8：解压前按 file_size 预检，防 zip bomb
    import zipfile
    from kernel.plugins import PluginError
    path, pub = _pkg(tmp_path, {"main.py": b"print(1)\n"}, name="bomb")
    with zipfile.ZipFile(path, "a") as z:
        zi = zipfile.ZipInfo("huge.bin")
        zi.file_size = 4 * 1024 * 1024 * 1024  # 声明 4GiB
        z.writestr(zi, b"")
    with pytest.raises(PluginError):
        _driver(tmp_path, pub).validate(path)
