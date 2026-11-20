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

安装来源支持三种形式：

```powershell
uh plugin install .\web-search.uhpkg
uh plugin install market://web-search@0.1.0
uh plugin install git+https://github.com/example/uh-web-search.git
```

`market://` 默认从 `.uh/market` 查找本地镜像，也可由 `UH_PLUGIN_MARKET_ROOT` 覆盖。Git 来源默认只允许 `github.com`，可通过 `UH_PLUGIN_GIT_HOSTS` 扩展白名单。
