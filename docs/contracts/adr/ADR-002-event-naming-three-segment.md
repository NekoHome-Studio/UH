# ADR-002：内部总线事件命名统一三段（域.对象.动作）

- 状态：**Accepted**（2026-11-15，用户裁决「B」）
- 关联：事件命名 2/3 段冲突（P0-2 登记 / M1-1 前置）· 影响 M1-1 / M2-5

## 背景
§6.6 约定事件命名「域.对象.动作」三段，但标准内部事件 capability.invoked / capability.failed / memory.write 等为两段（域.动作），自相矛盾。

## 决策
**内部总线事件统一三段「域.对象.动作」**；采用新事件名，旧两段名标记 deprecated（只增不改，过渡期兼容，最早 v0.2.0 移除）。

## 适用范围（重要）
- 本决策仅约束**内部总线事件**（§6.6，M1-1 冻结）。
- **外部 WS 事件契约（events.md，MS-0 已冻结）不变**：task.* / step.* / delta / tool.* 等保持原样，遵循 events.md 自身约定，不受 §6.6 三段约束。内部→外部映射表（events.md §6）桥接两者。

## 内部事件改名映射（提案，M1-1 定稿）
| 旧（两段，deprecated） | 新（三段） |
|---|---|
| capability.invoked | capability.module.invoked |
| capability.failed | capability.module.failed |
| capability.finished | capability.module.finished |
| memory.write | memory.record.write |
| llm.delta | llm.stream.delta |
| tool.confirm | tool.confirmation.requested |
| tool.confirm.resolved | tool.confirmation.resolved |
| cua.yield | cua.session.yielded |
| kernel.request.received / kernel.route.decided / mcp.tool.call / mcp.tool.result | 已三段，保留 |

## 后果
1. M1-1 实装三段内部事件名；旧名 deprecated 别名过渡（只增不改）。
2. events.md §6 映射表 M1-1 更新：新增三段内部名，旧名标 deprecated（外部 WS 事件不动）。
3. M2-5 钩子插件订阅新三段名；旧名兼容期可用。
4. review-precheck 内部事件白名单随 M1-1 更新。
5. 对象段（module/record/stream/confirmation/session）由 M1-1 按能力细分定稿。

## 与 ADR-001 协同
检索走 MCP Server（ADR-001），其调用经 mcp.tool.call / mcp.tool.result（已三段）；非 MCP 能力用 capability.module.*。
