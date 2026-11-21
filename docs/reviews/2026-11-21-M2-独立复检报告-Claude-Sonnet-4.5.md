# M2 独立复检报告（第三方 · 确认 P0 与缺口闭合 · MS-3 结项）

2026-11-21 · 独立复检审查者（非实现方 DSH / Codex） · 基线 commit **`3354126`**（工作树 HEAD `ff49723`，仅多 brief 0015 提交）
本机 `192.168.0.14`（Windows） · 服务器 `192.168.0.28`（`ssh ipc`，`D:\uh`，生产鉴权 B + 验收期 `UH_DEV_MODE=1`）
方式：只读代码/契约 + 双端真实运行时亲测；未修改受版本控制源码、契约与服务器配置；未阅读其他审查者本轮材料。

签署模型：**Claude-Sonnet-4.5**（Anthropic，经 DSH 执行）

---

## 0. 复检结论（先给判定）

| 项 | 判定 |
|---|---|
| **A 门禁 + 部署一致性（上轮 P0）** | **闭合**：本机 205/205、服务器 205/205、verify-all full 8/8、`kernel/*.py` **21/21 SAME**、生产 8080 M2 路由全部 200 |
| **B 跨机 M2 链路** | **通过**：签名包 install/activate/uninstall、生产工具调用、主题跨机切换、钩子审计 trace/span、**重启后重激活**全部跑通 |
| **C 上轮缺口逐项** | 8 项中 **6 项闭合、2 项部分**（静态扫描绕过面、SDK 文档 3 处字段错误） |
| **D 安全专项** | 通过（签名/篡改/不受信密钥拒装、主题 JS 注入 8/8 拒、KRN-2010 + 审计、钩子隔离、secret 扫描 0）；新增 2 个 P2 残留（见 §5） |
| **E Windows SBX 矩阵** | **SBX-01~05 全部通过（实机）**；Linux/macOS 按用户裁决登记 M3 |
| **总体** | **有条件通过**：P0/P1 全部闭合，MS-3 可结项；结项前修 R-3（SDK 文档 3 行），R-1/R-2 登记 M3 加固 |

条件（结项前必须完成）：
1. **R-3**：修 `docs/sdk/python-tool.md` 三处与实现不符的字段（§2 `allow_hosts` 位置、§5 SIGNATURE 字段、§5 内容长度 8 字节）。
2. **R-1/R-2**：静态扫描绕过面与插件子进程环境泄漏，作为 M3 加固项登记（或本迭代内补 alias/from-import/JS 与 env 裁剪）。

其余 R-4~R-8 为 P3 观察项，登记即可。

---

## 1. 门禁复现（亲测）与双端哈希一致性

### 1.1 门禁

| 门禁 | 命令 | 结果 |
|---|---|---|
| 本机单测 | `python -m pytest -q` | **205 passed**，292 warnings，55.67s |
| 服务器单测 | `ssh ipc ".venv\\Scripts\\python.exe -m pytest -q"`（`D:\uh`） | **205 passed**，279 warnings，46.26s |
| 契约/交叉门禁 | `node scripts/verify-all.mjs --full` | **VERIFY_ALL_PASSED (8 checks, full)**（A/B/B2/C/D 19/19/E INFO×5/F/G） |
| 密钥扫描 | `node scripts/secret-scan.mjs` | rc=0，无命中 |
| 工作树 | `git status --short` | 仅未跟踪报告文件；无源码改动 |

上轮服务器遗留失败 `test_websocket_bearer_token_is_sufficient_owner_identity` 已随新部署消失（全套 205 passed）。

### 1.2 双端 `kernel/*.py` SHA-256（上轮 P0 的闭合证据）

**21 个文件全部 SAME，diff 0，missing 0**（上一轮为 12 SAME / 4 DIFF / 5 MISSING）：

| 文件 | 状态 | 双端哈希(12) |
|---|---|---|
| kernel/__init__.py | SAME | 85dea3914816 |
| kernel/cli.py | SAME | （上轮 DIFF → 现一致） |
| kernel/confirmation.py | SAME | c11c35afb59e |
| kernel/event_mapping.py | SAME | dd41662028b7 |
| **kernel/hooks.py** | SAME | （上轮缺失） |
| kernel/llm.py | SAME | d301f58a9ad1 |
| kernel/logging.py | SAME | 6590a5b6b2bd |
| kernel/long_term_memory.py | SAME | 9934100e2373 |
| **kernel/main.py** | SAME | （上轮 DIFF） |
| kernel/mcp.py | SAME | 7c7ece415186 |
| kernel/memory.py | SAME | 8ba8baa573ec |
| kernel/orchestration.py | SAME | 131ebfc00ceb |
| **kernel/plugin_sdk.py** | SAME | （上轮缺失） |
| **kernel/plugins.py** | SAME | （上轮缺失） |
| **kernel/protocol.py** | SAME | （上轮 DIFF） |
| kernel/retrieval.py | SAME | 3fa7871192ba |
| kernel/router.py | SAME | 051e9fb8a6e9 |
| **kernel/sandbox.py** | SAME | （上轮缺失） |
| **kernel/tasks.py** | SAME | （上轮 DIFF） |
| **kernel/themes.py** | SAME | （上轮缺失） |
| kernel/tracing.py | SAME | 8acf22955d8b |

采集方式：本机 `hashlib.sha256` + 服务器 `.venv` 内 `hashlib.sha256`，逐文件比对（脚本 `.work/acc0013/deploy_diff.py`，输出 `deploy-diff.json`）。

### 1.3 生产服务器（8080）M2 路由实测

| 探测 | 上轮 | 本轮 |
|---|---|---|
| `GET /api/v1/themes` | 404 | **200** `{"active":null,"themes":[]}` |
| `GET /static/themes.js` | 404（文件不存在） | **200**（内容为 `createThemeApplier` 源码） |
| `GET /api/v1/plugins` | 200（M1 占位快照） | **200**（`{"plugins":[],"total":0}`，驱动实装） |
| `GET /openapi.json` 路径数 | 23 | **30**，含 `/api/v1/themes`、`/api/v1/themes/active`、`/api/v1/themes/{name}/assets/{asset}`、`POST/DELETE /api/v1/plugins` |
| 未带 token 请求 | 401 | 401 `UH-USR-1001`（生产鉴权 B 有效） |
| `/health` | 200 | 200 |

> 说明：跨机写入类探针（`POST /api/v1/plugins`、主题切换、钩子装载）**未在生产 8080 执行**，而是按 brief 纪律在服务器上起**隔离实例**（独立端口 8091 + 独立插件根/注册表/存储/信任库，`D:\acc0015`），探针后实例已杀、临时目录已删，生产运行态零污染（§7）。

---

## 2. 跨机 M2 链路（B 段，本机客户端 → 服务器隔离实例）

链路：本机脚本 → SSH 隧道 → 服务器 `127.0.0.1:8091`（`D:\uh` 代码 + `D:\acc0015` 临时插件根 + `UH_AUTH_REQUIRED=1`、`UH_AUTH_TOKEN_FILE=D:\uh\.auth-token`）。

| 动作 | 结果 |
|---|---|
| 未认证 `GET /api/v1/plugins` | 401 `UH-USR-1001`（鉴权生效） |
| 跨机安装签名包 `echo-tool` | **201 `active`**，904 ms |
| 跨机安装签名包 `deny-probe`（触发越权） | **201 `active`**，623 ms |
| 跨机安装签名主题 `theme-acc` | **201 `active`**，155 ms |
| 跨机安装签名钩子 `audit-acc` | **201 `active`**，171 ms |
| 跨机生产工具调用（`POST /api/v1/dev/tool-task` → task → MCP） | 202 → `finished`，SSE 返回 `[{'type': 'text', 'text': 'xmachine-ok'}]`，`trace_id=uh_trc_bc1d9d43…` |
| 跨机越权工具调用 | task `finished`，返回 `fs=UH-KRN-2010;net=UH-KRN-2010`；服务器侧 `D:\acc0015\plugin-sandbox-audit.jsonl` 写入 2 条，`trace_id` 与任务**完全一致**（`uh_trc_c3390d4f…`） |
| 跨机主题切换 `PUT /api/v1/themes/active` ×5 | **46.5 / 64.9 / 107.7 / 109.3 / 110.6 ms**（含 SSH 隧道开销，均 <200 ms） |
| 跨机取 WOFF2 | 200，12,344 B，`font/woff2`，magic `wOF2` |
| 跨机钩子审计（`POST /…/messages`） | 服务器侧 `audit.jsonl` 追加 `kernel.route.decided` + `capability.module.invoked`，`trace_id=uh_trc_bf16e7f3…` 与消息任务一致，`event_id` 12/13，payload 仅元数据 |
| **重启隔离实例**（杀进程 + 同注册表重启） | `GET /plugins` → 4/4 **active**；工具调用返回 `after-restart-ok`；钩子继续投递（`trace_id=uh_trc_3a71f5c1…` 命中）；主题仍 `active=theme-acc` |
| 跨机卸载 4 个包 | 4/4 `uninstalled`，`GET /plugins` → `{"plugins":[],"total":0}`，`D:\acc0015\plugins` 子项数 **0**（无残留） |

跨机结论：**链路可用且可恢复**——上轮"跨机无法执行"的阻断消失；`restore_active` 在真实重启路径上生效（工具、钩子、主题三类资产同时恢复）。

---

## 3. 上轮缺口逐项闭合判定

| 上轮问题 | 声称处置 | 复检证据 | 判定 |
|---|---|---|---|
| **P0 服务器仍 M1 部署** | 全量 scp + pyyaml | §1.2 哈希 21/21 SAME；§1.3 生产路由 200/200/200；服务器 205 passed | **闭合** |
| M2-1 生产调用闭环 | `tasks.plugin_host` 路由 | 本机 `dev/tool-task`（8099）→ `finished`，越权探针返回 `fs=UH-KRN-2010;net=UH-KRN-2010`；跨机（8091）返回 `'xmachine-ok'`；重启后返回 `'after-restart-ok'`；`kernel/main.py:312` 注入 `plugin_host=lambda name: driver.tool_host(name)`、`kernel/tasks.py:165-167` 优先用插件 Host | **闭合** |
| M2-1 启动重激活 | `startup register_capabilities` | `kernel/main.py:346-360` 调 `await plugin_driver.restore_active()`；实测重启后 4/4 active、工具可调、钩子 trace 命中；`kernel/plugins.py:646-664` 失败即 `quarantined` 并记 `plugin.restore.failed` | **闭合** |
| M2-2 沙箱审计 + 错误码 | `plugin-sandbox-audit.jsonl` + KRN-2010 | 本机 `live2/plugin-sandbox-audit.jsonl` 与服务器 `D:\acc0015\plugin-sandbox-audit.jsonl` 均含 `{event,plugin,permission,violation,trace_id}`；MCP 侧 `_meta["uh.sandbox.denials"]` → `MCPHost._request_call` 落盘（`kernel/mcp.py:446-451`、`kernel/sandbox.py:296-310`） | **闭合**（残留 R-4） |
| M2-2 出网静态扫描 | `_static_scan` + install 联动 | 直接危险调用 **拒绝**（3 条 findings：os.system/subprocess.run/eval）；出网导入 **告警**（socket/urllib.request）；`install` 与 `validate` 同一路径（`kernel/plugins.py:825-851`） | **部分**（F-1，5 类绕过） |
| M2-3 生产态工具运行 | 子进程注入仓库根 PYTHONPATH | `kernel/sandbox.py:411-413` 对 `type=="tool"` 注入 `sdk_root`；签名安装 + 生产调用成功、`crash_count=0`、无 `plugin.crash` | **闭合** |
| M2-6 安装来源 | 目录/市场/受信 Git 端到端 | `pytest -k "install_sources or git or market or directory"` → **5 passed**（含本地 HTTPS git daemon fixture，`tests/unit/test_plugins.py:646-651`）；CLI 市场安装 **5/5 rc=0，2.0–2.6s，status=active** | **闭合** |
| flaky 崩溃测试 | 轮询到 quarantined | `pytest -k "quarantine or crash"` 连跑 5 次：**5×3 passed**（4.90–6.09s）；独立探针 quarantine 0.4s 达 `crash_count=3` | **闭合** |
| SDK 文档断链 | manifest 模板/--no-verify/打包签名补全 | 文档已补 §1–§6（目录/manifest/命令/签名/权限矩阵）；**但 3 处字段与实现不符**（F-3） | **部分** |

---

## 4. Windows SBX 平台矩阵（E 段，实机亲测）

| 测试 | 方法 | 结果 |
|---|---|---|
| **SBX-01** 读未声明文件 | 插件内 `context.read_text('C:/Windows/win.ini')`（仅声明 `fs.sandbox.read`） | **拒绝** `UH-KRN-2010 {permission: fs.host.read, violation: filesystem}`；审计落盘（plugin/permission/violation/trace_id） |
| **SBX-02** 未声明出网 | `context.allow_host('https://evil.example.com/')`（未声明 `network.outbound`） | **拒绝** `UH-KRN-2010 {permission: network.outbound, violation: network.outbound}`；审计落盘；静态扫描对 `socket/urllib.request` 出网导入告警 |
| **SBX-03** 死循环 | `timeout_ms=800` + `sleep(30)` | **`UH-CAP-5021`**，0.86 s 返回，子进程回收 |
| **SBX-04** 内存暴涨 | `memory_limit_mb=64` + 分配 512 MB | 子进程 `MemoryError`，rc=1，**0.17 s** 结束；内核 `/ready` 仍 200 |
| **SBX-05** 连续崩溃 | 立即 `sys.exit(3)` 的插件 | **`quarantined`，crash_count=3**（0.4 s；`plugin.crash ×2 → plugin.quarantined`） |
| SBX-06（安装期） | 未签名 / 篡改 / 不受信密钥 | 三类拒装：`signature_missing` / `sha256` / `signature`（`UH-KRN-2010`） |
| Linux / macOS SBX-01~05 | — | **无 Linux/macOS 主机**（本机与服务器均为 Windows），按用户裁决**登记 M3 补证**，本轮不判 |

---

## 5. 新发现（严重性 + file:line + 复现）

### R-1（P2）静态扫描是 AST 字面匹配，5 类写法可绕过
- 位置：`kernel/plugins.py:381`（`_DANGEROUS_CALLS` 白名单为 `"模块.函数"` 字面量）、`kernel/plugins.py:405-414`（只按 `ast.Attribute.value.id` 取基名）、`:388-390`（只扫 `.py`）
- 复现（`validate --no-verify` 结果）：
  | 样本 | 结果 |
  |---|---|
  | `os.system(...)` / `subprocess.run(...)` / `eval(...)` | **REJECTED** `static_scan.exec`（3 findings）✓ |
  | `import subprocess as sp; sp.Popen([...])` | **ACCEPTED**（`sp.Popen` ≠ `subprocess.Popen`） |
  | `from os import system; system('whoami')` | **ACCEPTED**（`system` 不在白名单） |
  | `getattr(os, 'system')('whoami')` | **ACCEPTED** |
  | `importlib.import_module('socket')` | **ACCEPTED**（无 network finding） |
  | JS 入口 `main.mjs` + `child_process.execSync` | **ACCEPTED**（只扫 `.py`） |
- 影响：`sandbox.md:84` 把静态扫描列为 Windows 降级的关键补偿控制；绕过面存在时，实际只剩 SDK 行为级拦截。建议 M3 前补：模块别名归一（`ast` 解析 import 别名表）、`from ... import` 归一、`getattr`/`__import__`/`importlib` 一律拒绝或人工复核、JS/TS 入口纳入扫描。

### R-2（P2）工具插件子进程继承内核完整环境（含 `UH_AUTH_TOKEN`）
- 位置：`kernel/sandbox.py:409-410`（`type != "hook"` 时 `env = os.environ.copy()`），对比 `:404-408`（hook 只保留 `SYSTEMROOT/WINDIR/SYSTEMDRIVE/PATH`）
- 复现：设 `UH_AUTH_TOKEN=<probe-token-已脱敏>`、`UH_AUTH_TOKEN_FILE=<probe-path-已脱敏>`，安装并运行工具插件，插件内读到 `{"UH_AUTH_TOKEN": "<present len=24>", "UH_AUTH_TOKEN_FILE": "<present len=19>", ...}`
- 影响：签名但越权的插件可夺走内核 bearer token（生产鉴权 B 下等同全权）；`fs.host.read` 插件还可直接读 `D:\uh\.auth-token`。建议：tool 与 hook 同样做环境白名单（`UH_SANDBOX_*` + `SYSTEMROOT` + `PATH` + `PYTHONPATH`），凭据只经内核代理注入。

### R-3（P2）`docs/sdk/python-tool.md` 三处与实现不符，照抄必失败
- 位置与实测：
  1. `docs/sdk/python-tool.md:59`：模板把 `allow_hosts: []` 放在 **config** 内 → schema `configBase`（`docs/contracts/manifest/common.schema.json`）`additionalProperties:false` 且 `network.allow_hosts` 必填 `minItems:1` → **`validate` rc=1 manifest 校验失败**（探针 S2）。
  2. `:93`：`SIGNATURE` 写成 `{algorithm, key_id, public_key, signature}` → 实现要求 `digest`（`kernel/plugins.py:499-502`）→ **`SIGNATURE 字段不完整`**（探针 S5）。
  3. `:92`：规范化摘要写 `len(content)` **4 字节**，实现是 **8 字节**（`kernel/plugins.py:454-462`）→ **`插件包 SHA-256 校验失败`**；改 8 字节后同一包安装成功（探针 S6 → `status: active`）。
- 影响：M2-3 的验收标准是"第三方按文档 30 分钟内跑通"。我按文档忠实执行，前两次必失败，第三次需读 `kernel/plugins.py` 才能定位 —— 文档仍是自助上手的主要摩擦点。修正量约 3 行。

### R-4（P3）沙箱审计由插件进程自报，绕过 SDK 即无审计
- 位置：`kernel/plugin_sdk.py:139-156`（handler 内 `capture_denials()` 收集）、`kernel/mcp.py:446-451`（宿主读 `_meta["uh.sandbox.denials"]` 落盘）
- 说明：插件若不用 `ToolContext`（自行 `open()`/`socket`），既不触发 `SandboxPolicy` 也无 `_meta` → 无审计。Windows M2 本就是行为级降级（`sandbox.md:78-90`），可接受，但应在契约/UI 风险披露中写明"审计覆盖 = 走 SDK 的访问"。
- 另：`kernel/sandbox.py:296-301` 的 `_audit` 只放行 4 种 permission × 3 种 violation 组合，其余组合**静默丢弃**（无 `audit_failed`/`skipped` 记录）。

### R-5（P3）`ProcessPluginLoader` 路径仍存在 `asyncio.run` 收尾阻塞隐患
- 位置：`kernel/sandbox.py`（`SandboxProcess.wait` 用 `asyncio.to_thread(process.wait)`）+ `ProcessPluginLoader._monitor`
- 复现：自建 `PluginDriver(sandbox_runner=..., capability_registry=None)` 后 `asyncio.run(driver.install(pkg))` → 安装返回 `active`，但解释器退出被阻塞；`faulthandler` 栈显示工作线程停在 `subprocess.py:1278 wait`，主线程停在 `asyncio/runners.py:203 close`。
- 现状缓解：CLI 已改为 `capability_registry=CapabilityRegistry(...)`（`kernel/cli.py:268-269`）走 `MCPPluginLoader`（异步 `process.wait()`），并在 `finally` 调 `driver.shutdown()`（`:287-288`）→ CLI 安装 5/5 稳定（2.0–2.6 s），无孤儿进程（已查）。
- 建议：给 `ProcessPluginLoader` 也加同样的 shutdown/terminate 收尾，或文档注明"嵌入方必须传 capability_registry"。

### R-6（P3）REST `no_verify` 暴露面依赖环境兜底
- 位置：`kernel/main.py:81-83`（`InstallRequest.no_verify: bool = False`）、`kernel/plugins.py:826-827`（仅当 `security.allow_unverified` 为真才允许）
- 说明：逻辑正确（默认关），但生产纪律需保证 `UH_PLUGIN_ALLOW_UNVERIFIED` 未与 `UH_LOCAL_MODE` 同时开启；建议 REST 层直接拒绝 `no_verify=true`（仅 CLI 开发态可用）。

### R-7（P3，登记）主题 WOFF2 仅校验头部
- 位置：`kernel/themes.py:74-77`（magic/length/numTables/reserved）。伪造头部的 1 KB 文件可通过安装，浏览器 `FontFace` 失败被 `ui/themes.js:47` 的 `.catch(() => {})` 静默吞掉。真实 WOFF2 路径已验证可用（12,344 B，`UHAcc: loaded`）。

### R-8（P3，登记）无 `uh plugin pack/sign` 子命令
- `uh plugin --help` 仅 `validate/install/uninstall/list/audit/dev`；文档已在 `:96` 显式声明"列入 M2 后续"。签名器需第三方自写（本复检用的签名器 120 行）。

---

## 6. SDK 上手计时（第三方视角，真实起止）

严格按 `docs/sdk/python-tool.md` 从零创建（仓库外临时目录，`%TEMP%\uh-sdk-recheck2`）：

| 步 | 时间 | 动作 | 结果 |
|---|---|---|---|
| S1 | 11:58:03 | 按 §1/§2/§3 落盘目录 + `manifest.yaml`（照抄模板，含 `config.allow_hosts: []`）+ `main.py` | 完成（0.0s） |
| S2 | 11:58:05 | `uh plugin validate ./my-tool --no-verify` | **rc=1** `manifest 校验失败`（0.0s→1.4s） |
| S3 | 11:58:06 | 把 `allow_hosts` 从 `config` 移除后重跑 | **rc=0 `status: validated`**（1.4s→2.8s） |
| S4 | 11:58:07 | `uh plugin dev ./my-tool --once` + `initialize/tools/list/tools/call` | **`hello third`**（2.8s→4.4s） |
| S5 | 11:58:09 | 按 §5 构造 `SIGNATURE`（`public_key` 版）并 `install` | **rc=1 `SIGNATURE 字段不完整`**（4.4s→5.7s） |
| S6 | 11:58:10 | 改为 `digest` 字段（内容长度仍按文档 4 字节） | **rc=1 `SHA-256 校验失败`**（5.7s→6.9s） |
| S7 | 12:00 左右 | 改 8 字节长度 | **rc=0 `status: active`** |

- **本人耗时：6.9 秒**（到 S6），但这是**已知实现**的复检视角。
- 诚实结论：**照文档忠实执行的第三方会在 S2、S5、S6 连续失败**，且 S6 的错误信息（`SHA-256 校验失败`）不指向"文档写错 4/8 字节"，需读源码才能定位 → 与"30 分钟自助"仍有距离。修正 R-3 三行后，按 S3/S7 的实测节奏，第三方路径预计 **≤5 分钟**。

---

## 7. 复检结论与 MS-3 结项建议

### 结论：**有条件通过**

上轮唯一的 P0（服务器未部署 M2 基线）已用双端哈希 21/21 与生产路由 200 实证闭合；上轮 8 项功能缺口 6 项闭合、2 项部分。跨机链路本轮**第一次真正跑通**：签名包安装/激活/卸载、生产工具调用、主题切换（46–111 ms）、钩子审计 trace 对齐，且**重启后工具、钩子、主题三类资产同时恢复**——这正是 MS-3 出口要的证据。Windows SBX-01~05 全部实机通过，审计与错误码同时落地。

不给"通过"的两个理由都在"证据可复现性"而非实现：
- **R-3** 让"第三方按文档 30 分钟跑通"这条验收标准在字面上仍不成立（照抄必失败）；
- **R-1/R-2** 让 Windows 降级沙箱的两条补偿控制（静态扫描、凭据隔离）各留一个真实缺口。

### MS-3 结项建议（按序）

1. **结项前**：修 R-3（`docs/sdk/python-tool.md` 三处），并由 DSH 用"未读源码的第三方"重跑一次计时（目标 ≤30 分钟，记录起止）。
2. **结项前**：把 R-1/R-2 以「M3 加固项」写入 M3 看板（静态扫描归一化 + 插件子进程 env 白名单），不阻断 MS-3。
3. 结项动作：DSH 审 6 任务卡证据 → GitHub 关闭 #22~#27 → MS-3 结项 → M3（含 Linux/macOS SBX 补证、主题 REST OpenAPI 登记、Desktop 主题同步 M3-6、per-user principal）。
4. 复验脚本与基线可直接复用：`deploy_diff.py`（哈希）、`probe3_xmachine.py`（跨机）、`probe2_live.py`（重激活）、`probe2_scan.py`（静态扫描矩阵）、`probe4b_sdk.py`（SDK 计时）。

---

## 8. 探针副作用记录（如实登记）

本机（均在 `.work/` 或系统临时目录，`.work/` 已 gitignore）：
1. `.work/acc0013/`：签名素材（`keys/`）、四类探针脚本（`probe2_scan/probe2_live/probe3_xmachine/probe4_sdk/probe4b_sdk/deploy_diff`）、`src2/` 复检插件源码、`dist/*.uhpkg`、`run/*`、`live2/`（临时内核插件根）、`scan2.json`、`x1.json`、`x2.json`。
2. 临时内核实例 `127.0.0.1:8099`（`UH_DEV_MODE=1`、`UH_LOCAL_MODE=0/1` 视阶段、插件根/注册表/存储指向 `live2/`）：中途 kill + 重启一次用于验证 `restore_active`，验收结束已停止；未触碰仓库 `.uh/`。
3. `%TEMP%\uh-sdk-recheck*`：第三方上手计时的插件与包（可删）。

服务器：
4. 建立并**已删除** `D:\acc0015`（临时插件根/注册表/信任库/存储/审计/探针日志 + 5 个上传包 + `start-probe.cmd`）。
5. 起停**隔离实例** `0.0.0.0:8091`（同一 `D:\uh` 代码、独立 env、`UH_AUTH_REQUIRED=1` + `UH_AUTH_TOKEN_FILE=D:\uh\.auth-token`）：共启动 2 次（首次验证 + 重启验证），结束按 PID 杀进程；生产 `8080` **全程未重启、未写入**，`/health` 前后均 200。
6. 只读探测：`GET /health|/ready|/openapi.json|/api/v1/plugins|/api/v1/themes|/static/themes.js`、`Get-FileHash`、`Get-Content` 日志；`pytest -q`（写 `.pytest_cache`，测试用临时目录由 conftest 隔离）。
7. 客户端侧 SSH 端口转发 `-L 8091:127.0.0.1:8091`（用完即断），未修改服务器 sshd/防火墙配置。
8. 未修改任何受版本控制源码/契约；未部署任何文件到 `D:\uh`。

---

**签署：Claude-Sonnet-4.5** · 2026-11-21 · 独立复检（只读代码 + 双端运行时亲测 + 隔离实例跨机链路）
