# M2 插件期独立验收报告（第三方 · 跨机 · MS-3 结项口径）

2026-11-20 · 独立验收审查者（非实现方 DSH / Codex） · 基线 `067e7a2`（工作树 HEAD `9da129f`，仅多一个 brief 提交）
本机 `192.168.0.14`（Windows） · 服务器 `192.168.0.28:8080`（`ssh ipc`，`D:\uh`）
审查方式：只读代码 / 契约 / 运行时；未修改受版本控制源码、契约与服务器配置；未阅读其他审查者的 M2 验收材料。

签署模型：**Claude-Sonnet-4.5**（Anthropic，经 DSH 执行）

---

## 0. 结论摘要（先给判定）

| 项 | 判定 |
|---|---|
| **A 门禁复现** | 本机 **通过**（pytest 195/195、verify-all full 8/8、secret-scan 0、git diff --check 0）；服务器 **未通过**（147 passed / 1 failed，且**无 M2 模块**） |
| **B 六任务** | M2-3、M2-4 **通过（本机口径）**；M2-1、M2-2、M2-5、M2-6 **部分通过** |
| **C 跨机插件链路** | **未通过 / 无法执行**：服务器运行的仍是 M1 期内核（`POST /api/v1/plugins` → 405，`GET /api/v1/themes` → 404，CLI 无 `plugin` 子命令） |
| **D 安全专项** | **通过（本机口径）**：签名/篡改/不受信密钥拒装、主题 JS 注入矩阵全拒、沙箱越权 KRN-2010、钩子异常隔离、secret 扫描无明文 |
| **总体** | **不通过**（1 个 P0 阻断：声明的「服务器 195/195」与实际部署不符，跨机链路无法验收） |

阻断项与复验清单见 §7；探针副作用见 §8。

---

## 1. 门禁复现（亲测）与部署一致性

### 1.1 本机门禁（全部亲测）

| 门禁 | 命令 | 结果 |
|---|---|---|
| 单测 | `python -m pytest -q` | **195 passed**，256 warnings，59.47s（Python 3.14.3） |
| 契约/交叉校验 | `node scripts/verify-all.mjs --full` | **VERIFY_ALL_PASSED (8 checks, full)**：A manifest schema、B events、B2 运行时映射、C 交叉一致性、D OpenAPI 19/19、E v3 基线待修订 INFO×5、F swagger-cli 3.1 valid、G AsyncAPI 2.6 errors=0 |
| 密钥扫描 | `node scripts/secret-scan.mjs` | 无输出，rc=0（无明文密钥命中） |
| 空白检查 | `git diff --check` | rc=0 |
| 工作树 | `git status --short` | 仅未跟踪的报告文件；无源码改动 |

补充：`node scripts/review-precheck.mjs` 直接执行报 `MODULE_NOT_FOUND`（依赖未安装），但作为 verify-all 的 C 项可正常通过 —— 见 F-8。

### 1.2 服务器门禁（亲测，与声明不符）

```
ssh ipc "cd /d D:\uh && .venv\Scripts\python.exe -m pytest -q"
→ 1 failed, 147 passed, 105 warnings in 23.66s
  FAILED tests/unit/test_websocket.py::test_websocket_bearer_token_is_sufficient_owner_identity
```

声明「195/195（本机+服务器）」**不成立**：服务器测试集只有 148 项（缺 `test_plugins / test_sandbox / test_hooks / test_themes / test_plugin_sdk / test_official_plugins / test_review5_fixes` 七个 M2 测试文件）。

### 1.3 部署一致性（内核文件 SHA-256 双端对比）

`kernel/*.py` 共 21 个文件：**SAME 12 / DIFF 4 / MISSING_ON_SERVER 5**。

| 文件 | 状态 | local(12) | server(12) |
|---|---|---|---|
| kernel/__init__.py | SAME | 85dea3914816 | 85dea3914816 |
| kernel/cli.py | **DIFF** | 363b1159de4b | e86b3e815d70 |
| kernel/confirmation.py | SAME | c11c35afb59e | c11c35afb59e |
| kernel/event_mapping.py | SAME | dd41662028b7 | dd41662028b7 |
| **kernel/hooks.py** | **MISSING_ON_SERVER** | 33ee70748864 | — |
| kernel/llm.py | SAME | d301f58a9ad1 | d301f58a9ad1 |
| kernel/logging.py | SAME | 6590a5b6b2bd | 6590a5b6b2bd |
| kernel/long_term_memory.py | SAME | 9934100e2373 | 9934100e2373 |
| **kernel/main.py** | **DIFF** | 4a99fc8c3e6a | eb727b9f5b20 |
| kernel/mcp.py | SAME | 7c7ece415186 | 7c7ece415186 |
| kernel/memory.py | SAME | 8ba8baa573ec | 8ba8baa573ec |
| kernel/orchestration.py | SAME | 131ebfc00ceb | 131ebfc00ceb |
| **kernel/plugin_sdk.py** | **MISSING_ON_SERVER** | d75c8014144b | — |
| **kernel/plugins.py** | **MISSING_ON_SERVER** | bf62e0e24cf4 | — |
| **kernel/protocol.py** | **DIFF** | 7a30f83fa93a | 62a10e09d681 |
| kernel/retrieval.py | SAME | 3fa7871192ba | 3fa7871192ba |
| kernel/router.py | SAME | 051e9fb8a6e9 | 051e9fb8a6e9 |
| **kernel/sandbox.py** | **MISSING_ON_SERVER** | 9cc656b931e5 | — |
| **kernel/tasks.py** | **DIFF** | 42f2afb46ccd | 013426942e18 |
| **kernel/themes.py** | **MISSING_ON_SERVER** | 7f8f269a5619 | — |
| kernel/tracing.py | SAME | 8acf22955d8b | 8acf22955d8b |

旁证：
```
ssh ipc "... -c \"import kernel.plugins\"" → ModuleNotFoundError: No module named 'kernel.plugins'
ssh ipc "... -m kernel.cli plugin list"   → uh: error: argument cmd: invalid choice: 'plugin' (choose from dev, memory, confirm)
dir D:\uh\ui                                → index.html、state-machine.js（无 themes.js）
```
结论：**服务器停留在 M1 部署**，M2 五模块缺失、四个核心文件版本落后；`D:\uh\docs\contracts\` 与 `D:\uh\tests\`（M1 子集）为较早同步。这与 brief 的基线声明冲突，直接否决 C 段。

服务进程身份核对（排除"另有一套 M2 部署在跑"的可能）：`D:\` 下仅 `D:\uh` 一个项目目录；8080 监听 PID 为 **7752**（`D:\Python312\python.exe -m uvicorn kernel.main:app --host 0.0.0.0 --port 8080`），其父进程 5744 为 `D:\uh\.venv\Scripts\python.exe` 启动器 —— 即实际提供 8080 服务的代码树就是上述 M1 版 `D:\uh`。

---

## 2. 六任务逐项判定

判定口径：以**生产链路可观测**为准；仅当本机运行时能复现且与契约一致才记「通过」。fixture 全绿不作为通过依据。

### 2.1 M2-1（#22）插件驱动 —— **部分通过**

| 验收点 | 证据（亲测） | 判定 |
|---|---|---|
| 签名不符拒装（SBX-06） | 未签名包 → `UH-KRN-2010 signature_missing`；签名后改 `main.py` → `UH-KRN-2010 sha256`；换私钥但沿用 `key_id` → `UH-KRN-2010 signature` | 通过 |
| 激活失败幂等回滚 | 入口为不可执行 `boom.bin` → `UH-KRN-2010 activate`，128ms 内在注册表 0 条、安装目录不存在、`.staging` 无残留 | 通过 |
| deactivate 5s 超时强杀 | 注入 30s 慢 deactivate → `deactivate` 耗时 **5.01s**，状态 `quarantined`，存活 PID 列表为空 | 通过 |
| 装/卸无需重启内核 | 运行中的内核（127.0.0.1:8099）REST 安装/卸载 echo-tool、theme、hook 均即时生效，`/ready` 前后 200 | 通过 |
| 卸载后无残留（E2E-05） | 卸载后安装目录不存在、无存活子进程、钩子订阅名 `["audit-acc"] → []`、审计文件随目录删除 | 通过 |
| 运行中签名 Hook 装卸 | 见 M2-5 | 通过 |
| 审计 / 订阅清理 | `plugin.install / plugin.uninstall / plugin.crash / plugin.quarantined` 事件落日志；`HookManager.names` 卸载后为空 | 通过 |
| **已安装工具从生产能力目录调用闭环** | 安装到 `.work/.../plugins/echo-tool/1.0.0` 后，从**安装目录**拉起 MCP 子进程：`initialize` → `tools/list`（1 个工具）→ `tools/call` 返回 `isError:false`，握手 921.9ms，退出码 0 | 通过（补项达成） |
| **内核启动后插件重激活** | 同一注册表新建驱动后：`active → installed`、`processes_after_boot=0`、**无 reactivate/activate API**、startup 无重激活钩子 | **未达成**（F-2） |
| CLI 安装工具插件 | 4 次 `uh plugin install market://…` 中 2 次 60–90s 不返回，注册表停在 `installed` | **缺陷**（F-3） |

### 2.2 M2-2（#23）最小沙箱 —— **部分通过**

| 验收点 | 证据 | 判定 |
|---|---|---|
| SDK 权限检查（SBX-01/02 行为级） | 插件内 `context.read_text('C:/Windows/win.ini')` → `UH-KRN-2010 {violation: filesystem}`；`context.allow_host('https://example.com/')` → `UH-KRN-2010 {violation: network.outbound}` | 通过 |
| 越权返回 KRN-2010 + **审计** | 错误码正确；内核日志中**无**对应沙箱拒绝审计事件（全仓无 `plugin.sandbox.*` 事件） | **审计缺失**（F-4） |
| 进程启动 / 超时回收（SBX-03） | `timeout_ms=800` + `sleep(30)` → `UH-CAP-5021`，0.87s 返回，子进程已回收 | 通过 |
| 实机内存限额（SBX-04） | `memory_limit_mb=64` + 分配 512MB → 子进程 `MemoryError`，rc=1，0.24s 结束，内核 `/ready` 仍 200 | 通过（Job Object 进程内存上限生效） |
| 连续崩溃 3 次隔离（SBX-05） | 立即 `sys.exit(3)` 的插件：`plugin.crash ×2 → plugin.quarantined`，1.4s 内 `crash_count=3, status=quarantined` | 通过 |
| SBX 平台矩阵（Linux / macOS） | 本机为 Windows，服务器为 Win10 工控机；**无 Linux/macOS 主机**，`bwrap` / `sandbox-exec` 分支无法执行 | **未取证**（见 §2.7 矩阵表） |

### 2.3 M2-3（#24）工具 SDK —— **通过（附文档缺口）**

| 验收点 | 证据 | 判定 |
|---|---|---|
| 三个官方工具 `uh plugin validate` | `web-search` / `file` / `db-query` 均 rc=0、`status: validated`，权限与 `risk_level` 与清单一致（约 2.8–3.3s/次） | 通过 |
| `uh plugin dev --once` MCP 握手 + 工具枚举 | 灌入 3 条 JSON-RPC：`initialize`（protocolVersion 2024-11-05）→ `tools/list`（echo，含 annotations）→ `tools/call` 返回 `dev-once`，rc=0，2.65s | 通过 |
| 模板可直接 `plugin dev` 热重载 | 改写 `main.py` 后自动重启：捕获到 `BOOT-A → BOOT-B`（3s + 4s 窗口） | 通过 |
| 签名工具注册与调用 | 签名包安装 → 从安装目录 MCP 调用成功（见 M2-1） | 通过 |
| 第三方 30 分钟上手 | 实测 11s（21:05:49→21:06:00），但**仅依 `docs/sdk/python-tool.md` 第一步即失败**（缺 `manifest.yaml`），必须转读 `docs/contracts/manifest` | 见 §3 / F-6 |
| `web-search` 现状 | 权限 `network.outbound` + `allow_hosts: api.duckduckgo.com`；真实调用仍 `proxy_required`（已知局限，登记） | 登记 |

### 2.4 M2-4（#25）主题插件 —— **通过（本机口径）**

| 验收点 | 证据 | 判定 |
|---|---|---|
| 签名包安装 | `theme-acc.uhpkg`（sig by Ed25519，trust store）→ 201 `active`，96ms | 通过 |
| 切换 <200ms（E2E-06） | **真实浏览器**（ego-browser，Chromium）：`change` 事件 → 计算样式首次变化 **14.2 / 16.8 / 19.2 / 22.6 / 26.7 ms**，settled **19.3–30.9 ms**；服务端 `PUT /api/v1/themes/active` 5 次 **13.0 / 13.4 / 13.4 / 14.1 / 15.0 ms** | 通过（远低于 200ms） |
| 非法 CSS 拒绝 | `url(javascript:alert(1))`、`expression(alert(1))`、`<script>`、`data:` URI、`@import`、`var(--x)`、反斜杠转义 → **全部 REJECTED**；`#ffffff` / `16px` → ACCEPTED | 通过 |
| 非法资产拒绝 | 附加 `assets/extra.svg`（`<svg onload=…>`）→ 400「只允许包内 PNG 预览与 font_stack 对应 WOFF2」；`evil.js` 同理；路径穿越 `assets/fonts/../../..` → 404 | 通过 |
| WOFF2 渲染样本 | 用 `fontTools` 从 `segoeui.ttf` 生成真实 WOFF2（12,344 B）→ `GET …/UHAcc.woff2` 200 `font/woff2` 且 `magic=wOF2`；页面 `document.fonts` 中 `UHAcc: loaded`，`body` computed `font-family: UHAcc, system-ui, sans-serif` | 通过 |
| 刷新与内核重建恢复 | 选择写入 `plugins.theme.json`（`"theme-acc"`）；新建驱动读取注册表后 `theme_catalog.active == theme-acc`，变量与字体路径完整 | 通过 |
| Desktop 同步 | 按 M3-6 登记，不阻断 | 登记 |

### 2.5 M2-5（#26）审计 Hook —— **部分通过**

| 验收点 | 证据 | 判定 |
|---|---|---|
| 生产消息入口路由事件 | 签名 `audit-acc` 钩子 + `POST /api/v1/sessions` → `POST /…/messages`（202）→ `audit.jsonl` 落 1 行 | 通过 |
| trace_id / span_id / event_id 对照 | 审计行 `action=kernel.route.decided`，`trace_id` 与任务 `trace_id` **完全一致**（`uh_trc_dc9bc5d6…`），`span_id=sp_60502dd1`，`event_id=2014`，`parent_span_id=null`，`envelope_version=1` | 通过 |
| 元数据最小化 | payload 仅 `session_id / mode / task_type / rule / confidence / fallback / task_id`；消息正文「审计字段对照：验收探针」**未泄漏** | 通过 |
| 慢执行 / 异常 / 卸载 / 超时回收 | 异常钩子（`sys.exit(7)`）：总线发布 **0.3–0.4ms** 返回，主链路不阻塞，`HookManager.names` 卸载后为空；超时回收见 SBX-03 | 通过（本机） |
| 跨平台进程行为 | 仅 Windows 可执行 | **未取证** |
| 服务重启后 Hook 恢复 | 重启后 `names=[]`、`status=installed`、发布事件**不再投递**（`delivered_after_restart=false`） | **未达成**（F-2） |

### 2.6 M2-6（#27）插件 CLI —— **部分通过**

| 验收点 | 证据 | 判定 |
|---|---|---|
| 本地包 / 目录验证 | 签名 `.uhpkg` rc=0；未签名 rc=1 `signature_missing`；官方三目录 rc=0 | 通过 |
| market 本地镜像 | `UH_PLUGIN_MARKET_ROOT` + `market://echo-tool@1.0.0` → 安装成功（`active`）；缺 market root 时错误码 `UH-USR-1003` 带 `market_root` 明细 | 通过 |
| Git 主机白名单 | `git+https://evil.example.com/x.git` → **1.8s 内** `UH-KRN-2010 {violation: git.host}`（未发起网络） | 通过 |
| 实跑受信 Git 源安装 | `git+https://github.com/NekoHome-Studio/UH` → 57.2s 后 `UH-USR-1003 git 插件源缺少 manifest.yaml`（证明受信主机**真实 clone 可通**，但无合格仓库，正例未完成） | **部分** |
| `audit` 权限矩阵 | 输出 name/version/type/status/risk_level/permissions/package_sha256 | 通过 |
| **validate 拦权限越界** | `permissions: [fs.host.read, network.outbound]`（无 `network.allow_hosts`）→ rc=1 schema 拒绝 | 通过（schema 级） |
| **validate 拦危险调用** | `import socket / os.system / subprocess / eval / open('C:/Windows/win.ini')` 的插件 → **rc=0 validated** | **未达成**（F-5） |

### 2.7 SBX 平台矩阵证据表（M2 最小版）

| 测试 | Linux M2 | macOS M2 | Windows M2（本机实测） | M3 |
|---|---|---|---|---|
| SBX-01 读未声明文件 | 未取证（无 Linux 主机） | 未取证（无 macOS 主机） | **通过**：`UH-KRN-2010 filesystem`（SDK 行为级），**审计缺失** | AppContainer |
| SBX-02 未声明出网 | 未取证 | 未取证 | **通过**：`UH-KRN-2010 network.outbound`（SDK 行为级），**审计缺失**；出网静态扫描缺失（F-5） | AppContainer |
| SBX-03 死循环 | 未取证 | 未取证 | **通过**：`UH-CAP-5021`，0.87s | 同 |
| SBX-04 内存暴涨 | 未取证 | 未取证 | **通过**：Job Object `PROCESS_MEMORY`，512MB→`MemoryError`，内核不受影响 | cgroups |
| SBX-05 连续崩溃 3 次 | 未取证 | 未取证 | **通过**：`quarantined`，crash_count=3 | 同 |
| SBX-06 签名不符 | 三平台一致 | 同 | **通过**：missing / sha256 / trust 三类拒绝 | 回归 |
| SBX-07~09 | — | — | 不在 M2 范围 | M3 必过 |

结论：**Windows 列 6/6 通过（SBX-01/02 附审计缺口）**；Linux/macOS 两列**零证据** —— 本机与服务器均为 Windows，环境不可得，M2-2 平台矩阵未闭环。

---

## 3. SDK 上手计时（第三方视角，真实起止）

计时前提：模拟第三方，**只依 `docs/sdk/python-tool.md`** 从零创建；在 `/tmp/uh-sdk-timing/demo-sdk` 新建（仓库外）。

| 步 | 动作 | 时间 | 结果 |
|---|---|---|---|
| T0 | 按文档第 5–29 行落盘 `main.py`（`ToolPlugin` + `@plugin.tool("hello")` + `run_stdio`） | 21:05:49 | 成功 |
| T1 | 直接 `uh plugin dev <dir> --once`（文档未给其它步骤） | 21:05:51 | **失败**：`插件目录缺少 manifest.yaml` |
| T2 | 转去读 `docs/contracts/manifest/` schema 与 `tool.schema.json` | 21:05:56 | — |
| T3 | 手写 `manifest.yaml`（name/version/type/entry/risk_level/permissions/config） | 21:05:57 | 成功 |
| T4 | `uh plugin dev --once` 灌入 3 条 JSON-RPC：`initialize` → `tools/list` → `tools/call hello` | 21:06:00 | **成功**：`hello uh`，rc=0 |

**起止 21:05:49 → 21:06:00，耗时 11 秒**（本人已知内核结构，不具第三方代表性）。

诚实说明：
1. 文档路径在 T1 **直接断链** —— `docs/sdk/python-tool.md` 没有 `manifest.yaml` 模板、字段表、调用方式（`uh plugin dev` 用法）与打包/签名步骤；真实第三方必须自行发现 `docs/contracts/manifest`（F-6）。
2. 仓库**没有** `uh plugin pack` / `uh plugin sign` 子命令（`uh plugin --help` 只有 validate/install/uninstall/list/audit/dev），签名包必须自行实现「排除 SIGNATURE 的规范化摘要 + Ed25519 over digest」才能产包 —— 本报告用的签名器是自写的 120 行脚本（`.work/acc0013/mk.py`）。
3. 因此「第三方 30 分钟内上手」的**结论性证据不足**：顺畅路径存在（11s），但**文档完备性**未达 30 分钟自助标准。

---

## 4. 跨机插件链路（C）与安全专项（D）

### 4.1 跨机链路 —— 无法执行（阻断）

| 计划动作 | 服务器实际响应 | 结论 |
|---|---|---|
| 跨机安装签名包 `POST /api/v1/plugins {source: <服务器路径>}` | **405 Method Not Allowed**（`UH-KRN-5001` 包裹） | 路由不存在 |
| 跨机主题切换 `GET/PUT /api/v1/themes` | **404 `UH-USR-1003 Not Found`** | 路由不存在 |
| 跨机钩子审计事件 | 无插件装载能力 → 无事件 | 不可达 |
| 跨机校验 `uh plugin validate` | `argument cmd: invalid choice: 'plugin'` | CLI 无 M2 子命令 |
| 服务器侧 `import kernel.plugins` | `ModuleNotFoundError` | 模块缺失 |

`GET /api/v1/plugins` 仍返回 M1 占位 `{"plugins": [], "total": 0}`（`kernel/main.py` 旧版 `app.state.plugins` 快照），佐证服务器为 M1 期构建。
**按要求未改动服务器代码/配置/部署**：C 段的验收证据不存在，需在重部署后重新执行（§7）。

### 4.2 安全专项（本机，全部亲测）

| 项 | 方法 | 结果 |
|---|---|---|
| 主题 JS 注入 | 10 条载荷矩阵（8 恶意：`url(javascript:)`/`expression()`/`<script>`/`javascript:`/`data:`/`@import`/`var()`/反斜杠转义；2 合法：色值、长度） | **8/8 恶意载荷拒绝、2/2 合法通过**；主题无 JS 字段，资产白名单只允许 `assets/preview.png` + font_stack 对应 WOFF2 |
| 插件签名验签（Ed25519，`UH_PLUGIN_TRUSTED_KEYS_FILE`） | 未签名 / 内容篡改 / 不受信私钥 | 三类全部拒装（`signature_missing` / `sha256` / `signature`），错误码 `UH-KRN-2010` |
| 沙箱越权拒绝 + 审计 | 插件内越界读、未声明出网 | 拒绝通过（KRN-2010）；**审计缺失**（F-4） |
| 钩子异常隔离 | `sys.exit(7)` 钩子 + 事件发布 | 发布 0.3ms 返回，主链路不受影响；`hook.failed` 记录 |
| secret 扫描 | `node scripts/secret-scan.mjs` | 无命中（rc=0）；报告全文不含 token 原文 |

---

## 5. 新发现（按严重性）

### F-1（P0）服务器未部署 M2 基线 → 门禁声明失真、跨机无法验收
- 位置：部署侧（`D:\uh`）；本地事实源 `kernel/main.py:229-234`、`kernel/cli.py:381-395`
- 复现：§1.2、§1.3、§4.1（哈希 12 same / 4 diff / 5 missing；405 / 404 / CLI 无 `plugin`；pytest 147+1F）
- 影响：brief 的「195/195（服务器）」与「C 跨机链路」两项前提同时失效；MS-3 无法在本基线上结项。

### F-2（P1）内核启动后不重激活已安装插件（M2-1、M2-5 待补项均未实现）
- 位置：`kernel/plugins.py:561-565`（`_load_registry` 把 `active` 降级为 `installed`）、`kernel/main.py:333-346`（startup 仅注册检索工具）、全仓无 `reactivate`/`activate_record` 入口
- 复现：同注册表新建驱动 → `status: active → installed`、`processes_after_boot=0`、无 re-activate API；钩子重启后 `names=[]` 且事件不再投递（`delivered_after_restart=false`）
- 影响：插件在重启后静默失效（工具不可调用、审计钩子静默丢失）——对审计场景属可用性/合规缺口。

### F-3（P1）`uh plugin install`（工具插件）偶发 60s+ 不返回，注册表停在 `installed`
- 位置：`kernel/cli.py:263-270`（一次性 driver + `asyncio.run`）、`kernel/sandbox.py:125-128`（monitor 用 `to_thread(process.wait)`）
- 复现：`uh plugin install market://echo-tool@1.0.0`（trust store 就位），**4 次中 2 次**超时 60s/90s，`plugins.json` 记 `status: installed`；同命令换 stdout 重定向/清空 root 后 4.9s 返回 `active`
- 机理：一次性 driver 留下存活插件子进程，`asyncio.run` 关闭时默认执行器线程阻塞在 `process.wait()`（Python 3.14 `THREAD_JOIN_TIMEOUT=300s`）
- 影响：CI/脚本安装不可靠；无残留清理（孤儿进程风险）。生产 REST 路径（常驻 loop）未复现。

### F-4（P2）沙箱越权拦截无审计记录（SBX-01/02 要求「拦截 + 审计」）
- 位置：`kernel/sandbox.py:62-82`（`SandboxPolicy` 抛 `SandboxError`，不落审计）；全仓无 `plugin.sandbox.*` 事件
- 复现：插件内越界读/未声明出网 → 客户端拿到 `UH-KRN-2010`，内核日志仅 `request.*`，无拒绝事件
- 影响：Windows M2 行为级拦截缺少可追溯证据链，与 `docs/contracts/sandbox.md:109-110` 的「+审计」判据不符。

### F-5（P2）`uh plugin validate` 无源码静态扫描，危险调用可安装
- 位置：`kernel/plugins.py:736-747`（validate 仅 manifest schema + 主题资产 + 签名）、`kernel/cli.py:253-260`
- 复现：含 `import socket`、`os.system`、`subprocess`、`eval`、读 `C:/Windows/win.ini` 的插件 → `status: validated`，rc=0
- 影响：M2-6「validate 能拦住权限越界与危险调用」只达成 schema 半句；`sandbox.md:84` 把静态扫描列为 Windows 降级的关键补偿控制，实际未落地。

### F-6（P2）SDK 指南不自洽：按文档无法从零走到调用成功
- 位置：`docs/sdk/python-tool.md:5-31`
- 复现：§3 的 T1 —— `uh plugin dev` 报「插件目录缺少 manifest.yaml」；文档亦未提 `uh plugin dev`、`manifest.yaml`、`.uhpkg` 打包与签名流程
- 影响：M2-3 的第三方 30 分钟指标缺可复现路径。

### F-7（P2）CLI 与内核的钩子加载器不一致
- 位置：`kernel/plugins.py:516-527`（只要传了 `sandbox_runner`，`hook` 也映射到 `ProcessPluginLoader`）、`kernel/cli.py:249-250` vs `kernel/main.py:233`（内核显式覆盖为 `HookPluginLoader`）
- 复现：`uh plugin install <hook>.uhpkg` → 只拉起进程、不注册 `HookManager` 订阅；同样操作在 REST 下正常投递
- 影响：CLI 侧安装钩子"看似成功但不工作"，排障成本高。

### F-8（P3）主题 WOFF2 只校验头部
- 位置：`kernel/themes.py:74-77`（magic/length/numTables/reserved，缺 totalSfntSize、表目录与 Brotli 解压校验）
- 复现：1KB 全零 body + 合法头部 → `theme_asset_type` 返回 `font/woff2`，可安装；浏览器端 `FontFace` 失败被 `catch` 静默吞掉（`ui/themes.js:47`）
- 影响：伪字体可通过安装（无渲染断言）；真实 WOFF2 路径已单独验证可用。

### F-9（P3）`scripts/review-precheck.mjs` 直接执行不可用
- 复现：`node scripts/review-precheck.mjs` → `MODULE_NOT_FOUND`；但其作为 `verify-all` 的 C 项通过
- 影响：门禁入口不一致，单跑该脚本会误判。

### F-10（P3，登记）主题 REST 未进 OpenAPI；单共享 token
- `GET/PUT /api/v1/themes`、`/api/v1/themes/{name}/assets/{asset}` 未登记 `docs/contracts/openapi.yaml`（已知局限，按只增后续版本）；
- 单共享 token 的 per-user principal 待 M3（已知局限）。二者不阻断。

---

## 6. 六任务「结项前仍需确认」对照（对 `docs/reviews/2026-11-20-M2-收尾验收.md`）

| 待补项 | 本轮结论 |
|---|---|
| M2-1 已安装工具从生产能力目录调用闭环 | **已补**（安装目录 MCP 握手 + 调用，921.9ms） |
| M2-1 内核启动后插件重激活 | **未补**（F-2） |
| M2-2 Windows SBX-01/02 拒绝 + 审计 + 出网静态扫描 | 拒绝 ✅ / 审计 ❌（F-4）/ 静态扫描 ❌（F-5） |
| M2-2 Windows SBX-04 实机内存限额 | **已补**（512MB vs 64MB） |
| M2-2 Linux/macOS SBX-01~05 | **未补**（无环境，零证据） |
| M2-3 第三方 30 分钟计时 | 已计时 11s，但文档不完备（F-6） |
| M2-3 `plugin dev` 热重载 | **已补**（BOOT-A→BOOT-B） |
| M2-3 签名工具注册与调用 | **已补** |
| M2-4 WOFF2 渲染样本 | **已补**（真实 WOFF2 + FontFace loaded） |
| M2-5 跨平台进程行为 | 仅 Windows |
| M2-5 服务重启后 Hook 恢复策略 | **未补**（F-2） |
| M2-6 实跑受信 Git 源安装 | 部分（受信主机 clone 通，无合格仓库） |
| M2-6 静态扫描越界/危险调用拒绝 | **未补**（F-5） |
| M2-6 目录/市场/Git 安装验收 | 目录 ✅ / 市场 ✅ / Git 部分 |

---

## 7. 验收结论与 MS-3 结项建议

### 结论：**不通过**（1 个 P0 阻断）

本机 M2 实现质量总体良好：签名链路（Ed25519 + 规范化摘要）、生命周期回滚、Job Object 资源限额、崩溃隔离、Hook 异步隔离与元数据最小化、主题数据化（CSS 变量白名单 + 真实 WOFF2）都经生产链路可观测地成立，浏览器端主题切换 14–31ms。

但不能结项，原因只有一条且是硬性的：**被验收的「跨机真实运行时」并不存在**。服务器仍是 M1 期内核（缺 5 个 M2 模块、4 个文件版本落后），`POST /plugins` 405、`/themes` 404、CLI 无 `plugin` 子命令、服务器单测 147+1F。brief 的「195/195（本机+服务器）」与「C 跨机链路」因此双双失效，而 MS-3 的出口标准正是这两项。

### MS-3 结项建议（按序执行）

1. **重部署** `067e7a2` 到 `192.168.0.28:D:\uh`（`scripts/server-deploy.ps1`，生产鉴权 B + `UH_DEV_MODE=1`），随后复跑：
   - `python -m pytest -q`（应为 195/195；当前遗留 `test_websocket_bearer_token_is_sufficient_owner_identity` 失败需一并定位）；
   - 双端 `kernel/*.py` 哈希应 21/21 SAME（本报告的 `deploy-diff.json` 可直接复用为对照基线）。
2. **重跑 C 段**（跨机 install/activate/uninstall、主题跨机切换、钩子跨机审计事件），探针把 `UH_PLUGIN_ROOT/UH_PLUGIN_REGISTRY/存储` 指临时目录。
3. **修 F-2**（启动重激活）与 **F-3**（CLI 安装挂起）后复验 M2-1/M2-5 的「重启恢复」两项。
4. **补 F-4/F-5**（沙箱审计事件 + validate 静态扫描规则最小集）后再判 M2-2/M2-6。
5. Linux/macOS 矩阵：若 M2 出口仍要求三平台，需要一台 Linux 与一台 macOS 执行机；否则应由 DSH 在任务卡上显式降级为「Windows-only M2」，并在 MS-3 记录中写明。

上述 1–2 完成即可重开 MS-3 复验；3–5 可与 M3 并行，但不建议在 F-5（静态扫描）缺失的情况下对外发布插件市场。

---

## 8. 探针副作用记录（如实登记）

本机（均可清理，均在 `.work/` 或系统临时目录，`.work/` 已 gitignore）：
1. `.work/acc0013/`：签名素材与密钥（`keys/ed25519.key`、`keys/trust.json`）、探针脚本（`mk.py`、`probe_driver.py`、`probe_live.py`、`probe_live2.py`、`probe_cli.py`、`probe_sbx.py`、`probe_restart.py`、`deploy_diff.py`）、运行目录（`run/*` 插件根与注册表）、`live/`（临时内核插件根）、`dist/*.uhpkg`、`deploy-diff.json`。
2. 临时内核实例：`127.0.0.1:8099`（`UH_DEV_MODE=1`、`UH_LOCAL_MODE=1`、插件根/注册表/存储全部指向 `.work/acc0013/live`），验收结束后进程已退出；未触碰仓库 `.uh/`。
3. Python 环境：`pip install fonttools brotli`（用户级 sitepackages，用于生成真实 WOFF2 样本）。
4. 浏览器：ego-browser 复用 `default` 任务空间访问本机 8099，未登录任何外部站点。
5. 仓库工作树：未修改任何受版本控制文件；`git status` 仅新增本报告（及其他审查者的报告，未阅读）。

服务器（只读为主）：
6. `ssh ipc` 执行了 `pytest`（新增/刷新 `D:\uh\.pytest_cache`，测试用临时目录由 conftest 隔离）、`import kernel.plugins`、`kernel.cli plugin list`、`Get-FileHash`、`Get-Content kernel.out.log`。
7. HTTP 只读探测：`GET /health`、`/ready`、`/openapi.json`、`/api/v1/plugins`，以及 `POST /api/v1/plugins`（返回 405，未产生写入）、`GET /api/v1/themes`（404）。**未修改服务器源码、`.auth-token`、服务配置，未重启/停止服务，未部署任何文件。**

---

## 9. 证据复现命令（最小集）

```powershell
# 本机门禁
python -m pytest -q
node scripts/verify-all.mjs --full
node scripts/secret-scan.mjs

# 部署一致性（双端 kernel/*.py 哈希）
python .work\acc0013\deploy_diff.py
ssh ipc "cd /d D:\uh && .venv\Scripts\python.exe -m pytest -q"

# 签名 / 回滚 / 强杀 / 隔离 / 重激活
python .work\acc0013\probe_driver.py signature tool rollback quarantine forcekill restart hookclean

# 生产链路（主题 / 钩子 / 资源）
$env:UH_DEV_MODE="1"; $env:UH_LOCAL_MODE="1"
$env:UH_PLUGIN_ROOT="$PWD\.work\acc0013\live\plugins"; $env:UH_PLUGIN_REGISTRY="$PWD\.work\acc0013\live\plugins.json"
$env:UH_PLUGIN_TRUSTED_KEYS_FILE="$PWD\.work\acc0013\keys\trust.json"
python -m uvicorn kernel.main:app --host 127.0.0.1 --port 8099
python .work\acc0013\probe_live.py; python .work\acc0013\probe_live2.py

# 沙箱矩阵 / 钩子隔离 / 重启恢复 / CSS 矩阵
python .work\acc0013\probe_sbx.py
python .work\acc0013\probe_restart.py

# CLI / 市场 / Git 白名单 / dev 握手 / 热重载
python .work\acc0013\probe_cli.py
```

---

**签署：Claude-Sonnet-4.5** · 2026-11-20 · 独立验收（只读审查 + 亲测运行时 + 跨机部署比对）
