# M1 五审（再审）报告 · DeepSeek-V4-Flash

| 项 | 内容 |
|---|---|
| 审查性质 | 四审（跨机，结论"不通过"）后的**复验**，重点验 P0 是否闭环、修复是否引入回归 |
| 基线 | `d23d027`（四审审查点）→ 复验点 **`3031015`**（四审 P0/P1 修复 + 修复报告 + 交接文档） |
| 跨机拓扑 | 本机 `192.168.0.14`（Python 3.14.3）→ 服务器 `192.168.0.28:8080`（Python 3.12.10 venv，生产鉴权模式 B） |
| 审查时间 | 2026-11-18 21:32 ~ 22:25（Asia/Shanghai） |
| 签署模型 | **deepseek-v4-flash**（provider `deepseek-official`，reasoningEffort `high`；记账名 `deepseek-flash`）· preset `standard` |
| 结论 | **通过（附条件）** —— 四审 P0 已实证闭环，跨机链路无回归；4 项条件建议 M2 前收敛 |

---

## 1. 四审 P0 复验：**已闭环**

| 四审要求 | 复验证据 | 判定 |
|---|---|---|
| 令牌轮换 | 旧令牌 `GET /api/v1/plugins?token=<old>` → **401**；新令牌 → **200** | ✅ |
| 令牌出仓库 | `git grep <old>` 工作树 **0 命中**；文档改为 `UH_AUTH_TOKEN_FILE=D:\uh\.auth-token` 引用 | ✅ |
| 令牌不进历史（新增） | `git log --all -S<new>` **无命中**（新令牌从未入库） | ✅ |
| 令牌不进日志 | 服务器 `kernel.out.log` / `kernel.err.log` 中 `token=` 命中数 **0 / 0**（此前 97+）；启动参数已加 `--no-access-log` | ✅ |
| 密钥扫描门禁 | `scripts/review-precheck.mjs` 新增检查 9「密钥扫描（无明文 token 入仓库）」 | ✅ |
| 旧令牌历史残留 | `d23d027` 仍在历史中，但令牌已失效 → 可接受（轮换即吊销） | ✅（登记） |

> 剩余提醒：`.work/probe-accept-cross.py` / `.work/probe-p02-cross.py` 内嵌了**新**令牌明文（gitignored，不入库）。建议改为从 `.work/server-auth-token.txt` 读取，避免本机磁盘再留副本。

---

## 2. 门禁复现

| 门禁 | 位置 | 结果 |
|---|---|---|
| `python -m pytest -q` | 本机 | **148 passed** |
| `node scripts/verify-all.mjs --full` | 本机 | **8/8 PASS** |
| `server-test.ps1` | 服务器 | **147 passed / 1 failed** ⚠️ 见 §5-1 |
| `node scripts/verify-all.mjs` | 服务器 | **FAIL ×2（B2、C）** ⚠️ 见 §5-2 |

---

## 3. 跨机生产链路复验（新令牌）

| 组 | 结果 |
|---|---|
| 免鉴权面 `/health` `/ready` `/metrics` `/docs` `/openapi.json` | 5/5 200 |
| 无凭据业务端点（含 `/plugins`、`/memory/export`、`/memory/short-term`） | 5/5 401 + `UH-USR-1001` |
| 建会话 / 发消息 / `stream_url` 绝对 ws / `meta.trace_id` | 4/4 |
| 跨机 WS | 单连接 **117 事件**，`event_id` 连续，终态 `task.finished` 送达，终态重连只推终态 |
| 工具确认（批准/拒绝） | `confirmation_required → 批准 → confirmation_resolved → tool.result → task.finished`；拒绝 → `task.cancelled` |
| 取消 / steps / 409 | `status=cancelling` → 终态 `cancelled`；`/steps` 含 `task_id`；终态 `resume` → **409** |
| 幂等 | 同键同 body 回放同一 `task_id` |
| 路径白名单 | `C:\Windows\win.ini`、`D:\uh\pyproject.toml` 均拒 |

探针 `cross.py` 本轮原始输出 26/30，4 项失败经**定点复测**确认全部为探针口径问题（见 §6），产品行为正确。

---

## 4. 本轮修复的实际验证（不止看 commit message）

| 修复项 | 验证方式 | 结果 |
|---|---|---|
| `UH_AUTH_TOKEN_FILE` 支持 | 服务器 env 已改文件引用，业务请求 200 | ✅ 生效 |
| SSE `/tasks/{id}/stream` 归属守卫 | 无 cookie 的裸客户端请求 → **403 `UH-USR-1002`**，响应带 envelope | ✅ 生效 |
| `owner_matches` 双因子 | token-only 的 WS → `4403`；`token+client_id` 或 `token+Cookie` → 正常收流 | ✅ 生效（语义变更见 §5-3） |
| `CAP-4004` 参数类错误不可重试 | 路径越界现返回 `UH-USR-1003` + `retryable:false` + `detail.upstream_code=-32602` | ✅ 生效 |
| `UH_RETRIEVAL_ROOT` + `data/` 目录 | 服务器 `TEST-PATH D:\uh\data` = True，env 已设 | ✅ 生效 |
| span 溯源（总线自动生成 span_id） | 代码核对 + 事件流 `span_id` 非空 | ✅ |
| 终态释放 delta 缓冲 | 代码核对（终态时从 `event_buffer` 剔除 delta，保留结构事件） | ✅（未做内存实测） |

---

## 5. 仍存在的条件项

### §5-1 服务器测试 147/1：测试文件未随部署同步（P2·流程）
`tests/unit/test_websocket.py` 本机与服务器 SHA256 不同；失败用例正是被双因子修复**改过语义**的
`test_websocket_bearer_token_is_sufficient_owner_identity`。即服务器跑的是旧测试，属**假失败**。
建议：部署脚本除 `kernel/`、`ui/` 外同步 `tests/` 与 `docs/contracts/`，或在服务器侧只跑"部署相关"子集并显式标注。

### §5-2 服务器契约门禁 B2/C 失败（P2·流程）
直接原因已定位一半：服务器缺 `uh-issues/` 与 `PROGRESS.md`，而 `review-precheck.mjs` 会 `walk("uh-issues")` 并读 `PROGRESS.md` → C 失败；B2 在 `python -c` 可正常导入的前提下仍失败，未定位到根因（疑似 node `execSync` 下的解释器/路径差异）。
建议：`verify-all` 在失败分支打印子进程 stderr（现在被 `String(e.stdout || e.message).slice(0,200)` 吞掉），并把"仓库完整性检查"与"运行时必需检查"分级。

### §5-3 双因子归属改变 WS 契约语义，但契约未同步（P1·文档）
`task-flow.md §2` 仍写「Bearer token 经 query 参数 token 携带」即为鉴权/归属依据；实测**仅 token 订阅 → 4403**，必须额外带 `client_id`（或 Cookie）。既有第三方客户端会因此断流。
建议（只增）：在 §2 增加一行「归属 = token + client_id 双因子；`client_id` 可经 Cookie、`X-UH-Client-ID` 头或 `?client_id=` 传入」，并标明 deprecated 过渡策略。

### §5-4 部署形态三项（P1，建议 M2 前收敛）
1. 服务器仍 `UH_DEV_MODE=1`（`/dev/tool-task`、`/dev/confirmations` 对持 token 者开放）。
2. 仍为明文 `ws://` + `?token=`：访问日志已关，但局域网嗅探/浏览器历史仍会暴露主令牌；建议 TLS 反代 + WS 短时票据。
3. `/metrics`、`/docs`、`/openapi.json` 免鉴权（局域网可读），建议写入 `deploy-hardening.md` 豁免清单或收紧。

---

## 6. 探针口径问题（非产品缺陷，已定点复测）

| 探针项 | 现象 | 根因 | 定点复测 |
|---|---|---|---|
| D3 终态送达 | 报 `['delta']` | 探针在既有任务上复用了旧连接时序 | 新建任务：117 事件、终态 `task.finished` ✅ |
| D4 终态重连 | 返回 `task.created` | 同上（连接时任务未终态） | 任务终态后重连：只推终态 + close 1000 ✅ |
| E3 批准链 | 只见 `task.finished` | 批准后才重连 → 命中"终态只推终态"设计 | **批准期间保持 WS 连接**：`created→tool.call→required→resolved→tool.result→finished` ✅ |
| F4 终态 resume | 404 | 该任务为 chat（无 checkpoint）且尚未终态 | chat 无 checkpoint → 404；workflow 终态 → 409 ✅ |

---

## 7. 结论

**通过（附条件）。**

四审唯一的阻断项（令牌进入公开仓库 + 落日志）已**实测闭环**：旧令牌失效、工作树无残留、新令牌从未入库、日志零命中、并补了密钥扫描门禁。跨机生产链路（鉴权矩阵 / WS 实时与回放 / 确认 / 取消 / steps / 409 / 幂等 / 路径白名单）在新令牌下全部正常，未见修复引入的功能回归。

**四项条件（不阻断 M1 出口，建议 M2 前完成）**
1. `task-flow.md §2` 补 `client_id` 双因子归属（只增），避免第三方客户端断流。
2. 服务器 `UH_DEV_MODE=0` 复跑一次确认链路后固化。
3. TLS 反代 + WS 短时票据（消除 URL 携带主令牌）。
4. 部署脚本同步 `tests/` 与 `docs/contracts/`（或分级门禁），并让 `verify-all` 失败时打印子进程 stderr。

**签署**：`deepseek-v4-flash` · 2026-11-18

---

## 8. 副作用记录

- 未修改服务器文件、未 scp、未重启；仅创建任务/事件流（运行态）与读取配置。
- 本机新增/修改仅 `.work/probe4/cross.py`（gitignored，为适配双因子已补 `client_id`）。
- 报告中不含任何令牌原文；旧令牌与当前令牌均以 `<old>/<new>` 代称。
