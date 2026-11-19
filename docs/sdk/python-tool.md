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

