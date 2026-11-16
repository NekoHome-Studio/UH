# Brief 0005：M1-4 LangGraph 编排（状态机 + Checkpoint + resume + deadline）

## 角色与约定
你是实现者（Codex），在 UH! 仓库（Python 3.12 + FastAPI）工作，先读 AGENTS.md。只改代码与测试，**不要 git commit**（DSH 审查后提交）。离线测试用 SQLite/JSON checkpoint + 可选 langgraph 回退，无需真实 PostgreSQL。

## 目标
把 LangGraph 状态机封装为独立能力模块（适配层隔离），支持多步 workflow + Checkpoint 持久化 + 中断恢复。workflow 链路成立的前提。

## 必读上下文（仓库内）
- AGENTS.md；kernel/protocol.py（CapabilityRegistry/Envelope/EventBus，编排注册为能力经 RPC 调用）。
- kernel/retrieval.py / kernel/mcp.py：适配层隔离 + 可选依赖回退模式参考（LlamaIndex/Chroma 缺依赖回退）。
- kernel/main.py / kernel/tasks.py：当前 workflow 模式尚未接编排；POST /tasks/{id}/resume 契约见 docs/contracts/task-flow.md §4（尚未实装）。
- docs/contracts/task-flow.md §4：resume 返回 {task_id, resumed_from_event_id, checkpoint_id?}，触发 task.resumed。
- docs/contracts/events.md：task.resumed / step.started / step.finished / task.degraded 外部事件；内部三段命名（ADR-002）。
- docs/contracts/errors.md：超时 UH-CAP-5021；编排不可用降级。
- v3 §4.2 / §6.3.2：节点完成落 Checkpoint，resume 不重复执行已完成节点，每次转移带 deadline_ms。

## 需求
1. 编排能力模块（kernel/orchestration.py 或 capabilities/orchestration/）：LangGraph 状态机封装，**适配层隔离**（LangGraph API 变动频繁，业务只依赖 UH! 内部接口，见风险）。langgraph 缺依赖时提供最小回退状态机（顺序节点执行 + checkpoint），保证离线测试与内核不携带第三方对象。
2. 示例 workflow：plan → retrieve → generate → verify 多节点（节点可调用检索/LLM 能力或 mock）；每节点完成落 Checkpoint。
3. Checkpoint 持久化：目标 PostgreSQL（env UH_CHECKPOINT_POSTGRES_URL），**离线回退 SQLite 或 JSON**（langgraph-checkpoint-sqlite 或自实现）；重启后可从 Checkpoint 恢复。
4. resume：实装 POST /tasks/{id}/resume（task-flow.md §4），从中断点继续，**不重复执行已完成节点**；触发 task.resumed 事件（含 skipped_steps/resumed_from_event_id）。
5. deadline：每次状态转移带 deadline_ms，超时由内核主动取消（→ UH-CAP-5021）。
6. 集成：workflow 模式经 CapabilityRegistry 调编排能力；**关闭编排模块不影响普通对话**（chat/search 降级可用，M1-1 降级路径）。
7. 事件：节点 step.started/step.finished（module=langgraph），resume task.resumed，降级 task.degraded；配置经 env，严禁硬编码密钥。

## 测试（必须，离线，SQLite/JSON checkpoint + 回退状态机）
- 多节点 workflow 按序执行，每节点落 checkpoint。
- 中断后 resume：已完成节点不重复执行（用执行计数断言）。
- Checkpoint 重启恢复：新实例加载 checkpoint 续跑。
- deadline 超时 → 内核取消 → UH-CAP-5021。
- 编排关闭 → chat 仍完成（降级）。
- resume 端点返回契约形状 + task.resumed 事件。
- 不得削弱现有 75 个测试。

## 不动什么
- docs/contracts/ 冻结契约 + ADR；kernel/protocol.py / mcp.py / retrieval.py / tracing.py / logging.py 既有行为；/health /ready router 既有端点。
- 不新增公开 REST 端点（resume 是契约已定义的 /tasks/{id}/resume，实装它而非新增）。

## 门禁（必须跑并报告）
- python -m pytest -q  （期望全绿）
- node scripts/verify-all.mjs  （期望 VERIFY_ALL_PASSED）

## 报告契约
输出：新增/改动文件清单 + 设计要点（langgraph 封装/回退状态机/checkpoint 后端选型）+ pytest 结果 + verify-all 结果 + resume 不重复执行/重启恢复/deadline 取消如何验证。不要提交。
