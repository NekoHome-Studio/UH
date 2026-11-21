# UH! M2 修复后独立回归复核报告（第三方 · 跨机）

> **审查模型：Tencent Hunyuan Hy（腾讯混元 Hy）**
> 运行产品：WorkBuddy（Agent 模式）
> 复核时间：2026-11-20（23:06–23:30 GMT+8）
> 验收机：192.168.0.14 · 目标机：192.168.0.28:8080（`ssh ipc`，`D:\uh`）
> 基线：本轮实测 HEAD **`3354126`**（M2 结项架构缺口补完），对比基线 `9da129f`（我上一轮 M2 独立验收对象）
> 修复提交：`e924de7`（flaky 测试轮询化 / SBX-02 静态扫描 / SDK 文档）→ `3354126`（M2-1 生产调用闭环 + 启动重激活 / M2-2 审计落盘 / M2-3 PYTHONPATH / M2-6 安装来源与静态扫描联动）
> 独立性：未阅读其他模型验收材料；只据代码、契约、本机与跨机真实运行时。
> 性质：非契约、非规范文件；不修改源码/契约/服务器配置，只新增本报告。

---

## 0. 结论摘要

**结论：有条件通过（P1 未闭环；MS-3 仍不建议立即结项）。**

上一轮我给 M2 判「不通过」，列了 7 项发现（N1 服务器未部署 M2、N2 flaky 测试、N3 M2-1 两处闭环、N4 SBX 平台证据、N5 SDK 文档、N6 M2-6 来源证据、N7 登记项）。两小时后基线推进了两个提交，本轮复核结果：

| 上轮发现 | 本轮判定 | 证据 |
|---|---|---|
| N1 服务器未部署 M2 | ✅ **已闭环** | 双端六模块 SHA256 完全一致；服务器 `server-test.ps1` **205 passed**；跨机 `/api/v1/themes` 从 404→200 |
| N2 flaky 崩溃测试 | ✅ **已修** | 改轮询等待；本机该项在 6/7 新 API 测试子集中通过 |
| N3 M2-1 生产调用/重激活 | ✅ **已修** | `plugin_host` 路由 + `restore_active()`；`test_kernel_restart_restores_signed_tool_and_hook` 通过 |
| N4 SBX 平台矩阵 | ⚠️ **部分** | Windows 审计落盘与 KRN-2010 透出已实现并有测试；Linux/macOS 仍无运行时证据 |
| N5 SDK 文档 | ✅ **已修** | 文档 34~96 行补上手/manifest 模板/`--no-verify`/签名算法 |
| N6 M2-6 来源证据 | ⚠️ **部分** | directory/market 端到端通过；**git 端到端本机确定性失败**（`git http-backend` fixture 返回 1） |
| N7 登记项 | ✅ 无异议 | web-search proxy_required / 主题 REST 待评审 / Desktop M3-6 |

**新增阻断级问题（本轮发现）**：

1. **本机 pytest 仍非 195/205 全绿**：`test_signed_install_sources_activate_and_execute[git]` 失败（`git+https://localhost:...` fixture 的 `git http-backend` 返回 1）；实测 **1 failed / 204 passed**。服务器 205/205 是全的，本机不是。
2. **SBX-02 静态扫描可被轻易绕过**（新引入）：直接 `os.system()` 被拒，但 `getattr(os,'sys'+'tem')()`、`import os as o; o.system()`、`from os import system`、`f=eval; f()`、`p=subprocess; p.run()` 全部通过 `validate()`（`static_scan: []`），我已用真实签名包在 install 路径验证。

因此不建议现在关闭 #22~#27；但相比上一轮，只剩两项需要处理，已非常接近 MS-3 出口。

---

## 1. 门禁与部署一致性（亲测）

| 检查 | 上轮 | 本轮实测 |
|---|---|---|
| 本机 pytest | 1 failed / 194 passed | **1 failed / 204 passed in 68.20s**（失败项换成 Git 安装用例） |
| `verify-all.mjs --full` | 8/8 | **8/8 PASS**（A/B/B2/C/D/F/G，E INFO） |
| 服务器 `server-test.ps1` | 147 passed / 1 failed（148 项） | **205 passed in 37.49s** ✅ |
| 双端内核哈希 | 服务器缺 5 个 M2 模块 | **六个模块全部一致** ✅ |

双端 SHA256（`kernel/{main,plugins,sandbox,hooks,themes,plugin_sdk}.py`）：

```
main.py       b9f461092a3742e0498c0fe7c796a5999baa1966b268f84b908f23c8d9643684
plugins.py    8f9027616317d525e178b4d596a7e226354b81ebac546657d3b085e3ed061668
sandbox.py    7fcb6410e1fa33b06f4b9dc62cfe49b858e13661ffccf987c17f55e7fbeb22a0
hooks.py      33ee70748864f90d4008f66b7190c954a4949b13bf71b1ac4ffcee1ccbfc59e
themes.py     7f8f269a56194f6b2b5764d1ee2b29fe855e7c646bd45263fcda50e12204f9be
plugin_sdk.py e0da690ae2b4b555d266fa3f5093792fd053f7913f18bd26ceefdd29dcb06761
（本机与服务器逐一相同）
```

---

## 2. 上轮发现逐项复核

### N1 服务器未部署 M2 —— ✅ 已闭环

- 服务器 M2 六模块全部存在且哈希与本机一致（上表）。
- `GET /api/v1/themes` 从 404 → **200** `{"data":{"active":null,"themes":[]}}`。
- `GET /api/v1/plugins` → 200 `total=0`。
- **跨机信任边界实测**（把 `.uhpkg` scp 到 `D:\uh\` 后用服务器本地路径 install）：

```
未签名包          -> 403 UH-KRN-2010 detail={'violation':'signature_missing'}
签名但公钥非信任  -> 403 UH-KRN-2010 detail={'key_id':'xreview','violation':'signature_trust'}
install 后 plugins total 保持 0（无可信公钥，未实际装载）
```

跨机 install→activate→uninstall 全链因「不能改服务器信任库」的纪律未执行成功路径，只验到信任拒绝边界；成功路径由本机临时信任库的 7/7 新 API 测试覆盖（`test_kernel_restart_restores_signed_tool_and_hook`、`test_signed_install_sources_activate_and_execute[directory/market]` 等）。

### N2 flaky 崩溃测试 —— ✅ 已修

改轮询等待；`test_mcp_tool_removes_capabilities_after_three_child_crashes` 实测通过，断言三次崩溃后 `crash_count==1/2/3`、`tool_host` 每次重建、第三次后 `status=quarantined`、`tool_host is None`、`mcp.tool.brief_echo` 能力注销、`/health` 仍 200。

### N3 M2-1 两个闭环 —— ✅ 已修

- `kernel/main.py` 新增 `plugin_host=lambda name: app.state.plugin_driver.tool_host(name)` 传入 TaskManager；`tasks.py` 在 `host.call_tool` 前 `self.plugin_host(tool_name) or host` 选择插件 host。
- `startup` 新增 `await app.state.plugin_driver.restore_active()`；`shutdown` 新增 `await app.state.plugin_driver.shutdown()`。
- `test_kernel_restart_restores_signed_tool_and_hook` 实测通过：重启前后进程对象不同、新进程存活、hook 重新订阅并在重启后继续把 route 决策审计到 `audit.jsonl`（trace_id 可对照）。

### N4 SBX 平台矩阵 —— ⚠️ 部分

Windows 侧已实现审计落盘与错误码透出：`test_sandbox_denials_return_code_and_persist_correlated_audit` 实测通过，断言越界读/出网返回 `UH-KRN-2010`、`task.failed` 带同码、`plugin-sandbox-audit.jsonl` 内容与预期逐条相等（含 `trace_id`、`permission`、`violation`），且审计中不含越界绝对路径与 `example.invalid`。

Linux/macOS SBX-01~05 仍无运行时环境证据（brief 明写要求留存拦截/回收结果 + 错误码 + 审计）。**这项是 MS-3 唯一还没有证据的硬条件。**

### N5 SDK 文档 —— ✅ 已修

`docs/sdk/python-tool.md:34-96` 补齐：第三方上手步骤、目录结构、manifest.yaml 模板、本地验证 `--no-verify`（含所需 env）、生产 `.uhpkg` 签名算法。本轮再次按文档做第三方视角（前轮已实测 28 秒），文档现在自洽。

小瑕疵：`:106` 仍写「生产能力目录调用闭环与启动重激活见 M2-1 后续」，而本轮已实现，文档滞后（非阻断）。

### N6 M2-6 安装来源 —— ⚠️ 部分

`test_signed_install_sources_activate_and_execute` 参数化 directory/market/git 三来源：directory 与 market 在 6/7 子集中通过；**git 在本机确定性失败**：

```
source = git+https://localhost:50082/fixture.git
assert uh_cli(["plugin","install",source]) == 0  -> assert 1 == 0
captured stderr: subprocess.CalledProcessError: Command ['git','http-backend'] returned non-zero exit status 1
（tests/unit/test_plugins.py:622 serve_git 的本地 CGI fixture）
```

失败在测试 fixture 的 `git http-backend`，非产品代码；但 brief 要求「实跑受信 Git 源安装」，本机证据仍缺。服务器 205/205 说明其环境 git fixture 正常，跨平台测试基础设施不一致。

---

## 3. 本轮新发现

### P1-1（阻断）SBX-02 静态扫描存在多种直接绕过

`kernel/plugins.py:381-421` 的 `_static_scan` 用 AST 匹配有限的函数名集合 `_DANGEROUS_CALLS`，只识别 `ast.Name` 直接调用与 `ast.Attribute` 且 base 是 `ast.Name` 的形式。实测：

```
SCAN| direct os.system:        ['exec_danger:os.system']     ✅ 拦截
SCAN| getattr indirect:        []                            ❌ 漏检
SCAN| aliased import:          []                            ❌ 漏检
SCAN| from import:             []                            ❌ 漏检
SCAN| builtins eval:           ['exec_danger:eval']          ✅ 拦截
SCAN| indirect eval:           []                            ❌ 漏检
SCAN| subprocess via var:      []                            ❌ 漏检
SCAN| network direct:          ['network:socket']            ✅ 告警
```

并用真实签名包在 `validate()` 路径确认：

```
direct  -> REJECTED {'violation':'static_scan.exec', findings os.system}
indirect(getattr) -> validate ACCEPTED, static_scan=[]
```

即：**相同效果的危险调用，换一种 Python 写法即可绕过新增的静态扫描门禁**。签名可信模型下影响受限（需先有可信签名），但 `sandbox.md` §3 把静态扫描列为 Windows M2 的**补偿控制**，且 e924de7 提交说明宣称"validate 拒危险 exec"——实际只是拒了最直白的写法。

**建议**：① 不只做 AST 名字匹配，改用「导入+调用图」分析（追踪 alias、`from` 导入、变量赋值、`getattr` 常量折叠）；② 对无法判定的间接调用采取 fail-closed 告警并要求显式权限；③ 至少补上本轮 5 种绕过的负例测试。

### P2-2（非阻断）SDK 文档残留过期表述

`docs/sdk/python-tool.md:106` 仍写「生产调用闭环与启动重激活见 M2-1 后续」，但 3354126 已实现；建议改「见 `plugin_host` / `restore_active()`」。

### P2-3（非阻断）跨机成功路径未端到端实测

纪律禁止改服务器信任库，因此跨机只验到签名/信任拒绝边界；成功 install/activate/call/uninstall 依赖本机测试替代。若要 MS-3 完全闭环，需由 DSH 在服务器注入测试公钥后重跑一次跨机成功链（探针副作用需清理）。

---

## 4. 探针副作用

- 跨机：把两个测试包 scp 到 `D:\uh\_review_{signed,xreview}.uhpkg`（**服务器文件，我创建了两个临时文件**）；install 均被拒，未装载插件；**未删这两个文件**（无删服务器文件权限/纪律，需 DSH 清理）。除这两个临时包外未改服务器任何配置/信任库/注册表。
- 本机：所有全链/扫描探针在 `%TEMP%\uhm2r_*` 下，插件根与存储均隔离到临时目录；探针进程均正常退出（全链脚本一次 SIGTERM 后改为测试子集验证）。
- 未改源码/契约/服务器配置；只新增本报告。

---

## 5. MS-3 结项建议与结论

**结论：有条件通过。签署：Tencent Hunyuan Hy（腾讯混元 Hy）**

上一轮的 P0（服务器未部署）已闭环，N2/N3/N5 已修，N4/N6 部分修。**剩余两件事需要 DSH 决定**：

1. **修静态扫描绕过（P1-1）**：这是新引入门禁的形式化漏洞，建议补调用图分析 + 5 类负例测试后再结项。
2. **补 Linux/macOS SBX-01~05 运行时证据（N4）**，或由 DSH 在结项材料中明确登记「MS-3 仅 Windows M2 实机 + CI 静态证据，Linux/macOS 留 M3」（brief 原口径要求留存，需明确豁免才能放行）。
3. 本机 Git fixture 失败（N6）需在 CI 或本机复现通过后重跑 205/205；服务器已是 205/205。

若 DSH 接受以上登记并补完静态扫描，即可走 #22~#27 关闭 → MS-3 结项 → M3 生产期。

**肯定**：双端部署哈希一致、服务器 205/205、M2-1 生产闭环与重启恢复有真实测试、沙箱审计带 trace_id 落盘且不泄漏越界目标、跨机签名/信任拒绝边界行为正确——M2 的工程质量已达到接近结项的水平。

---

> 审查主体：**Tencent Hunyuan Hy（腾讯混元 Hy）** · WorkBuddy Agent 模式
> 基线 `3354126`（对比 `9da129f`）· .14 → .28:8080 · 2026-11-20
> 本报告不含凭据原文；探针副作用已逐项登记。
