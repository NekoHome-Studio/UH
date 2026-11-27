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


# --- 三轮对抗（子代理代码分析指出，DSH 实测确认并修复）---
@pytest.mark.parametrize("src,label", [
    (b"from builtins import eval\neval('1')\n", "from-builtins-eval"),
    (b"import builtins as b\nb.eval('1')\n", "builtins-alias"),
    (b"from builtins import exec\nexec('1')\n", "from-builtins-exec"),
    (b"import os\nos.system('id')\n\x00bad\n", "syntax-error"),
    (b"import os\nvars(os)['system']('id')\n", "vars-subscript"),
    (b"import os\nos.__dict__['system']('id')\n", "dict-subscript"),
])
def test_scan_rejects_round3_bypasses(tmp_path, src, label):
    from kernel.plugins import PluginError
    path, pub = _pkg(tmp_path, {"main.py": src}, name=label)
    with pytest.raises(PluginError) as ei:
        _driver(tmp_path, pub).validate(path)
    assert ei.value.detail.get("violation") == "static_scan.exec"


# --- 四轮对抗（v4-bypass 实测指出 g=getattr 赋值别名绕过）---
@pytest.mark.parametrize("src,label", [
    (b"import os\ng = getattr\ng(os,'system')('id')\n", "assign-getattr"),
    (b"import os\nf = os.system\nf('id')\n", "assign-system"),
    (b"e = eval\ne('1')\n", "assign-eval"),
    (b"import os\ngetattr(os,'sys'+'tem')('id')\n", "concat-attr"),
])
def test_scan_rejects_round4_alias_bypasses(tmp_path, src, label):
    from kernel.plugins import PluginError
    path, pub = _pkg(tmp_path, {"main.py": src}, name=label)
    with pytest.raises(PluginError) as ei:
        _driver(tmp_path, pub).validate(path)
    assert ei.value.detail.get("violation") == "static_scan.exec"


# --- 五轮对抗（DSH 自测 8 样本，3 新绕过：容器存函数/类属性/partial 实参）---
@pytest.mark.parametrize("src,label", [
    (b"import os\nd={'k':os.system}\nd['k']('id')\n", "dict-value"),
    (b"import os\nclass A:\n    m = os.system\nA().m('id')\n", "class-attr"),
    (b"import os, functools\nfunctools.partial(os.system,'id')()\n", "partial-arg"),
    (b"import os\nos . system('id')\n", "space-attr"),
    (b"import os\n(getattr)(os,'system')('id')\n", "paren-getattr"),
    (b"exec(\"import os; os.system('id')\")\n", "exec-string"),
    (b"import os\nglobals()['os'].system('id')\n", "globals-sub"),
])
def test_scan_rejects_round5_bypasses(tmp_path, src, label):
    from kernel.plugins import PluginError
    path, pub = _pkg(tmp_path, {"main.py": src}, name=label)
    with pytest.raises(PluginError) as ei:
        _driver(tmp_path, pub).validate(path)
    assert ei.value.detail.get("violation") == "static_scan.exec"


# --- 六轮（v5-bypass 残余面提示 + DSH 最新基线实测：仅实例属性赋值仍绕）---
@pytest.mark.parametrize("src,label", [
    (b"import os\nclass A:\n    pass\na=A()\na.m=os.system\na.m('id')\n", "instance-attr"),
    (b"import os, functools\nf = functools.partial(os.system,'id')\nf()\n", "assign-call"),
    (b"import os\nK='system'\ngetattr(os,K)('id')\n", "getattr-var"),
    (b"import os\nd={'k':os.system}\nk='k'\nd[k]('id')\n", "subscript-var"),
    (b"(lambda:0).__globals__['__builtins__']['__import__']('os').system('id')\n", "lambda-globals"),
])
def test_scan_rejects_round6_residual(tmp_path, src, label):
    from kernel.plugins import PluginError
    path, pub = _pkg(tmp_path, {"main.py": src}, name=label)
    with pytest.raises(PluginError) as ei:
        _driver(tmp_path, pub).validate(path)
    assert ei.value.detail.get("violation") == "static_scan.exec"


# --- 七轮：通用兜底（危险 callable 被引用即拒，覆盖返回/解包/迭代/动态类等一切传递路径）---
@pytest.mark.parametrize("src,label", [
    (b"import os\nclass A:\n    def f(self):\n        return os.system\nA().f()('id')\n", "method-return"),
    (b"import os\ndef g():\n    return os.system\ng()('id')\n", "func-return"),
    (b"import os, operator\noperator.attrgetter('system')(os)('id')\n", "attrgetter"),
    (b"import os\n(type('X',(),{'m':os.system}))().m('id')\n", "dynamic-class"),
    (b"import os\n[*[os.system]][0]('id')\n", "starred-unpack"),
    (b"import os\n{**{'m':os.system}}['m']('id')\n", "dict-unpack"),
    (b"import os\nnext(iter([os.system]))('id')\n", "iter-next"),
])
def test_scan_universal_fallback_rejects_references(tmp_path, src, label):
    from kernel.plugins import PluginError
    path, pub = _pkg(tmp_path, {"main.py": src}, name=label)
    with pytest.raises(PluginError) as ei:
        _driver(tmp_path, pub).validate(path)
    assert ei.value.detail.get("violation") == "static_scan.exec"


def test_scan_allows_benign_plugin(tmp_path):
    # 兜底不得误伤正常插件（无危险属性引用）
    path, pub = _pkg(tmp_path, {"main.py": b"import json\ndef run(args):\n    return json.dumps({'ok': True})\n"}, name="benign")
    info = _driver(tmp_path, pub).validate(path)
    assert info["status"] == "validated"
    assert not [f for f in info["static_scan"] if f["kind"] == "exec_danger"]


# --- 八轮：反射/内省兜底（动态 chr() 构造名无法静态判定，一律封反射路径）---
_CHR = "chr(115)+chr(121)+chr(115)+chr(116)+chr(101)+chr(109)"


@pytest.mark.parametrize("tpl,label", [
    ("import os\nos.__dict__[{c}]('id')\n", "dict-chr"),
    ("import os\nos.__getattribute__({c})('id')\n", "getattribute-chr"),
    ("import os\nvars(os).get({c})('id')\n", "vars-get-chr"),
    ("import os\nglobals()['os'].system('id')\n", "globals-ref"),
    ("import os\nsetattr(__builtins__,'x',os.system)\nx('id')\n", "setattr-ref"),
])
def test_scan_rejects_reflection_paths(tmp_path, tpl, label):
    from kernel.plugins import PluginError
    src = tpl.format(c=_CHR).encode()
    path, pub = _pkg(tmp_path, {"main.py": src}, name=label)
    with pytest.raises(PluginError) as ei:
        _driver(tmp_path, pub).validate(path)
    assert ei.value.detail.get("violation") == "static_scan.exec"


# --- 独立审查 R1 P2-1：尾名黑名单漏掉的具名危险调用族 ---
@pytest.mark.parametrize("src,label", [
    (b"import os\nos.spawnve(os.P_WAIT, '/bin/sh', ['sh','-c','id'], os.environ)\n", "spawnve"),
    (b"import os\nos.spawnvp(os.P_WAIT, 'id', ['id'])\n", "spawnvp"),
    (b"import os\nos.execvp('id', ['id'])\n", "execvp"),
    (b"import os\nos.execle('/bin/sh','sh','-c','id',{})\n", "execle"),
    (b"import os\nos.posix_spawn('/bin/id',['id'],os.environ)\n", "posix_spawn"),
    (b"import subprocess\nsubprocess.getoutput('id')\n", "getoutput"),
    (b"import subprocess\nsubprocess.getstatusoutput('id')\n", "getstatusoutput"),
    (b"import pty\npty.spawn(['/bin/id'])\n", "pty_spawn"),
])
def test_scan_rejects_r1_exec_family_bypasses(tmp_path, src, label):
    from kernel.plugins import PluginError
    path, pub = _pkg(tmp_path, {"main.py": src}, name="r1-" + label.replace("_", "-"))
    with pytest.raises(PluginError) as ei:
        _driver(tmp_path, pub).validate(path)
    assert ei.value.detail.get("violation") == "static_scan.exec"


def test_scan_still_allows_benign_after_r1_widening(tmp_path):
    """扩展黑名单后正常插件不得误伤。"""
    path, pub = _pkg(tmp_path, {"main.py": b"import json\ndef run(args):\n    return json.dumps({'ok': True})\n"}, name="r1-benign")
    info = _driver(tmp_path, pub).validate(path)
    assert info["status"] == "validated"
    assert not [f for f in info["static_scan"] if f["kind"] == "exec_danger"]


# --- 自审修正：常见变量名不得因新尾名而误伤 ---
@pytest.mark.parametrize("src", [
    b"getoutput = 'result'\nprint(getoutput)\n",
    b"getstatusoutput = 'x'\nprint(getstatusoutput)\n",
])
def test_scan_allows_common_variable_names_after_r1_widening(tmp_path, src):
    """getoutput/getstatusoutput 只在全限定调用匹配，普通变量名放行。"""
    path, pub = _pkg(tmp_path, {"main.py": src}, name="common-var")
    info = _driver(tmp_path, pub).validate(path)
    assert info["status"] == "validated"
    assert not [f for f in info["static_scan"] if f["kind"] == "exec_danger"]
