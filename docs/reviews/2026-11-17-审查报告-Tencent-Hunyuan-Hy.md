# UH! 项目审查报告（模型署名版）

> **审查模型：Tencent Hunyuan Hy（腾讯混元 Hy）**
> 运行产品：WorkBuddy
> 审查日期：2026-11-17（18:47–19:20 GMT+8）
> 审查对象：`main` 分支 commit `41442bc`（`M1-8 完成 + M1 能力期收官`）
> 报告性质：**非契约、非规范文件**；不修改 `docs/contracts/` 冻结事实源
> 关联详版：`docs/reviews/2026-11-17-审查与实测报告.md`（867 行，含完整输出与复现脚本全文）
> 工作区：审查期间未改动任何受版本控制源码；仅新增本报告与工作记忆

---

## 1. 审查主体与模型信息

| 项 | 内容 |
|---|---|
| 执行模型 | **Tencent Hunyuan Hy**（腾讯混元 Hy） |
| 产品外壳 | WorkBuddy（Agent 模式） |
| 审查方式 | 只读静态审查 + 实际运行门禁 + 5 项运行时复现 |
| 响应语言 | 简体中文；代码、命令、日志保留原文 |
| 环境 | Windows；Node `v22.22.2`；Python 3.14.3（`C:\Python314\python.exe`，pytest 9.2.0） |
| 默认 `python` | WorkBuddy 3.13.12，仅 pip，无 pytest |

本报告中“我”“本次审查”均指由 **Tencent Hunyuan Hy** 驱动、在 WorkBuddy Agent 模式下执行的这次审查。模型型号为按要求显式记录，便于后续追溯审查产物的生成主体。

---

## 2. 结论摘要

### 2.1 总体判断

UH! 的**契约设计成熟度明显高于实现与治理成熟度**。M1 能力期功能骨架已经成形，错误码、事件字典、响应包裹、隐私分级、MCP 抽象等设计考虑周全；但存在三类系统性问题：

1. **治理声称与事实源脱节**：README/PROGRESS 声称契约“已冻结、评审通过、111/111 全绿”，但契约正文仍写“冻结候选 / needs-review”，仓库内没有人工评审记录，也没有可复现的验证输出。
2. **门禁与 CI 名不副实**：默认环境跑 `pytest` 直接报无模块；`verify-all` 因硬编码 `python` 而 B2 失败；CI 只跑 Node 契约校验，不跑 pytest，也不跑 `verify-all`。
3. **实现存在可直接利用的安全与正确性缺陷**：WS 鉴权绕过、确认令牌泄露、确认 TTL 死等、任意路径文件索引外泄、WS 重订阅丢实时事件。

**当前状态不建议对外网暴露，也不宜把“M1 已验收”当作生产可用结论。**

### 2.2 声称 vs 实测

| 项目 | 文档声称 | 本次实测 |
|---|---|---|
| Python 测试 | 111/111（`PROGRESS.md:4,32,209`） | **110 passed / 1 failed**，失败可稳定复现 |
| `verify-all` fast | 全绿 | 默认 PATH **FAIL B2**；指定 Python 后 6/6 |
| `verify-all --full` | 全绿 | 指定 Python 后 8/8（含官方 OpenAPI/AsyncAPI 校验） |
| 高危安全项 | 未提及 | **5/5 运行时复现成功** |
| 契约状态 | 已冻结、评审通过 | 6 份契约正文仍 `needs-review`，无评审记录 |

---

## 3. 门禁实测

### 3.1 pytest

默认 `python`：

```
C:\Users\haoxu\.workbuddy\binaries\python\versions\3.13.12\python.exe: No module named pytest
```

改用 `C:\Python314\python.exe`：

```
......F................................................................. [ 64%]
.......................................                                  [100%]
FAILED tests/unit/test_cli.py::test_up_down_integration - TypeError: argument of type 'NoneType' is not a container or iterable
1 failed, 110 passed, 77 warnings in 31.09s
```

单独重跑仍失败（`1 failed, 15 warnings in 4.50s`），排除偶发。

**根因**：内核其实启动成功（`UH! 内核已起：http://127.0.0.1:59086 （pid 22440）`），但 `kernel/cli.py:121` 用 `subprocess.run(["tasklist", ...], text=True)` 按 UTF-8 解码中文 Windows 的 GBK 输出，读取线程抛 `UnicodeDecodeError`，`stdout` 变为 `None`；`:122` 的 `str(pid) in out` 抛 `TypeError`。

**影响**：`PROGRESS.md` 的 111/111 在本机不可复现；M0-6 “一键启动”跨平台承诺在当前环境未成立。

### 3.2 verify-all

默认 PATH：

```
PASS  A manifest schema（4 正例 + 2 负例 + 入口判别）  | ajv 8.20.0
PASS  B events schema（18 正例 + 7 负例）  | 18+7
FAIL  B2 运行时映射双向校验 + ADR-002 三段命名
PASS  C 交叉一致性预检（8 类）
PASS  D OpenAPI 端点覆盖（19/19）  | 16 路径
INFO  E v3 基线待修订项  | UH-CAP-50210=1, UH-LLM-*=3, UH-KRN-1001=1
VERIFY_FAILED=1
```

`PATH=/c/Python314:$PATH` 后：

```
PASS  B2 运行时映射双向校验 + ADR-002 三段命名  | push/subscribe negative probe
VERIFY_ALL_PASSED (6 checks, fast)
```

`--full`：

```
PASS  F swagger-cli validate（OpenAPI 3.1）  | docs/contracts/openapi.yaml is valid
PASS  G @asyncapi/parser（AsyncAPI 2.6）  | errors=0 version=2.6.0
VERIFY_ALL_PASSED (8 checks, full)
```

门禁自身问题：

- `scripts/verify-all.mjs:65-68` 硬编码 `python`，多 Python 环境结果取决于 PATH。
- `:94-96` 的 E 项恒为 `true` 且 `info=true`，`:121` 过滤 info 项，因此**不可能失败**。
- `docs/contracts/VERIFICATION.md:15-24` 表格列 A~H 共 8 项但漏 B2；`:57` 写 fast 5/5、full+remote 8/8，实际脚本 fast 6、full 8、full+remote 9。
- 本地依赖无版本（`verify-all.mjs:30`），实测 `.work/verify-deps/package.json` 为 `ajv ^8.20.0`、`js-yaml ^5.5.0`；CI 固定 `ajv@8 js-yaml@4`（`.github/workflows/contracts.yml:16`）。

### 3.3 CI 覆盖

`.github/workflows/contracts.yml:4-6` 触发路径仅 `docs/contracts/**` 与脚本自身，**不含 `kernel/**`、`tests/**`**；`:15-26` 只跑 Node 契约校验。改内核或测试不会触发 CI；CI 从不执行 pytest 与 `verify-all`。`AGENTS.md:27` 的“提交前必跑”纯属人工纪律。

---

## 4. 动态复现（5/5 成功）

### R1 — WS 鉴权绕过

设 `UH_WS_AUTH_REQUIRED=1` 且不配任何 token 环境变量，带 `?token=attacker` 连接：

```
RESULT: WS ACCEPTED with arbitrary token 'attacker' (no 4401)
```

`kernel/main.py:504-513`：`expected_token is None` 时任意非空 token 直接置 `authenticated=True`；首帧 `op:auth`（`:578-584`）的 `expected_token is not None and ...` 判断同样失效。与 `openapi.yaml:11-12` 的 bearerAuth 要求相反。

### R2 — 确认令牌泄露

```
HTTP status: 200
Token leaked in response: True
Leaked token prefix: uct-HVz0yHhx...
```

`kernel/confirmation.py:60-74` 的 `to_dict()` 含 `confirmation_token`；`kernel/main.py:460-466` 的 `/api/v1/dev/confirmations` 无鉴权直接返回。匿名调用者可取出 token 后自行批准高危工具。

### R3 — 任意路径索引与外泄

```
Index accepted: True
Marker retrievable via search: True
```

`kernel/retrieval.py:189-201` 的 `_read_document(path)` 直接 `Path(path).read_text()`，无白名单；`:250-252` 工具 schema 只声明 `path: string`；`:278-279` 原样透传。配合无鉴权的 `/dev/tool-task`（`main.py:454-458`）攻击链闭合。

### R4 — 确认 TTL 死等

```
Status after 1.2s TTL: pending_confirmation
Expired resolve raised: UH-USR-1003 confirmation_token 已过期
Waiting future done after expiry: False
Status after expired resolve: pending_confirmation
```

`kernel/confirmation.py:120-126` 过期只标 `rejected` 并抛错，不唤醒 future；`kernel/tasks.py:126` 的 `await fut` 无定时器。全仓库 `set_result` 仅 `tasks.py:154`（用户主动确认）与 `mcp.py:178`（MCP 响应），无 TTL 定时器。终态保证失效。

### R5 — WS 重订阅丢实时事件

```
task status: running seq: 3
initial replay: [1, 2, 3]
seq right after re-subscribe: 4
RESULT: TIMEOUT after 2.0s - live events lost after re-subscribe
```

`kernel/main.py:604-608`：`attach()` 返回新 subscriber，但 `queue_task` 被置 `None` 后未重建；主循环 `:612` 的 `if queue_task is not None and queue_task in done` 永远不成立。`op:auth` 分支（`:585-588`）同类问题。UI 恰好走 query token 且不切任务，所以未暴露。

---

## 5. 静态审查要点

### 5.1 治理与文档

- **冻结状态未回写**：`README.md:18` 与 `PROGRESS.md:80-82` 称评审通过并打 tag；但 `errors.md:3-4`、`events.md:3`、`task-flow.md:3`、`ui-state-machine.md:3`、`sandbox.md:3`、`deploy-hardening.md:3` 全部仍是“冻结候选 / needs-review”；六份修订对照也停在“待评审”；`PROGRESS.md:11-16` 六个 P0 仍“待评审”。
- **无评审记录**：`VERIFICATION.md:3,39-45` 只有评审要点清单；`MS-0-评审预检报告.md:5` 自承“预检不替代评审”。
- **验证产物缺失**：审查开始时 `.work/` 不存在；`VERIFICATION.md:56-57` 只有文字声称，无运行日志留存。
- **状态漂移**：`AGENTS.md:12-14` 停在 M0 进行中、下一步 M0-3/M0-4；`README.md:4,42-44` 停在 MS-0、下一步 P0-4~P0-6；`dsh-codex-collaboration.md:44` 仍写“当前 24 用例”。
- **PROGRESS 自相矛盾**：`:17` M1-9 待开始，`:30` M1-9 已完成；`:51` 未销账待办；状态表缺 M2/M3/R 共 18 项。
- **Issue 39 vs 38**：`issue-map.csv` 39 项，`uh-issues/README.md:6,17` 仍写 38 / M1×8；`ADDENDUM.md:11` 已记录 38→39。
- **旧脚本风险**：`uh-issues/README.md:1471-1472` 内嵌脚本缺 M1-9，`:1510` 回填带 `#` 会重造 `##N` bug；真实 `create-issues.sh:41,79` 已修。
- **tag 计数**：实际 4 个 tag（`contract-v0.1.0`、`contract-internal-v1`、`contract-events-v1`、`m1-v0.1.0`），文档一律称“三冻结 tag”；均为 lightweight，无签名/校验和。
- **未销账裁决**：`ADR-001:26-27` 仍写事件命名“仍待裁决”，但 ADR-002 已 Accepted；A2/A4 无裁决（`基线变更-v3-差异评估.md:39,41`）；`P0-2-修订对照.md:110-114` R6 三项冲突未决。

### 5.2 实现与运维

- **REST 全局无鉴权**：`openapi.yaml:11-12` 声明 bearerAuth，但 `main.py:221-250` 只有 trace/幂等中间件。`/memory/export`（`:372-381`）、`DELETE /memory/long-term`（`:363-370`）、`/dev/tool-task`（`:454-458`）、`/dev/capability/toggle`（`:468-471`）均无鉴权。
- **无界内存增长**：`tasks.py:41`、`main.py:121,292-298`、`event_mapping.py:256-260,274-284`、`confirmation.py:82-83` 均只增不清理。
- **StorageGuard 未执行**：`main.py:180-181` 声明 storage，`protocol.py:122-137` 定义 `check()`，但全仓库无 `guard.check(...)` 调用点。
- **CAP-3001 未进 REST**：只发 WS `task.degraded`（`tasks.py:210`），REST 包裹（`main.py:258-298`）无 `meta.degraded` / error，与 `errors.md:41` 不符。
- **span_id 自链自**：`tasks.py:168` 每次新造 span，`event_mapping.py:277-284` 的 `AuditLink` 直接取外部 span；审计链仅内存，无 180/365 天落盘。
- **异常透传**：`tasks.py:236,281`、`orchestration.py:300,399`、`mcp.py:352`、`llm.py:92-99` 把 `str(exc)` / 上游原文推给客户端，违反 `errors.md:79,89`。
- **隐私缺口**：`long_term_memory.py:194-195` L2 加密失败被 `:219,224` 静默吞；`:60-62` `override` 可绕过分级；`:49-58` 正则漏判纯数字生日/护照号。
- **同步 IO 混入 async**：`long_term_memory.py:189-191,324-328`、`retrieval.py:110`、`orchestration.py:122-142`。
- **巨型函数**：`main.py:119-629` 的 `create_app()` 约 510 行；`orchestration.py:244-318` 与 `:344-405` 重复节点执行逻辑。
- **关闭不释放**：无 shutdown handler；`memory.close()`、`llm.aclose()`、`mcp.close()` 无处调用。
- **dev_reset 危险**：`cli.py:175-182` 用 `r.flushdb()` 清空整库，与 `memory.py:27-28` 的 `uh:mem:st:` 前缀隔离冲突。
- **低危**：宽 except 静默吞、`main.py:53,69` 可变默认参数、`main.py:335-353` 分页 `total` 非全量、`confirmation.py:118-119` 可跳过任务绑定、`_bearer_token` 非标准回退。

### 5.3 测试与依赖

仓库 17 个测试文件、111 个 `def test_` 函数（静态核对与声称数量一致），但关键能力多为 mock/fake：

| 模块 | 测试方式 | 缺口 |
|---|---|---|
| LLM | `test_llm.py:20-24` httpx.MockTransport | 无真实 API 端到端 |
| Redis | `test_memory.py:3-10` fakeredis | 无真实连接/过期/并发 |
| LangGraph | `test_orchestration.py:14-28` dict 节点 | 真实分支可选导入；PostgreSQL checkpoint 未集成 |
| Chroma/LlamaIndex | `test_retrieval.py:13-14` LocalVectorStore | 真实 embedding 基准未集成 |
| MCP | `test_mcp.py:9-48` Mock/InProcess | HTTP transport 无测试 |
| UI 状态机 | `test_event_mapping.py:74-85` subprocess node | 依赖 node 与 cwd；无浏览器集成 |
| WebSocket | `test_websocket.py:29-31` TestClient | 非真实网络；时序 hack 敏感 |

依赖与部署：`pyproject.toml:10-23` 全 `>=` 无 lock；`server-deploy.ps1:10` 不固定版本；`:14` 防火墙规则重复添加；`:15-24` 先杀旧无回滚，仅以端口 Listen 计数代替健康检查，而 `server-m0.md:29` 称“幂等”。

### 5.4 值得肯定

1. `main.py:258-298` 统一 envelope + trace_id 中间件落地完整。
2. `retrieval.py:39-57` 不可信内容包装与 prompt-injection 防护意识。
3. `mcp.py:331-364` 超时取消路径干净（shield + wait_for + transport.cancel）。
4. `long_term_memory.py` 的 L1/L2/L3 分级与 L2 默认不入向量索引。
5. `confirmation.py:115-119,134-146` 一次性 token、任务绑定、审计结构清晰（缺 TTL 自动撤销）。
6. `event_mapping.py:119-140,262-285` 事件 schema 出口强校验与 event_id 连续性检查。
7. `tracing.py` 用 contextvars，`logging.py:54-61` 发射时绑定 trace/span。
8. 零硬编码密钥，构造参数可注入 mock，测试友好性基础好。

---

## 6. 问题清单

### 阻塞级（Blocker）

| ID | 问题 | 证据 | 验证 |
|---|---|---|---|
| B1 | WS 鉴权绕过 | `main.py:504-513,578-584` | 动态 R1 |
| B2 | `/dev/confirmations` 泄露确认 token | `main.py:460-466`；`confirmation.py:60-74` | 动态 R2 |
| B3 | 确认 TTL 死等 | `confirmation.py:120-126`；`tasks.py:126,154` | 动态 R4 |
| B4 | 任意路径索引外泄 | `retrieval.py:189-201,250-252,278-279` | 动态 R3 |
| B5 | REST 全局无鉴权 | `openapi.yaml:11-12` vs `main.py:221-250` 等 | 静态 + R2 |
| B6 | 门禁解释器环境依赖 | 实测；`verify-all.mjs:65-68` | 动态 |
| B7 | CLI 中文 Windows 解码崩溃 | `cli.py:121-122`；`test_cli.py:52-56` | 动态 110/111 |

### 高危（High）

H1 CI 不跑 pytest/verify-all；H2 WS 重订阅丢事件（`main.py:604-608`，动态 R5）；H3 冻结状态未回写；H4 无评审记录；H5 无界内存增长；H6 StorageGuard 未执行；H7 CAP-3001 未进 REST；H8 span_id 自链自；H9 依赖无锁/版本分叉；H10 部署脚本非幂等、无回滚。

### 中危（Medium）

M1 PROGRESS M1-9 状态互斥；M2 三份状态文档漂移；M3 Issue 39 vs 38；M4 4 tag 称“三 tag”；M5 ADR/A2/A4/R6 未裁决；M6 异常消息透传；M7 L2 加密静默吞与 override 绕过；M8 async 混同步 IO、巨型函数、无 shutdown；M9 `dev_reset` flushdb；M10 关键路径仅 mock 覆盖。

### 低危（Low）

宽 except 静默吞；可变默认参数；分页 total 语义；任务绑定可跳过；`_bearer_token` 非标准回退；隐私正则漏判；dev probe 绕过 envelope（已知取舍）。

---

## 7. 建议修复顺序

1. **安全止血**：修 WS `expected_token is None` 分支；REST 加统一鉴权并移除/保护 dev 端点；确认 TTL 到期主动 `set_result("rejected")` 并发终态事件；`index_document` 加目录白名单；`attach()` 成功后重建 `queue_task`。
2. **门禁可信度**：`verify-all` 用可配置解释器；CI 增加 pytest + verify-all 并纳入 `kernel/**`、`tests/**`；修 `cli.py:121` 编码；E 项改为 warn 或移出 fast；补真实后端集成测试。
3. **治理回写**：契约状态改为实际结论；补人工评审记录或撤回“评审通过”；修 AGENTS/README/issue README/协作文档状态；删 PROGRESS 陈旧行；处理 ADR 与 tag 说明。
4. **实现质量**：加 TTL/上限；真正调用 `guard.check`；REST 补 `meta.degraded`；span_id 指向真实审计 span；异常脱敏；修 L2 静默吞与 override；同步 IO 移出 async；补 shutdown；`dev_reset` 改前缀删除；锁依赖、修部署脚本。

---

## 8. 证据索引

- `kernel/main.py:112-116,119-629,221-250,258-298,363-381,460-471,483-513,559-620`
- `kernel/confirmation.py:60-74,103-146`
- `kernel/tasks.py:33-41,54-109,111-155,157-184,342-353`
- `kernel/retrieval.py:39-57,189-220,250-285`
- `kernel/event_mapping.py:119-140,215-235,253-285`
- `kernel/protocol.py:68-90,122-137`
- `kernel/cli.py:92-127,175-182`
- `scripts/verify-all.mjs:30,62-73,89-97,121-123`
- `.github/workflows/contracts.yml:4-6,15-26`
- `docs/contracts/errors.md:3-4,41,79,89,100-107`
- `docs/contracts/events.md:3,55-58,134,142`
- `docs/contracts/VERIFICATION.md:3,15-24,37,39-45,56-57`
- `PROGRESS.md:4,11-32,51,80-82,145,154,190,209`
- `README.md:4,6,18,30-44`
- `AGENTS.md:12-15,27,29`
- `uh-issues/README.md:6,17,1471-1472,1510`
- `uh-issues/ADDENDUM.md:3,11,36-37`

---

## 9. 审查声明

1. 本报告由 **Tencent Hunyuan Hy**（腾讯混元 Hy）在 WorkBuddy Agent 模式下生成，模型型号按用户要求显式记录。
2. 所有实测命令均实际执行；5 项复现均成功；未执行项已在详版附录 C 列明。
3. 本次审查**未修改任何受版本控制源码**；新增文件仅限本报告、详版报告与工作记忆。
4. 本报告为非契约文件，不改动 `docs/contracts/` 中任何冻结事实源。
5. 详版证据、完整命令输出与复现脚本全文见 `docs/reviews/2026-11-17-审查与实测报告.md`。

---

**报告结束。审查模型：Tencent Hunyuan Hy（腾讯混元 Hy）。**
