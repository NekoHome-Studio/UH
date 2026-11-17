# M1 独立审查报告（只读）· DeepSeek-V4-Flash

| 项 | 内容 |
|---|---|
| 审查对象 | M1 能力期：M1-1 ~ M1-9（内部协议 / MCP Host / 检索 / 编排 / 长期记忆 / 响应包裹 / 事件字典 / WebSocket / 高危确认） |
| 审查时间 | 2026-11-17 19:10 ~ 19:25（Asia/Shanghai） |
| 审查者 | DSH 会话 `session-a9dd898c-ce93-426b-a3a3-096edba8ca48` |
| **审查模型** | **deepseek-v4-flash**（provider `deepseek-official`，reasoningEffort `high`；DSH 用量记账名 `deepseek-flash`）· agent preset `standard` |
| 审查方式 | 只读代码走查（kernel/ 全量 3992 行 + docs/contracts 事件与任务流契约 + ui 状态机 + 门禁脚本）+ 门禁复现 + 定向运行时探针 |
| 内容改动 | **零**。`git status` 仅两份原本就存在的未跟踪项（`docs/reviews/`、`.workbuddy/`）；pytest 产生的 `.pytest_cache/`、`__pycache__/`、`.uh/` 为运行副产物 |
| 独立性声明 | 按要求**未阅读** `docs/reviews/2026-11-17-审查与实测报告.md`，本报告结论全部独立得出 |
| 结论 | 骨架成立、分层干净，但**不建议按「9/9 已完成」直接封板**：5 个 P0 契约级缺陷，其中 2 个属于「验收标准写了、实现没有」 |

---

## 0. 门禁复现（审查者本人执行）

| 门禁 | 命令 | 结果 |
|---|---|---|
| 单元测试 | `python -m pytest -q` | **111 passed / 30.47s**（76 warnings，均为 `on_event` 弃用与 fakeredis/mock 回退提示） |
| 契约验证 | `node scripts/verify-all.mjs` | **6 项全绿**：A manifest / B events 18+7 / B2 运行时映射双向 + ADR-002 / C 交叉一致性 / D OpenAPI 19 端点 / INFO E v3 待修订项 |

结论：PROGRESS.md 的「111/111 + verify-all 全绿」是**真实可复现**的，不是纸面数字。问题不在门禁红，而在**门禁覆盖不到的契约面**。

---

## 1. P0 阻断项（违反已冻结契约，修完再封板）

### P0-1 `GET /tasks/{id}/steps` 未实装，4408 回退链路断裂

| 项 | 内容 |
|---|---|
| 证据 | 路由表实测无该端点（仅 `/tasks/{id}`、`/resume`、`/confirm`、`/stream`）；`GET /api/v1/tasks/{id}/steps` 实测 **404 + UH-USR-1003** |
| 契约 | `docs/contracts/task-flow.md` §5.4（超窗回退 `/tasks/{id}/steps`）、`docs/contracts/events.md` §7.3、§7.4（决议 C8：`steps[].seq = step.started 的 event_id`） |
| 前端 | `ui/index.html:106`、`ui/index.html:119` 两处调用，失败被 `.catch(() => null)` 吞掉 |
| 影响 | M1-7 验收项「超 5 分钟窗口回退拉取 /tasks/{id}/steps」**实际未达成**；4408 后 UI 静默丢阶段条；C8 决议无实装载体 |

**修法**：补 `GET /api/v1/tasks/{id}/steps`，`seq` 取对应 `step.started` 的 event_id，返回 `{steps:[{seq,step_id,module,name,status,duration_ms}]}`；终态信息仍走 `GET /tasks/{id}`。

### P0-2 resume 打破「终态唯一且必为最后一个事件」

实测同一 task 的事件序列（workflow 任务跑完后调 `POST /tasks/{id}/resume`）：

```
BEFORE: task.created, step.started×4/step.finished×4, task.finished
resume: 200
AFTER : ... task.finished, task.resumed, task.finished     ← terminal count = 2
```

| 项 | 内容 |
|---|---|
| 证据 | `kernel/tasks.py:285` `resume()` 不校验 checkpoint 状态，对已完成任务照发 `task.resumed` 并重发终态 |
| 契约 | `events.md` §5.2（终态三选一且仅一次、必为最后一个）、`task-flow.md` §3（F4） |
| 加重项 | `tests/unit/test_orchestration.py:76` 把此行为**写成断言**（对已完成任务断言 200）——测试在保护缺陷而非拦截 |
| 影响 | 前端终态吸收逻辑失效、事件流可被无限延长、重放游标语义被污染 |

**修法**：仅允许 `checkpoint.status ∈ {interrupted, running}`；`completed` 返回 409 或幂等空操作。若确需「重跑已完成任务」，那是新 `task_id` 的新事件流，需走契约新增流程。

### P0-3 WS 认证路径下实时投递失效（只回放、不推送）

| 项 | 内容 |
|---|---|
| 证据 | `kernel/main.py:559-620`：`queue_task` 仅在初始 attach 之后创建一次；`op:auth` / `op:subscribe` 分支 `queue_task.cancel(); queue_task = None` 后**再未重建** |
| 实测 | `UH_WS_AUTH_REQUIRED=true`，连接不带 token，首帧 `{"op":"auth","token":"tok"}`：仅收到 `task.created`、`step.started`（回放），随后 20 个 `delta` 与 `task.finished` **全部丢失**；两次 3s 空等；`subscribers=1` |
| 契约 | `task-flow.md` §2（Bearer 经 query 或首条 `op:auth` 携带）——这是生产唯一鉴权方式 |
| 测试缺口 | `tests/unit/test_websocket.py` 只覆盖「不带 token 被 4401 拒绝」，未覆盖「认证成功后能否收到流」 |
| 影响 | 开启鉴权后 UI 拿不到流式增量，M1-7 的核心卖点失效；切换订阅任务同病 |

**修法**：attach 成功后统一 `queue_task = asyncio.create_task(subscriber.get())`；`op:subscribe` 切换任务后同样重建。补一条「auth 后收到 ≥1 个 delta」的回归。

### P0-4 高危确认无超时路径，`pending_confirmation` 永久挂起

| 项 | 内容 |
|---|---|
| 证据 | `kernel/tasks.py:117-131` `decision = await fut`，**无 `wait_for`**；`kernel/confirmation.py:120` 仅在被 `resolve()` 调用时才判过期，且过期直接 `raise`、**不 resolve future**；`confirm_cb` 里 `resolved_by="timeout"` 分支不可达 |
| 实测 | 工具任务 1.2s 后仍 `pending_confirmation`，事件仅 `task.created` + `task.confirmation_required`，永不产生终态 |
| 契约 | `events.md` §4 `resolved_by=timeout`、`task-flow.md` §3（终态必须送达） |
| 影响 | 违反「终态必送达」；WS 客户端永久挂住；订阅者、事件缓冲、future 泄漏。M1-8 的 `resolved_by=timeout` 是死代码 |

**修法**：`decision = await asyncio.wait_for(fut, self.confirmations.ttl_s)`；`TimeoutError` → `resolved_by="timeout"` + `task.cancelled(reason=timeout)`；`ConfirmationManager.resolve()` 对过期令牌同样唤醒 future（而非只抛错）。

### P0-5 RPC 8s deadline 小于编排 4 节点预算，真实 workflow 必被杀

| 项 | 内容 |
|---|---|
| 证据 | `kernel/protocol.py:122` `CapabilityRegistry.call` 默认 `deadline_ms=8000`；`kernel/tasks.py:255` 调 `orchestration.run` 未传 deadline；`kernel/orchestration.py:356` 对**每个节点**单独 `wait_for(8s)`，4 节点串行 |
| 契约 | `events.md` §4 / `errors.md` §3（CAP-5021 的 detail.module=orchestration 含 timeout_ms）；M1-4 要求「每次状态转移带 deadline_ms」 |
| 影响 | 任务级 8s 上限 ≪ 节点预算之和；任一真实 LLM 节点稍慢即 `task.failed(CAP-5021)`，且该路径下已发出的 `step.started` 永远没有配对终态（同时违反 P1-5）。测试全用毫秒级 mock 节点，111 绿灯发现不了 |

**修法**：任务级 deadline 显式传参（如 `UH_TASK_DEADLINE_MS`，默认 ≥ 节点预算之和），节点级取 `min(剩余任务预算, 单节点预算)`；超时统一走一条「step.failed + 终态」路径。

---

## 2. P1 必修项（契约偏差 / 安全面）

| # | 问题 | 证据 | 影响 |
|---|---|---|---|
| P1-1 | **dev 端点无门禁且泄漏确认令牌** | `GET /api/v1/dev/confirmations` 无鉴权返回**所有任务**的 `confirmation_token` + 全量审计（实测可取）；`POST /api/v1/dev/capability/{name}/toggle`、`POST /api/v1/dev/tool-task` 同样匿名可调；`tests/unit/test_confirmation.py:132` 正是靠该端点取 token（漏洞被写成测试用法） | 任何人读 token → `POST /confirm` → 批准高危工具，M1-8 的「人确认」底线在 HTTP 层被绕过 |
| P1-2 | `UH-USR-1002` 拒绝路径 detail 不符 errors.md | `kernel/mcp.py:514` detail=`module/tool_name/call_id`；契约要求 `confirmation_required/tool_name/risk_level`。`task.cancelled` 的 error 干脆不带 detail | 冻结错误码表的 detail 是硬约束，前端照契约取字段会取空 |
| P1-3 | **`UH-CAP-4003` 语义挪用** | `errors.md` 定义 = LLM 上游服务错误（detail: model/provider/upstream_status）；`kernel/mcp.py:352/358/397` 用它表示 MCP 传输/JSON-RPC 错误，`kernel/protocol.py:136` 用它表示任意模块 handler 异常 | 前端按域做文案：工具故障被显示成 LLM 故障。需走「只增」登记 detail.module 用法，或补工具段新码 |
| P1-4 | **18 事件只有 11 个有运行时发射器** | 无发射器：`tool.call`、`tool.result`、`safety.*`(3)、`task.yielded`、`step.failed`（业务链）。`mcp.tool.call/result` 只发在 EventBus（`kernel/mcp.py:497/520`），而 `EventMapper` 只在 `TaskManager._emit` 被调用——**缺 bus→task stream 桥** | UI 状态机 `thinking→acting`、`checking` 相关分支是真实运行中永不进入的死代码；M1-9 验收「18 事件端到端可触发」实际只有 18 个 schema fixture 支撑（`verify-all` B 项校验的是 fixture，不是运行时发射） |
| P1-5 | 业务链失败不留 `step.failed` | `kernel/tasks.py:214` 先发 `step.started`，异常路径 `235-241` 只发 `task.failed` | 违反 `events.md` §5.3 配对约束；前端阶段条卡在 running |
| P1-6 | `POST /tasks/{id}/cancel` 未实装 | `task-flow.md` §4 已冻结形状；路由表无此项 | `task.cancelled` 的 `user_request/timeout/system` 三条 reason 无任何发射器 |
| P1-7 | 幂等只防「重放」不防「并发」，且无 TTL/容量上限 | `kernel/main.py:228-298` 先查 cache 再执行、响应生成后才写；`cache_key=(method,path,key)` 不含 body | 两个并发同 key 请求都会真执行；同 key 不同 body 回放第一次结果；进程内 dict 单调增长 |
| P1-8 | `StorageGuard` 未接线 | 全仓 `guard.check` 只出现在 `tests/unit/test_protocol.py:109-113` | M1-1 任务项「禁止模块直连其他模块存储的**静态检查**」只交付了类，主链路零调用点，验收为空 |
| P1-9 | `stream_url` 仍是 SSE | 实测返回 `/api/v1/tasks/{id}/stream` | `task-flow.md` §1 形状为 `wss://…/api/v1/stream?task_id=`；F2 仅允许 M0 过渡，M1-7「统一 WS」未改句柄 |
| P1-10 | 长期记忆默认**跨会话**召回 | `kernel/long_term_memory.py:261` `session_id` 默认 None；`tasks.py` 传入的 `context.session_id` 通常为空 | A 会话 L1 记忆注入 B 会话 prompt。单用户本地或可接受，但需契约裁决「长期记忆是全局还是按会话」，现在两边都没写 |

---

## 3. P2 建议项（质量 / 演进）

1. **checkpoint_id 每节点重新生成**（`kernel/orchestration.py:266` 等）：`SQLiteCheckpointStore.save` 名义 upsert 实际每次 INSERT 新行；`load(task_id)` 靠 `updated_at` 取最新 → checkpoint_id 非稳定身份，按旧 id resume 读到旧快照，表随节点数线性增长。
2. **LangGraph 后端未被 CI 覆盖且答案可能丢**：`_run_langgraph` 返回 `state.get("answer")`（原始 dict），节点写的是 LangGraph 传入的 `graph_state` 副本；安装 langgraph 即自动切后端，`final_result` 可能为空。当前仅 fallback 后端被测。
3. **编排降级路径把用户输入当答案**：`kernel/tasks.py:265-270` 流式 delta 收了却丢弃，`final_result.content = content`（用户原话），并把同一段文本以 assistant 身份写入长期记忆。
4. **无 TTL 的内存增长**：`TaskManager.tasks`、`event_buffer`、`EventMapper` 的 4 个索引均无上限；`events.md` §7.2 说「持久化至任务 TTL」——TTL 不存在。
5. **可观测性**：`EventBus.publish` 用 `except Exception: pass` 吞掉订阅者异常（`kernel/protocol.py:70`），无日志；任务级失败无独立结构化日志。
6. **中间件隐式契约**：只有 body 已含 `{data,meta,error}` 的 dict 走规范化，其余 JSON 一律 `{data: payload}`（`kernel/main.py:267-285`）。建议写进契约或改显式装饰器。
7. **门禁自身瑕疵**：`scripts/review-precheck.mjs` 有 `const evMdTable = [...errorsMd.matchAll(/x/g)]; // placeholder no-op` 死代码；内部事件 allow-list 混入两段名（`route.decided`/`request.received`…），而 `validate_event_name` 只收三段——门禁比运行时宽松，「校验看似更全其实不对齐」。
8. **测试取向**：M1 新增用例偏 happy path + 契约形状；缺 WS 实时增量、确认超时、并发幂等、tool/safety 事件生产链路。建议每条 P0 补一条「先红后绿」的回归。

---

## 4. 做得好的地方（别改坏）

1. **适配层隔离**贯彻干净：MCP / Chroma / LangGraph / Postgres 的第三方对象没有一个穿透到内核接口，换实现不动业务。
2. **EventMapper 三件事处理得细**：event_id 连续无空洞的前置校验、审计专用事件不消耗外部序号、`span_id` 双向索引 + `AuditLink` —— 审计反查链是设计亮点。
3. **WS subscribe 竞态处理正确**：无 await 注册 + 快照，「回放与实时之间不丢事件」这个坑绕过去了；终态只推最后终态事件符合 §5.3。
4. **隐私取舍敢于定**：L3 拒入 + L2 加密且**明确不入向量库**（防语义泄露），并用断言锁住该决定——M1 里最有价值的决策。
5. **门禁真实可跑**：审查者本人复现 111/111 与 verify-all 6 项，非纸面数字。

---

## 5. 修复顺序建议

```
1) P0-1 steps 端点（前端 + C8 载体，最小改动、最大收益）
2) P0-4 确认超时（wait_for + resolved_by=timeout）
3) P0-5 deadline 分层（任务级预算显式传参，节点级取 min(剩余, 单节点)）
4) P0-3 WS 认证后重建 queue_task（一处 ~10 行）
5) P0-2 resume 状态校验（一条 guard + 改测试断言）
—— 以上 5 条修完再谈封板 ——
6) P1-1 dev 端点门禁（UH_DEV_MODE 默认关；测试改直接调 manager，不依赖 HTTP 泄漏 token）
7) P1-4 bus→task stream 桥（补 tool.call/result 发射；safety.* 要么接生产，要么在契约标「预留」）
```

---

## 附录 A · 复现命令

```bash
# 门禁
python -m pytest -q
node scripts/verify-all.mjs

# 路由表（确认无 /steps、无 /cancel）
python - <<'EOF'
from kernel.main import create_app
app = create_app()
for r in app.routes:
    p = getattr(r, "path", None)
    if p: print(sorted(getattr(r, "methods", None) or ["WS"]), p)
EOF

# resume-after-terminal（P0-2）
python - <<'EOF'
import time
from fastapi.testclient import TestClient
from kernel.main import create_app
app = create_app()
with TestClient(app) as c:
    sid = c.post("/api/v1/sessions").json()["data"]["session_id"]
    h = c.post(f"/api/v1/sessions/{sid}/messages", json={"content": "计划", "mode": "workflow"}).json()["data"]
    tid = h["task_id"]
    with c.stream("GET", h["stream_url"]) as r: "".join(r.iter_text())
    c.post(f"/api/v1/tasks/{tid}/resume", json={}); time.sleep(1)
    print([e["event"]["event"] for e in app.state.tasks.tasks[tid]["event_buffer"]])
EOF
# → [..., 'task.finished', 'task.resumed', 'task.finished']

# WS auth 后实时投递（P0-3）：设 UH_WS_AUTH_REQUIRED=true / UH_WS_AUTH_TOKEN，连接不带 token，
# 首帧 {"op":"auth","token":...}；预期收到 task.created + step.started 后无任何事件（delta 全丢）

# 确认无超时（P0-4）：建工具任务后不确认，1.2s 后 status 仍 pending_confirmation
```

## 附录 B · 与 PROGRESS.md 的差异点

| PROGRESS 声明 | 审查实测 |
|---|---|
| M1-7「超 5 分钟窗口回退拉取 /tasks/{id}/steps」✅ | 端点不存在，404 |
| M1-7「18 事件」✅ | 运行时发射器 11 个（缺 tool.call/result、safety.*、yielded、业务链 step.failed） |
| M1-9「18 事件端到端可触发」✅ | 仅 18 个 schema fixture 可校验，非运行时发射 |
| M1-8「resolved_by timeout」✅ | 无超时路径，`pending_confirmation` 永久挂起 |
| M1-4「deadline 超时由内核取消」✅ | 节点级有，任务级 8s 会被 RPC 层抢先杀掉 |
| M1-1「禁止模块直连其他模块存储的静态检查」✅ | `StorageGuard` 未接入任何生产调用点 |
| resume 验收 ✅ | resume 打破终态唯一性（实测两个终态事件） |

> 说明：上表不是「造假」，而是**验收口径与实现覆盖面的错位**——多处验收由 fixture/单测而非生产链路满足。建议 M1 二次验收时逐条按「生产链路可观测」重新签核。
