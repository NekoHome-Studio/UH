# UH! Desktop

桌面端使用 Tauri 2。`frontendDist` 和主窗口都指向 `http://127.0.0.1:8080/?desktop=1`。内核从 `ui/` 提供原样的 `index.html`、`state-machine.js`、`themes.js` 和页面脚本 `app.js`。安装包的运行资源也直接取自这些目录。桌面入口的 HTTP 响应附带 CSP，规则与 `tauri.conf.json` 一致。

## 构建与运行

Windows 需要 Rust 1.90 及以上、MSVC C++ Build Tools、WebView2 与 Node.js。其他系统的系统库要求见 [Tauri 构建前置条件](https://v2.tauri.app/start/prerequisites/)。运行内核还需要带项目依赖的 Python 3.11 及以上环境。从仓库根目录运行以下 PowerShell 命令，将 Python 和 npm 依赖限制在工作区。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
$env:UH_DESKTOP_PYTHON = (Resolve-Path .venv\Scripts\python.exe).Path
$env:UH_LLM_PROVIDER = 'mock'
cargo run --manifest-path desktop/src-tauri/Cargo.toml
```

生成 Windows 安装包时，在同一仓库根目录运行以下命令。

```powershell
$env:npm_config_cache = Join-Path (Get-Location).Path '.work\desktop-npm-cache'
Push-Location desktop
npm exec --yes --package=@tauri-apps/cli@2 -- tauri build --bundles nsis
Pop-Location
```

安装包输出到 `desktop/src-tauri/target/release/bundle/`。安装版使用包中的内核和 UI 源码，Python 解释器与依赖需提前准备。`UH_DESKTOP_PYTHON` 接受解释器的绝对路径。仓库存在 `.venv` 时自动优先使用它，其次查找 `PATH`。从其他目录启动时，可以通过绝对路径 `UH_DESKTOP_ROOT` 指定 checkout。仓库模式使用项目下的 `.uh/`，安装模式使用应用本地数据目录下的 `.uh/`。启动失败时查看对应的 `.uh/desktop-kernel.log`。

桌面优先连接带 `X-UH-Desktop: 1` 标记的同版本兼容内核，端口空闲时启动单进程 Uvicorn，绑定 `127.0.0.1:8080`。碰到其他服务占用端口时显示冲突。托盘「退出」只清理本桌面启动的进程。窗口关闭按钮隐藏至托盘。默认用 `Ctrl+Alt+U` 唤醒，可用环境变量 `UH_DESKTOP_SHORTCUT` 覆盖。快捷键冲突时仍可从托盘唤醒。

## 离线聊天

在 Ollama 中预先下载一个聊天模型。可参照 [Ollama 环境变量示例](ollama.env.example) 在启动桌面前设置以下变量。若桌面复用已有内核，请把变量设在该内核进程中并重启内核。

```powershell
$env:UH_LLM_BASE_URL = 'http://localhost:11434/v1'
$env:UH_LLM_PROVIDER = 'openai'
$env:UH_LLM_API_KEY = 'ollama'
$env:UH_LLM_MODEL = 'replace-with-local-model'
```

`ollama` 是官方兼容接口示例的占位值。内核向 `/v1/chat/completions` 发起流式请求，解析 SSE 并读取可选 usage 帧。回环端点直连，即使系统设置了 HTTP 代理。冷启动较慢的模型可通过 `UH_LLM_TIMEOUT_MS` 延长超时。 [Ollama OpenAI 兼容接口](https://docs.ollama.com/api/openai-compatibility)

无 Ollama 时，设置 `$env:UH_LLM_PROVIDER = 'mock'` 并重启内核。发送普通聊天文本，确认增量回复和 `task.finished`。自动化用例从共享页面入口创建会话、提交消息、订阅 WebSocket，并拦截出站 HTTP 传输。

## 双端验收

| Web 功能 | Desktop 使用的路径 |
| --- | --- |
| Token、会话和消息提交 | 共用页面、REST 和同源 Cookie |
| 流式聊天与重连回放 | 共用 WebSocket |
| 任务状态和降级提示 | 共用 `UHStateMachine` |
| 工具确认与拒绝 | 共用页面和确认 REST |
| 错误、取消和阶段回退 | 共用事件展示和 steps REST |
| 主题切换、CSS 变量和字体 | 共用 `themes.js` 与主题 REST |

共享聊天页面功能覆盖 6/6（100% 源码静态对照）。系统托盘、全局快捷键和通知属于桌面原生能力。通知消费当前桌面会话 WebSocket 中首次进入 `task.finished` 或 `task.failed` 终态的事件。托盘显示空闲、运行中或降级。

1. 用托盘「打开 Web UI」访问同一个 `127.0.0.1:8080`。如果启用了鉴权，在两个窗口各填入同一个 token。
2. 在该内核注册一个受信任的主题，例如仓库中的 `examples/plugins/official/theme-light`。在 Desktop 选择该主题，在浏览器检查主题选择框和 CSS 色值。焦点切换时会刷新，双方可见时正常网络条件下等待约 3 秒即可看到更新。
3. 在浏览器切回默认主题，检查 Desktop 同步。两端都向 `PUT /api/v1/themes/active` 写入同一注册表，并从 `GET /api/v1/themes` 重新读取活动主题。
4. 隐藏 Desktop，测试托盘与快捷键唤醒。在 Desktop 触发成功和失败任务，检查系统通知。Windows 应在安装包中验证系统通知的应用身份。最后在自启及预启动内核两种情况下分别通过托盘退出，检查 `8080` 是否按所有权释放。 [Tauri 通知平台说明](https://v2.tauri.app/plugin/notification/#supported-platforms)

可单独运行 `node scripts/verify-desktop.mjs` 检查源码复用和主题 REST。Tauri 编译、打包及系统托盘、快捷键、通知的实际操作均需要在具备原生工具链的机器上执行。
