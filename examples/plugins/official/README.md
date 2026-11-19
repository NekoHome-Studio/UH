# 官方工具插件模板

三个模板都使用 `kernel.plugin_sdk.ToolPlugin` 和 MCP stdio。

```powershell
$env:UH_LOCAL_MODE = "1"
$env:UH_PLUGIN_ALLOW_UNVERIFIED = "1"
uh plugin validate .\examples\plugins\official\web-search --no-verify
uh plugin validate .\examples\plugins\official\file --no-verify
uh plugin validate .\examples\plugins\official\db-query --no-verify
```

开发运行时可直接从模板目录执行 `python main.py`，再用 MCP Host 连接 stdin/stdout。正式安装时需要打包为 `.uhpkg` 并通过受信签名校验。
