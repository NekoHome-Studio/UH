# UH! Python 工具插件最小模板

插件进程使用 MCP JSON-RPC over stdio。清单中的 `entry` 指向 Python 文件，工具插件可以复用 `kernel.plugin_sdk.ToolPlugin`。

```python
from kernel.plugin_sdk import ToolPlugin, run_stdio
from kernel.plugins import PluginManifest

manifest = PluginManifest(
    name="echo-tool",
    version="1.0.0",
    type="tool",
    description="Echo",
    author="example",
    license="MIT",
    permissions=("fs.sandbox.read",),
    entry="main.py",
    config={"timeout_ms": 5000, "memory_limit_mb": 256},
    risk_level="read-only",
    raw={},
)
plugin = ToolPlugin(manifest, package_dir=".")

@plugin.tool("echo", description="Echo input", input_schema={"type": "object"})
def echo(arguments, context):
    return arguments.get("text", "")

run_stdio(plugin)
```

SDK 已提供 `tools/list`、`tools/call`、异步 handler、沙箱目录读写和出站 host 校验。正式插件包仍需按照 M2-1 的 `.uhpkg`、manifest 和签名规则打包。


## 完整上手（第三方自助，目标 ≤30 分钟）

### 1. 目录结构
```
my-tool/
├── manifest.yaml      # 必需，插件清单
└── main.py            # entry 指向的入口
```

### 2. manifest.yaml 模板（必需文件，validate 校验它）
```yaml
name: my-tool
version: 1.0.0
type: tool
description: My first UH tool
author: you
license: MIT
entry: main.py
risk_level: read-only          # read-only / write-host / irreversible
permissions:                   # 沙箱权限声明（未声明则拒绝）
  - fs.sandbox.read
  - fs.sandbox.write
  # - network.outbound         # 需出网时声明，并配下方 network 节点
config:                        # configBase：仅 timeout_ms/memory_limit_mb/rate_limit，禁 allow_hosts
  timeout_ms: 5000
  memory_limit_mb: 64
# network:                     # 出网白名单是顶层 network 节点（非 config）；allow_hosts 必填且 ≥1 项
#   allow_hosts:
#     - example.com            # 子域用 *.example.com
```

### 3. main.py（复用 SDK）
```python
from kernel.plugin_sdk import ToolPlugin, run_stdio
from kernel.plugins import PluginManifest
import yaml, pathlib

# 从 manifest.yaml 加载清单（与打包一致）
_raw = yaml.safe_load(pathlib.Path(__file__).with_name("manifest.yaml").read_text(encoding="utf-8"))
manifest = PluginManifest.from_dict(_raw)
plugin = ToolPlugin(manifest, package_dir=str(pathlib.Path(__file__).parent))

@plugin.tool("hello", description="Say hello", input_schema={"type": "object", "properties": {"name": {"type": "string"}}})
def hello(arguments, context):
    return {"greeting": "hello " + arguments.get("name", "uh")}

run_stdio(plugin)
```

### 4. 本地验证与开发模式
```bash
# 开发态跳过签名（须先设环境变量，否则 validate 拒未签名包）
export UH_LOCAL_MODE=1 UH_PLUGIN_ALLOW_UNVERIFIED=1   # Windows: set UH_LOCAL_MODE=1 && set UH_PLUGIN_ALLOW_UNVERIFIED=1
uh plugin validate ./my-tool --no-verify             # 期望 status=validated（含 static_scan 报告）
uh plugin dev ./my-tool --once                       # 灌入 initialize→tools/list→tools/call，期望 JSON-RPC 结果
```
- `validate` 现含 **SBX-02 静态扫描**：危险 exec（os.system/subprocess/eval/exec/ctypes）拒绝；出网导入（socket/requests/httpx 等）告警，需 `allow_hosts` 声明。
- `dev --once` 完成实际 MCP 握手与工具枚举；`dev`（不带 --once）支持热重载（改 main.py 子进程重启）。

### 5. 打包与签名（生产安装）
生产安装须签名 `.uhpkg`（zip）。签名算法：
1. 规范化摘要：对除 `SIGNATURE` 外的所有条目按文件名排序，`sha256( len(name)4字节大端 + name + len(content)8字节大端 + content ... )`（见 `kernel/plugins.py::_canonical_digest`，注意内容长度是 **8 字节**）。
2. Ed25519 over digest：`SIGNATURE` 文件为 JSON `{algorithm: ed25519, key_id, digest(规范化摘要hex), signature(base64)}`（四字段均必填字符串，见 `_read_signature`；公钥放信任库不放 SIGNATURE）。
3. 信任库：服务端 `UH_PLUGIN_TRUSTED_KEYS_FILE` 指向 `{key_id: public_key_base64}` JSON；未受信密钥拒装（SBX-06）。

> 注：当前无 `uh plugin pack/sign` 子命令，签名包需按上述算法自行构造（参考 tests/unit/test_plugins.py::_make_package）。pack/sign CLI 列入 M2 后续。

### 6. 权限参考
| 权限 | 作用 |
|---|---|
| fs.sandbox.read / fs.sandbox.write | 沙箱目录内读/写（越界拒绝 UH-KRN-2010） |
| network.outbound + network.allow_hosts | 出网白名单（顶层 network 节点，非 config；子域 *.example.com） |
| tool.invoke | 调用其他工具（经内核 RPC） |

### 已知局限
- 生产态签名工具子进程需能导入 kernel（PYTHONPATH 含仓库根）；生产能力目录调用闭环与启动重激活见 M2-1 后续。
- web-search 官方示例返回 proxy_required（真实搜索代理待接通）。
