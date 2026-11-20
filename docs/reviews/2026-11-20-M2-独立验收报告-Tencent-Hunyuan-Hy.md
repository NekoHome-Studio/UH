# UH! M2 插件期独立验收报告（第三方 · 跨机 · MS-3 结项口径）

> **审查模型：Tencent Hunyuan Hy（腾讯混元 Hy）**
> 运行产品：WorkBuddy（Agent 模式）
> 验收日期：2026-11-20（20:20–20:45 GMT+8）
> 验收机：192.168.0.14（本机）· 目标机：192.168.0.28:8080（`ssh ipc`，`D:\uh`）
> 基线：brief 指定 `067e7a2`，实测 HEAD **`9da129f`**（仅多一个 brief 提交，代码一致）；工作区干净
> 口径：Brief 0013（M2 插件期独立验收，MS-3 结项）· `docs/reviews/2026-11-20-M2-收尾验收.md`（DSH 自验基线）· M2-1~M2-6 Issue 卡 · `docs/contracts/sandbox.md` §5 · `docs/sdk/python-tool.md`
> **独立性声明**：未阅读其他审查者的 M2 验收材料（含 `2026-11-18-M2-增量审查报告-DeepSeek-V4-Flash.md`），结论由「代码 + 冻结契约 + 本机/跨机真实运行时」独立得出。
> 报告性质：非契约、非规范文件；不修改任何受版本控制源码/契约/服务器配置，只新增本报告。凭据全文脱敏。

---

## 0. 结论摘要

**验收结论：不通过。MS-3 不建议结项。**

核心阻断不是插件代码本身——M2 的五个模块（`plugins.py` / `sandbox.py` / `hooks.py` / `themes.py` / `plugin_sdk.py`）在本机跑出了扎实的证据：签名/篡改拒装、激活失败回滚、5 秒强杀、SDK 沙箱越权拦截、主题 JS 注入拦截、钩子异常隔离、主题切换 12~18ms。SDK 第三方上手实测 28 秒（目标 ≤30 分钟）。

但 **brief 的验收前提有两处与实测不符**，且都是结项口径的硬条件：

1. **门禁数字不成立**：本机 pytest **194 passed / 1 failed**（非 195/195），失败项是 `test_plugin_crashes_restart_three_times_then_quarantine`（SBX-05 连续崩溃隔离，本机确定性失败：`crash_count=2 < 3`）；服务器 `server-test.ps1` 仅 **147 passed / 1 failed**（148 项，非 195）。
2. **跨机 M2 链路无法验证**：服务器仍停在五审基线 `kernel/main.py` SHA256 `eb727b9f…`，**五个 M2 模块根本不存在**（`certutil` 只返回 main.py 一个哈希）；`/api/v1/plugins` 返回空列表、`/api/v1/themes` 404。brief 要求的跨机 install/activate/uninstall、主题切换、钩子审计三项均无法在未部署 M2 的服务器上实测。

另有三项 brief/任务卡点名的缺口经核对仍在：M2-1 的「已安装工具从生产能力目录调用」与「内核启动后插件重激活」均未接通；SBX 平台矩阵 Linux/macOS 的 SBX-01~05 未提供任何本机证据；SDK 文档缺 `--no-verify` 所需配置说明（第三方首次 validate 必被拒）。

---

## 1. A 门禁复现（亲测）

| 检查 | 声明 | 实测 | 判定 |
|---|---|---|---|
| 本机 `pytest -q` | 195/195 | **1 failed, 194 passed, 256 warnings in 51.27s** | ❌ |
| 失败项重跑 | — | 单独重跑仍失败（5.44s / 6.12s），非偶发 | ❌ |
| `verify-all.mjs --full` | 8/8 | **VERIFY_ALL_PASSED (8 checks, full)**，A/B/B2/C/D/F/G PASS，E INFO | ✅ |
| `git diff --check` | — | 通过 | ✅ |
| 服务器 `server-test.ps1` | 195/195 | **1 failed, 147 passed in 31.60s**（共 148 项） | ❌ |

**失败根因（本机）**：`tests/unit/test_plugins.py::test_plugin_crashes_restart_three_times_then_quarantine`——插件 `main.py` 为 `time.sleep(0.05)`，测试 `await asyncio.sleep(1.5)` 后断言 `crash_count >= 3`，实测只有 2 次崩溃（`kernel/plugins.py:536` 的 `_on_plugin_crash` 计数）。进程监控循环每次崩溃重启约 0.5s，1.5s 窗口只能容纳 2 次。**这是时间敏感的测试设计缺陷**（不是产品代码没实现隔离——我的独立探针在放宽时间后能观测到 `plugin.crash` 递增），但它使 brief 声明的 195/195 在当前环境不可复现。

**失败根因（服务器）**：`tests/unit/test_websocket.py::test_websocket_bearer_token_is_sufficient_owner_identity`——这是五审前「仅 token 即可作为 owner」的旧断言，与五审后双因子语义（`test_production_client_identity_cannot_be_spoofed`）冲突。服务器代码未同步，所以跑的是旧测试集。

### 部署一致性（双端 SHA256）

| 文件 | 本机 | 服务器 |
|---|---|---|
| `kernel/main.py` | `4a99fc8c…` | **`eb727b9f…`（五审基线，不一致）** |
| `kernel/plugins.py` | `bf62e0e2…` | **不存在** |
| `kernel/sandbox.py` | `9cc656b9…` | **不存在** |
| `kernel/hooks.py` | `33ee7074…` | **不存在** |
| `kernel/themes.py` | `7f8f269a…` | **不存在** |
| `kernel/plugin_sdk.py` | `d75c8014…` | **不存在** |

服务器运维口径「scp kernel/*.py 部署」未执行到 M2 文件，跨机 M2 验收前提不成立。

---

## 2. B 六任务逐项判定

### M2-1 插件驱动 —— 部分通过

独立探针（`%TEMP%\uhm2_probe_a.py`，存储/插件根指向临时目录）：

| 验收项 | 实测 | 判定 |
|---|---|---|
| SBX-06 未签名拒装 | `UH-KRN-2010 detail={'plugin':'unsigned','violation':'signature_missing'}` | ✅ |
| 篡改包拒装 | `UH-KRN-2010`（canonical digest 与 SIGNATURE 不符） | ✅ |
| 激活失败回滚 | 自定义 loader 在 activate 抛错 → `UH-KRN-2010 stage=activate`，`records=[]`，安装目录不存在 | ✅ |
| 停用超时强杀 | SlowLoader.deactivate sleep 60s → **5.03s** 后 `status=quarantined`、`killed=True` | ✅ |
| Ed25519 验签 | 用驱动同一 canonical digest 算法构造的签名包可 validate/install（见 SDK 计时） | ✅ |
| 已安装工具从生产能力目录调用闭环 | `kernel/tasks.py:164` 只走 `MCPHost.call_tool`，无 plugin registry 查找已安装工具；DSH 收尾报告也将此项列为「结项前仍需确认」 | ❌ 缺口 |
| 内核启动后插件重激活 | `kernel/main.py:333` startup 仅 `register_capabilities(retrieval_host)`，未遍历 registry 重激活已安装插件；同列收尾报告待补项 | ❌ 缺口 |

### M2-2 最小沙箱 —— 部分通过

| 验收项 | 实测 | 判定 |
|---|---|---|
| SDK 路径权限 | 沙箱内读 ALLOWED；未声明 `fs.sandbox.write` 写 → `UH-KRN-2010 fs.sandbox.write`；越界读 `AGENTS.md` → `UH-KRN-2010 filesystem` | ✅ |
| SDK 出网白名单 | 声明 `allow_hosts: [example.com]` 后 `example.com` 放行；`evil.com`/`https://evil.com`/`api.example.com` 全拒（`network.allow_hosts`）——契约要求子域需写 `*.example.com`，判正确 | ✅ |
| 进程启动/超时回收 | Windows Job Object 实现（`kernel/sandbox.py:310` `_WindowsJob`）；本机测试集 `test_sandbox.py` 通过 | ✅（本机） |
| 连续崩溃三次隔离 | 实现存在于 `plugins.py:532-545`（`_on_plugin_crash` 计数 ≥3 置 quarantined）；但**本机 pytest 该项失败**（1.5s 只崩 2 次），独立探针放宽时间后可见递增 | ⚠️ 实现在、测试不稳 |
| 实机内存限额（SBX-04 Windows） | 未见实机 OOM Kill 证据；仅 Job Object 代码与单测 | ❌ 证据缺 |
| Linux SBX-01~05 / macOS SBX-01~05 | 无 Linux/macOS 运行环境，无拦截/回收日志、错误码与审计留存 | ❌ 证据缺 |

### M2-3 工具 SDK —— 通过（上手计时达标）

见第 3 节。本机 `test_plugin_sdk.py` / `test_official_plugins.py` 通过；`docs/sdk/python-tool.md` 模板按文档可用。`web-search` 返回 `proxy_required`（brief 已登记，不阻断）。

### M2-4 主题插件 —— 通过（Web）

独立探针：签名主题包 validate `validated`、install `active`；`select_theme()` 五次耗时 **14.97 / 14.47 / 15.29 / 14.51 / 15.21 ms**，最大 15.29ms，全部 <200ms（E2E-06 ✅）。

注入拦截（`kernel/themes.py:29` `validate_theme_config`）：

```
--uh-radius: 8px                          -> ALLOWED
--uh-bg: url(javascript:alert(1))         -> DENIED 主题变量值非法
--uh-bg: expression(alert(1))             -> DENIED
--uh-bg: 'red; } body { display:none'     -> DENIED
theme_asset('script.js'/'onclick.svg')    -> DENIED 只允许包内 PNG 预览与 WOFF2
```

合法主题仅消费 CSS 变量，无 JS 注入面。WOFF2 实际渲染样本未提供（brief 登记待补）；Desktop 同步属 M3-6。

### M2-5 钩子插件 —— 通过（本机运行时）

独立探针（同一事件循环内 register → publish → 卸载）：

```
hook received=3 actions=['kernel.route.decided'] ×3
trace match=True span match=True      （trace_id=uh_trc_probe, span_id=sp_probe）
下游在 handler 抛错后仍发布成功        （e3 is not None = True）
names_before=('audit',) after=()       （卸载后订阅清理）
```

`HookManager._worker`（`kernel/hooks.py:98`）用 `asyncio.wait_for` 包裹 handler，异常/超时吞噬并按 `hook.failed` / `hook.timeout` 记录日志，不拖主链路。

### M2-6 CLI —— 部分通过

CLI 子命令齐全：`validate` / `install` / `uninstall` / `list` / `audit` / `dev`（`kernel/cli.py:381-412`），本地包与目录验证、market 本地镜像、Git 主机白名单、audit 权限矩阵在现有测试集中通过；CI 已配置官方示例校验。

缺口（与 DSH 收尾报告一致）：**受信 Git 源实跑安装、目录/市场/Git 三种来源的端到端安装、静态扫描越界/危险调用的拒绝证据**未提供；远端 CI 运行结果未见。

### SBX 平台矩阵证据表

| 测试 | Linux M2 | macOS M2 | Windows M2 | 本轮证据 |
|---|---|---|---|---|
| SBX-01 读未声明文件 | OS 拦截 | OS 拦截 | 行为级 | ✅ Windows SDK 侧拦截 + `UH-KRN-2010 filesystem`；Linux/macOS 无证据 |
| SBX-02 未声明出网 | OS 拦截 | OS 拦截 | 行为级 | ✅ Windows SDK 侧拦截 + `network.allow_hosts`；静态扫描证据缺 |
| SBX-03 死循环超时 | Kill | Kill | Kill | ⚠️ 代码/单测有，无独立运行时留存 |
| SBX-04 内存暴涨 | OOM Kill | OOM Kill | OOM Kill | ❌ 无实机证据 |
| SBX-05 连续 3 次崩溃 | quarantined | quarantined | quarantined | ⚠️ 实现在，**pytest 确定性失败** |
| SBX-06 签名不符 | 拒装 | 拒装 | 拒装 | ✅ 独立探针证实 |
| SBX-07~09 | M3 | M3 | M3 | 不适用（M2 不做系统调用过滤） |

---

## 3. C SDK 第三方上手计时

**严格第三方视角**：只读 `docs/sdk/python-tool.md`（31 行），从零创建插件到实际 MCP 工具调用成功，未看实现源码（除最后一次排查配置）。

| 步骤 | 时间（GMT+8） | 耗时 |
|---|---|---|
| 起算（打开文件、读文档） | 20:24:28 | 0s |
| 创建 `manifest.yaml` + `main.py`（照文档模板） | 20:24:34 | 6s |
| `uh plugin validate` → **拒绝**：`插件源缺少签名`（文档未提示） | 20:24:44 | +10s |
| 尝试 `--no-verify` → **拒绝**：`当前模式禁止跳过插件签名校验`（需 `UH_LOCAL_MODE=1` + `UH_PLUGIN_ALLOW_UNVERIFIED=1`，文档未写） | 20:24:54 | +10s |
| 加本地开发 env 后 validate 通过 | 20:24:56 | +2s |
| `uh plugin dev --once`：initialize + tools/list + tools/call 成功，返回 `ECHO:third-party-ok` | 20:24:56 | <1s |

**总耗时 28 秒**（起止 epoch `1795177468` → `1795177496`），远低于 30 分钟目标。

**文档缺口（新发现，P2）**：`docs/sdk/python-tool.md` 只给了可复制的插件模板，未说明：① 目录源默认 validate 会拒签；② 本地跳过验签需要两个 env + `--no-verify`；③ 正式 `.uhpkg` 签名包如何生成/获取信任。第三方照文档第一次必然卡在 validate——本轮靠遍历 CLI 报错信息才解出。建议文档补「本地开发模式」一节。

---

## 4. D 跨机插件链路（受阻）与安全专项

### 跨机链路

| 链路 | 实测 | 判定 |
|---|---|---|
| 服务器 `GET /health` | 200 | ✅ |
| 服务器 `GET /api/v1/plugins`（带 token） | 200 `{"plugins":[],"total":0}` —— 旧引擎只读端点，无 M2 install/activate/uninstall | ❌ 无法验 |
| 服务器 `GET /api/v1/themes` | 404 UH-USR-1003 | ❌ 无法验 |
| 服务器 M2 模块文件 | 全部不存在（哈希命令仅返回 main.py 旧版 `eb727b9f…`） | ❌ 部署缺失 |
| 跨机签名包 install/activate/uninstall | 无对应端点，未执行 | ❌ 未验 |
| 主题跨机切换 / 钩子跨机审计事件 | 无对应端点，未执行 | ❌ 未验 |

**结论**：brief 的 C 项在服务器当前部署态下不可执行。这不是探针能力问题，是验收前提缺失。

### 安全专项（本机真实运行时）

| 专项 | 实测 | 判定 |
|---|---|---|
| 主题包 JS 注入 | `url(javascript:)` / `expression()` / 分号注入全拒；非 PNG/WOFF2 资产全拒 | ✅ |
| Ed25519 验签 | 未签名、篡改包均 `UH-KRN-2010`；信任库仅认 `trusted_keys` 中的 key_id，包自带公钥不建立信任根（`plugins.py:453-467`） | ✅ |
| 沙箱越权拒绝 + 审计 | 路径/出网越权均 `UH-KRN-2010` 并带 `detail.violation`，日志事件 `plugin.crash` / `plugin.quarantined` 等可见 | ✅ |
| 钩子异常隔离 | handler 抛 `RuntimeError` 后主链路继续，worker 记录 `hook.failed` | ✅ |
| secret 扫描 | `scripts/secret-scan.mjs` 带空格/YAML/引号赋值全识别；文件引用与合成 token 正确豁免；`scanTrackedFiles()` 仓库命中 `[]` | ✅（五审正则漏洞已闭环） |

---

## 5. 新发现（按严重性）

### N1（P0，阻断）服务器未部署 M2，跨机验收前提不成立

`certutil` 对服务器 `D:\uh\kernel\{plugins,sandbox,hooks,themes,plugin_sdk}.py` 全部返回"找不到文件"，`main.py` 哈希 `eb727b9f…` 是五审基线。brief 声明的 195/195 服务器门禁实测为 147/148（旧测试集）。**建议**：部署 M2 六模块 + 同步测试集后重新派发跨机验收；在此之前 MS-3 不得结项。

### N2（P1，阻断）本机 pytest 195/195 不可复现：SBX-05 测试确定性失败

`tests/unit/test_plugins.py::test_plugin_crashes_restart_three_times_then_quarantine`：1.5s 窗口内只崩 2 次（`crash_count=2`）。根因是测试等待时间与进程重启间隔（约 0.5s/次）不匹配，非实现缺失（独立探针可见递增逻辑）。**建议**：改为轮询等待 `crash_count>=3` 或注入可控时钟，不要用固定 sleep；修完重跑全量并更新 brief 基线。

### N3（P1）M2-1 两个点名待补项未接通

`kernel/main.py:333` startup 只注册 retrieval capabilities，不遍历 plugin registry 重激活；`kernel/tasks.py:164` 工具调用只走 MCPHost，不从已安装插件包加载执行。DSH 收尾报告自己也把这两项列为「结项前仍需确认」。**建议**：在启动钩子加 `PluginDriver.records` 中 active 插件的 `loader.activate`；工具调用路径加 plugin registry 查找与 sandbox runner 执行。

### N4（P1）SBX 平台矩阵证据缺口

brief 明确要求「留存 Linux/macOS SBX-01~05 拦截/回收结果 + 错误码 + 审计」及「Windows SBX-04 实机内存限额、SBX-02 静态扫描证据」。本轮只有 Windows SDK 侧路径/出网拦截与代码级 Job Object，无 Linux/macOS 运行时、无实机内存 OOM Kill、无静态扫描拒绝证据。**建议**：补 CI 矩阵（Linux 容器跑 bwrap SBX-01~05）与 Windows 实机 SBX-04 截图/日志。

### N5（P2）SDK 文档缺本地开发模式说明

`docs/sdk/python-tool.md:31` 只说「正式插件包仍需按 M2-1 打包」，未说明目录源 validate 默认拒签、跳过验签需 `UH_LOCAL_MODE=1` + `UH_PLUGIN_ALLOW_UNVERIFIED=1` + `--no-verify`、签名包生成流程。第三方首次 validate 必被拒（本轮实测两次拒绝后才解出）。**建议**文档补「本地开发 / 签名发布」两节，并把计时口径写进 M2-3 任务卡证据。

### N6（P2）M2-6 安装来源与静态扫描证据缺

受信 Git 源实跑安装、目录/市场/Git 三来源端到端、静态扫描越界/危险调用拒绝证据、远端 CI 结果均未提供。现有 CLI 代码与单测覆盖不足以支撑 brief 的 D 项「实跑」口径。

### N7（P2）其余 brief 登记项复核

`web-search` `proxy_required`、主题 REST OpenAPI 未登记、Desktop 同步 M3-6、per-user principal M3——均为 brief 明示「登记不阻断」，本轮无异议。

---

## 6. 探针副作用记录

- 探针 A/B（`%TEMP%\uhm2_probe_{a,b}.py`）：全部在 `tempfile.mkdtemp()` 下运行，`UH_PLUGIN_ROOT` / `UH_PLUGIN_REGISTRY` / `UH_LONG_TERM_STORE` / `UH_RETRIEVAL_STORE` 均指向临时目录，**未触碰 `.uh/` 运行态、未装/卸任何真实插件到生产目录**。
- SDK 计时插件在 `%TEMP%\uhm2_sdk_probe\echo-tool`，仅本地 `dev --once` 子进程，已随临时目录留存于系统 temp（无进程残留）。
- 跨机只读：仅 `GET /health`、`GET /api/v1/plugins`、`GET /api/v1/themes`，未执行任何写操作/清库/重启。
- 未修改源码/契约/服务器配置；探针脚本与临时验证脚本收尾删除；只新增本报告。

---

## 7. MS-3 结项建议与结论

**结论：不通过。签署：Tencent Hunyuan Hy（腾讯混元 Hy）**

不建议 DSH 关闭 #22~#27 或宣告 MS-3 结项。放行路径：

1. **先修复验收前提**：服务器部署 M2 六模块 + 同步 195 测试集；本机修 SBX-05 时间敏感测试并重跑全量 195/195。
2. **补齐 M2-1 两个闭环**：启动重激活、已安装工具从生产目录调用。
3. **补 SBX 平台矩阵证据**：Linux/macOS 运行时日志 + Windows SBX-04 实机（brief D 项硬要求）。
4. **补 M2-6 实跑证据**：三种来源安装 + 静态扫描拒绝 + 远端 CI。
5. **补 SDK 文档缺口**后重新派第三方计时（本轮 28 秒结论是在翻阅 CLI 报错后才达成，不反映文档真实上手成本）。
6. 上述完成后重新派发跨机独立验收（brief 0013 同口径），再走 DSH 审任务卡 → GitHub 关闭 #22~#27 → MS-3 结项 → M3。

**值得肯定**：M2 的信任边界设计扎实（包自带公钥不建立信任根、canonical digest、签名/篡改/越权全有结构化错误码与审计）；钩子异常隔离与主题注入拦截经运行时证实；主题切换 15ms 远优于 200ms 门槛；SDK 核心链路可用。这些是结项前已具备的底子。

---

> 审查主体：**Tencent Hunyuan Hy（腾讯混元 Hy）** · WorkBuddy Agent 模式
> 基线 `9da129f`（brief 指定 `067e7a2`）· .14 → .28:8080 · 2026-11-20
> 本报告不含任何凭据原文；所有实测输出为脱敏摘要。
