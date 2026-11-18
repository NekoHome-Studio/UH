# M1 三审验收报告（独立第三方 · 生产链路口径）

> **签署模型：Tencent Hunyuan Hy（腾讯混元 Hy）**
> 运行产品：WorkBuddy（Agent 模式）
> 审查日期：2026-11-18（18:12–18:25 GMT+8）
> 依据：`briefs/0010-m1-third-review-acceptance.md`
> 基线：commit **`cb4d1e6`** / tag **`m1-review2-fixed-v0.1.0`**；实测 HEAD **`8a7d1c5`**（与基线仅差 brief 文件，代码一致）
> 角色：**独立验收审查者**（非实现方 DSH）；未阅读其他审查者的三审报告，仅以一审/二审报告与修复回应作基线对照
> 报告性质：非契约、非规范文件；**未修改任何受版本控制源码/契约，只新增本报告**

---

## 0. 验收结论

**结论：通过（有条件）**。

- 门禁亲测全绿：pytest **142/142**，verify-all full **8/8**。
- brief 的 9 项生产链路范围逐条实测，**8 项完全通过，1 项（UI）以静态 + 状态机运行时通过**（无浏览器环境，未做真机点击）。
- 二审发现的阻塞/高危项在真实生产配置下已关闭；我额外跑了多组绕过回归（别名、默认豁免、fail-closed、Host 注入、幂等并发、跨模块隔离、L3 覆盖、归属校验），未发现可利用的新绕过。
- 保留条件：①无浏览器真机验证 UI 点击；②单共享 token 的 per-user 隔离属 brief 明示的已知设计局限；③少量非阻塞 observability/脱敏残留（见 §4）。

**签署：Tencent Hunyuan Hy · 2026-11-18**

---

## 1. 门禁复现（亲测）

```
"/c/Python314/python.exe" -m pytest -q
-> 142 passed, 110 warnings in 38.89s

PATH="/c/Python314:$PATH" node scripts/verify-all.mjs --full
-> VERIFY_ALL_PASSED (8 checks, full)
   PASS A / B / B2 / C / D / F / G   INFO E
```

探针纪律：所有运行时探针先把 `UH_LONG_TERM_STORE`、`UH_RETRIEVAL_STORE`、`UH_CHECKPOINT_SQLITE_PATH` 指向系统临时目录（`tempfile.mkdtemp`，每次独立），未触碰 `.uh/` 运行态；探针结束后删除临时脚本。

---

## 2. 生产链路 9 项逐条结论

### 2.1 鉴权（通过）

配置矩阵实测：

| 场景 | 结果 |
|---|---|
| 单设 `UH_AUTH_REQUIRED=1`（无 `UH_LOCAL_MODE`）→ 无 token POST `/api/v1/sessions` | **401**，401 响应 `meta.trace_id` 非空 |
| 生产配置下 `/api/v1/sessions`、`/api/v1/memory/export`、`/memory/export`、`DELETE /memory/long-term`、`/memory/short-term`、`/plugins`、`/api/v1/plugins`、`/api/v1/dev/confirmations` 无凭据 | 全部 **401** |
| ops `/health` `/ready` `/metrics` `/` 与静态 `/static/state-machine.js` | **200**（豁免正确） |
| `?token=valid` 查询回退 | **201** |
| 设 `UH_AUTH_REQUIRED=1` 但不配期望 token，任意 token | **401**（fail-closed） |
| `UH_LOCAL_MODE=1` 本地豁免 | **201** |

结论：N1（默认豁免）与 N2（别名绕过）已修，401 trace_id（brief R2）具备。

### 2.2 WS 实时流（通过）

- 带 token 连接 `/api/v1/stream`，收到 `task.created → step.started → delta×6`，`event_id` 为 `[1..N]` 连续。
- 断线按 `after_event_id=1` 重连，首帧为 `event_id=2`，无丢事件。
- 运行中任务 `cancel` 后，通过订阅队列实时收到 `step.failed → task.cancelled`，验证 step 闭合（brief R7）。
- 跨归属：另一 `client_id` 订阅他人任务 → 收到 `error / UH-USR-1002`（生产共享 token 模型下的结构性守卫）。
- 换 `op:auth` token 后仍通过原归属校验，未越权。

### 2.3 工具确认 inline（通过）

经真实 `MCPHost` + REST `/confirm` 全链路：

| 路径 | 结果 |
|---|---|
| pending → approved | `pending_confirmation` → **finished**，`host calls=1`，audit `approved/user` |
| pending → rejected | **cancelled**（契约要求），未执行工具 |
| TTL 超时（直接构造 `ConfirmationManager(ttl_s=1)` + 真 MCPHost） | **cancelled**；事件序列 `confirmation_required(resolved_by=timeout) → task.cancelled(reason=timeout)`；audit `rejected/timeout` |

注：`ConfirmationManager` TTL 固定 300s，无环境变量覆盖；探针通过构造参数注入短 TTL，**非缺陷**。

### 2.4 工具确认 RPC（通过）

manifest 标 `irreversible` 的 `index_document` 经 `register_capabilities` → `CapabilityRegistry.call`：

```
RPC degraded: True
RPC error_code: UH-USR-1002
工具执行: False
风险级降级尝试（read-only）: 被拒 UH-USR-1002
```

N3 fail-closed 与 GPT-6 风险降级均通过。

### 2.5 取消（通过）

运行中 `POST /cancel` → HTTP 200、`status: cancelling` → 终态 `cancelled`；实时订阅收 `step.failed` + `task.cancelled`；第二次 cancel → **409**；终态 resume → **404**（任务不存在路径，非 409；见 §4 非阻塞项）。

### 2.6 超窗回退（通过）

把首事件 `recorded_at` 回拨 301s 后重连：

```
首帧: replay_unavailable / window_expired
关闭码: 4408
GET /tasks/{id}/steps: 200，data.task_id 存在，steps 非空
```

### 2.7 隔离/隐私（通过）

| 项 | 实测 |
|---|---|
| 幂等跨客户端 | 同键不同 `X-UH-Client-ID` 不复用（共享 token 模型下按调用者区分）；回放响应无 `Set-Cookie`（首次 `callerA`，回放 `callerB`） |
| 幂等并发 in-flight | 同键并发两请求返回同一 task_id（去重生效） |
| StorageGuard 跨模块 | `access("llm.generate","retrieval")` → `PermissionError UH-KRN-2010` |
| StorageGuard 命名空间冲突 | `declare("orchestration.run", {"retrieval"})` → 拒绝 |
| L3 拒入（含 override） | 身份证内容 + `privacy_level="L1"` → 拒入，日志 `reasons:[id_card]` |
| L2 加密不语义索引 | L2 条目存在；`recall` 语义行不含 L2；`export` 默认脱敏 |
| 无痕不写 | 未在探针中单独跑（见 §4） |
| trace 嵌套 | `outer → inner → outer`（token reset 正确） |
| `limit=0` | 返回 0 条 |
| checkpoint_id | 6 次保存、1 个唯一 ID（per-task 稳定） |
| PG 配置回退 | `store: SQLiteCheckpointStore`（并带 WARN，符合修复声明） |

### 2.8 UI（通过：静态 + 运行时状态机）

无浏览器真机环境，采用静态方法核验 + Node 运行 `ui/state-machine.js`：

- UI 静态检查全部命中：确认弹窗与 `/confirm` 调用、重连/退避、4408 渲染 steps、4401 正确报错、degraded 徽标、`resp.ok` 检查、`JSON.parse` try/catch。
- 真实确认事件序列运行时迁移：
  `task.created→routing → step.finished(router)→thinking → tool.call→acting → confirmation_required→awaiting_user → confirmation_resolved(approved)→acting → tool.result→thinking → delta→streaming → task.finished→idle/terminal`。
- UI `index.html:52` 引用 `/static/state-machine.js`；该路径在鉴权豁免清单内，生产模式下实测 **200**，不会出现脚本加载失败。
- 缺项：`aria-live` 未设置（可访问性细节，非 brief 范围）。

### 2.9 门禁与治理（通过）

- CI 已含 pytest + verify-all 双 job；触发路径含 `kernel/**`、`tests/**`。
- 契约头已回写 `frozen`；AGENTS/README/PROGRESS 阶段对齐 M1 完成。
- errors.md 有效码计数与预检标签一致（verify-all C 通过）。

---

## 3. 二审发现逐项判定

| 二审编号 | 判定 | 关键证据 |
|---|---|---|
| N1 REST 默认豁免 | 已修 | 单设 `UH_AUTH_REQUIRED=1` → 401 |
| N2 别名绕过 | 已修 | 全部 `/memory/*`、`/plugins` 别名 401 |
| N3 RPC 绕确认 | 已修 | RPC `UH-USR-1002` fail-closed，工具未执行 |
| N4 Host 注入 | 已修 | Host=evil → `stream_url` 为相对路径；`UH_PUBLIC_BASE_URL` 生效 |
| N5 幂等 Cookie / 跨客户端 | 已修 | 回放无 Set-Cookie、跨客户端不复用、并发去重 |
| N6 dev 门禁 | 已修 | `UH_DEV_MODE=0` → dev 端点 404；默认关 |
| N7 StorageGuard 空操作 | 已修 | 跨模块 `PermissionError`、命名空间冲突拒绝 |
| N8 WS 换 token 归属 | 已修 | 跨归属订阅 4403/USR-1002 |
| N9 confirm_timeout 跨轮 | 已修 | 每轮 `confirm_timeout` 重置（`tasks.py:142`） |
| N10 ADR-003 覆盖/兜底码 | 已修 | verify-all C/B2 含契约校验；RPC `error_mode` 覆盖路径在位 |
| N13 trace/limit=0/L2/checkpoint/PG/stderr | 已修（stderr 为静态确认） | 见 §2.7 |
| N14 UI | 已修（静态+状态机） | 见 §2.8 |

---

## 4. 新发现与残留（均非阻塞）

1. **终态 resume 返回 404 而非 409**（`main.py:510` 附近）：终态任务 `POST /resume` 经 `_task_guard` 命中“任务不存在”语义返回 404；契约/openapi 已登记 409 Conflict。brief R3 要求“终态再 cancel/resume → 409”。cancel 已 409，resume 未对齐。建议核对 `ResumeConflictError` 是否被 guard 前置吞掉。
2. **dev 路径仍回显确认 token 前 6 位**（`main.py:524-530`）：完整 token 已脱敏，但 `tok[:6]` 仍出现。限于 dev 端点且 dev 默认关，非生产暴露面。
3. **CAP-3001 无结果经 RPC 返回时在 payload 内**（非 `error_code` 字段）：`mcp.tool.search` 无结果返回 `data.degraded=true` + `error.code=CAP-3001`，但 `CapabilityRegistry` 的降级信封字段为空。属观测口径差异，不影响功能。
4. **无痕模式未单独动态验证**：探针覆盖了 L3/L2，未单独跑 `memory_recall=False`（静态代码路径清晰，M1-5 已有单测）。
5. **可访问性**：`#chat` 无 `aria-live`，动态回复不被读屏播报。
6. **`aria-live` 与浏览器真机 UI 点击**：无浏览器环境，按 brief 允许的静态 + 状态机口径验收；建议 M2 前补一次真机 smoke。

---

## 5. 探针副作用记录

- 所有探针存储指向 `C:\Users\haoxu\AppData\Local\Temp\uh3_*` 独立临时目录，变量设置后创建 app/Client。
- 探针中 `cancel`、`confirm`、`tool-task` 会写入临时 long-term / retrieval / checkpoint 文件；未触碰仓库 `.uh/`。
- 临时脚本 `uh3_probe_auth.py`、`uh3_probe_ws_confirm.py`、`uh3_probe_confirm2.py`、`uh3_probe_confirm3.py`、`uh3_probe_timeout.py`、`uh3_probe_misc.py`、`uh3_probe_cancel_events.py`、`uh3_probe_final.py` 位于系统 Temp，审查结束后删除。
- 未产生残留服务进程；未修改任何受版本控制文件（除本报告）。

---

**验收结论：通过（有条件）。签署 Tencent Hunyuan Hy（腾讯混元 Hy）· 2026-11-18。**
