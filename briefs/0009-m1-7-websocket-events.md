# Brief 0009：M1-7 WebSocket 事件流（18 事件 + after_event_id 回放 + 终态保证 + 订阅鉴权）

## 角色与约定
你是实现者（Codex），在 UH! 仓库（Python 3.12 + FastAPI + 原生 JS UI）工作，先读 AGENTS.md。只改代码与测试，**不要 git commit**（DSH 审查后提交）。事件字典已冻结（contract-events-v1），实装它而非改契约。

## 目标
把 M0 的 SSE 过渡升级为 WebSocket 统一事件通道：连接鉴权 + 18 事件推送（经 M1-9 映射）+ after_event_id 断线回放 + 终态保证 + 5 分钟窗口回退 + 订阅归属鉴权（SEC-04）。

## 必读上下文（仓库内）
- AGENTS.md；docs/contracts/task-flow.md（§2 WS 连接/鉴权/关闭码 4401/4403/4404/4408/1000；§5 回放语义：after_event_id 游标、终态保证、5 分钟窗口、steps seq 对齐）；docs/contracts/events.md（18 事件+信封）；docs/contracts/events.schema.json；docs/contracts/ui-state-machine.md。
- kernel/event_mapping.py（M1-9 EventMapper：内部→外部映射+span_id+event_id 单调+schema 校验，复用）；kernel/protocol.py（EventBus/Envelope）；kernel/tasks.py（当前 SSE 发外部事件，TaskManager）。
- kernel/main.py（当前 GET /tasks/{id}/stream SSE + trace 中间件 + 幂等包裹）；ui/index.html + ui/state-machine.js（M1-9 状态机已接入，当前 EventSource）。
- docs/contracts/errors.md：UH-USR-1002（权限不足/越权订阅）、UH-USR-1001（未认证）、UH-USR-1003（task 不存在）。

## 需求
1. WS 端点 /api/v1/stream（FastAPI WebSocket）：query task_id 必填 + after_event_id（last_event_id 兼容别名）；鉴权（token 经 query 或首条 op:auth；本地模式可关闭认证但归属仍校验）。
2. 事件推送：任务内部事件经 EventMapper 映射为外部事件（schema 校验+span_id+event_id 单调）推 WS；18 事件按字典。
3. 回放：per-task 事件缓冲（带时间戳）；重连 after_event_id=N 重放 (N,latest]；**终态保证**：任务已终态直推终态事件（不重放中间）；**5 分钟窗口**：超窗发 replay_unavailable + close 4408，客户端回退 GET /tasks/{id}/steps。
4. 订阅归属（SEC-04）：订阅非本连接归属的 task_id → UH-USR-1002 + close 4403；task 不存在 → 4404；未认证 → 4401；终态正常关闭 1000。需最小归属模型（task 记录 owner client_id/token，subscribe 校验）。
5. 客户端 op：subscribe/auth/ping→pong（task-flow）；服务端控制帧 pong/error/replay_unavailable。
6. UI：ui/index.html 改用 WebSocket（ws/wss，按 location.protocol），保留 state-machine.js 状态机；处理 close 码（4408 回退 steps，4403 提示）；SSE 端点可保留兼容但 UI 默认 WS。
7. steps seq 与 event_id 对齐（task-flow §5，M0 已有 steps，核对对齐）。

## 测试（必须，TestClient.websocket_connect）
- WS 连接+鉴权成功，收到 task.created 等事件，信封经 events.schema.json 校验。
- 断线重连 after_event_id 回放不丢事件（event_id 连续）。
- 终态保证：任务终态后重连直推终态事件。
- 超窗回退：模拟超 5 分钟 → replay_unavailable + close 4408。
- SEC-04：订阅他人 task_id → USR-1002 + close 4403。
- 关闭码 4401/4404/1000 路径。
- 不得削弱现有 96 个测试。

## 不动什么
- docs/contracts/ 冻结契约 + ADR + 三个 tag；kernel/event_mapping.py / protocol.py / retrieval.py / orchestration.py / long_term_memory.py 核心；ui/state-machine.js 状态机逻辑。
- 不新增公开 REST 端点（WS 是契约已定义的 /api/v1/stream）。

## 门禁（必须跑并报告）
- python -m pytest -q  （期望全绿）
- node scripts/verify-all.mjs  （期望 VERIFY_ALL_PASSED）

## 报告契约
输出：新增/改动文件清单 + 设计要点（WS 鉴权归属模型/回放缓冲/终态保证/窗口回退/UI 切换）+ pytest 结果 + verify-all 结果 + 回放不丢/终态保证/SEC-04/超窗回退如何验证。不要提交。
