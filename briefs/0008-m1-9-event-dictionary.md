# Brief 0008：M1-9 事件字典冻结 + 总线映射 + UI 状态机（M1-7 前置）

## 角色与约定
你是实现者（Codex），在 UH! 仓库（Python 3.12 + FastAPI + 原生 JS UI）工作，先读 AGENTS.md。只改代码与测试，**不要 git commit / 不要打 tag**（DSH 审查后提交并打 contract-events-v1）。

## 目标
把 P0-3 已交付的事件契约（events.md / events.schema.json / ui-state-machine.md）实装为运行时：内部→外部事件映射 + UI 状态机 + CI 双向校验 + 审计反查。冻结后 M1-7 WS 才能基于稳定字典实装。

## 必读上下文（仓库内）
- AGENTS.md；docs/contracts/events.md（18 外部事件 + §6 内部→外部映射表 + 信封 event_id/event/version/task_id/trace_id/span_id/ts/payload）；docs/contracts/events.schema.json（载荷判别）；docs/contracts/ui-state-machine.md（10 状态转移表）。
- docs/contracts/adr/ADR-002-event-naming-three-segment.md（内部事件三段命名）。
- kernel/protocol.py（EventBus/Envelope，bus_seq）；kernel/mcp.py / retrieval.py / orchestration.py / long_term_memory.py（已发内部事件：mcp.tool.call/result、capability.module.*、memory.long_term.rejected 等）。
- kernel/tasks.py（当前 SSE 发外部事件 task.created/step.*/delta/task.finished/task.degraded）；ui/index.html（当前 EventSource 渲染）。
- scripts/verify-all.mjs / review-precheck.mjs（已有 events schema 正负例校验 + 事件名白名单）。

## 需求
1. 内部→外部映射模块（kernel/event_mapping.py）：按 events.md §6 把内部总线事件映射为外部 WS 事件信封；外部事件必带内部来源 span_id（审计反查）；映射可双向溯源（任一外部事件可查内部来源）。
2. 信封合规：外部事件经 events.schema.json 校验（event_id 单调/event 枚举/payload 判别）；未知事件拒推。
3. UI 状态机：ui/index.html（或抽 ui/state-machine.js）按 ui-state-machine.md 实装 10 状态转移（idle/routing/thinking/checking/acting/streaming/awaiting_user/yielded/error/cancelled），事件驱动迁移，degraded 正交标记，未知事件容错；转移逻辑抽为可测纯函数。
4. CI 双向校验：推送/订阅事件经 schema 校验，未登记事件/非法载荷拒绝（负例测试）；内部事件三段命名校验（ADR-002）。
5. 审计反查：内部 span_id → 外部事件 → trace_id 链路可验证（§9.8）。
6. tasks.py 外部事件经映射模块发出（带 span_id），保持现有 SSE 行为兼容（M1-7 再升 WS）。

## 测试（必须）
- 映射：内部 capability.module.invoked→step.started、mcp.tool.call→tool.call、kernel.task.degraded→task.degraded 等（按 §6）；外部事件带 span_id；双向溯源。
- 信封：18 外部事件载荷经 events.schema.json 校验通过；未知事件/非法载荷拒推（负例）。
- UI 状态机：转移纯函数覆盖主要路径（task.created→routing、delta→streaming、task.finished→idle、task.failed→error、confirmation→awaiting_user、degraded 正交、未知事件容错）。
- 审计反查：span_id→外部事件→trace_id 可断言。
- 不得削弱现有 90 个测试。

## 不动什么
- docs/contracts/ 冻结契约 + ADR（实装它们而非改）；kernel/protocol.py / mcp.py / retrieval.py / orchestration.py / long_term_memory.py 核心逻辑。
- 不新增公开 REST 端点；不打 tag（DSH 打 contract-events-v1）。

## 门禁（必须跑并报告）
- python -m pytest -q  （期望全绿）
- node scripts/verify-all.mjs  （期望 VERIFY_ALL_PASSED）

## 报告契约
输出：新增/改动文件清单 + 设计要点（映射模块/UI 状态机抽离/CI 校验/审计反查）+ pytest 结果 + verify-all 结果 + 映射双向溯源/状态机迁移/未知事件拒推如何验证。不要提交、不打 tag。
