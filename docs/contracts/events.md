# UH! 事件字典 · 唯一事实源（Event Dictionary）

> 版本：v0.1.0（契约冻结候选）· 状态：needs-review · 对应任务：P0-3（扩展）→ M1-9 冻结
> 基线：v3 整合版 §5.4 / §6.4 / §6.6 / §9.8 · 关联：task-flow.md、events.schema.json、ui-state-machine.md、errors.md
> 效力：评审通过后，内部事件总线、WS 推送、钩子插件、审计、前端状态机一律以本文件为准；事件散落在 5.4/6.4/6.6/9.8 的局面就此终结。

## 1. 治理规则

1. 事件只增不改（v3 §6.4 已有约定）；语义变更需新事件名，旧名标 deprecated_in + 替代事件。
2. 载荷字段新增标 since: 1.x，向后兼容；删除字段必须升主版本（§6.8）。
3. 每个事件登记：direction / emitted_by / trigger / payload / ordering / terminal；缺登记不得推送。
4. 维护人：内核负责人；CI 以 events.schema.json 校验推送与订阅（M1-9 实装）。

## 2. 统一信封（所有 WS 服务端事件）

```json
{
  "event_id": 42,
  "event": "step.started",
  "version": 1,
  "task_id": "uh_task_7f3a9c...",
  "trace_id": "uh_trc_9c8d...",
  "span_id": "sp_001",
  "ts": "2026-11-14T08:30:01Z",
  "payload": {}
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| event_id | integer ≥1 | 是 | 任务内单调递增、无空洞（决议 C3）；回放游标 |
| event | string | 是 | 本字典登记的事件名 |
| version | integer | 是 | 载荷版本，当前恒 1 |
| task_id | string | 是 | uh_task_ 前缀 |
| trace_id | string | 是 | 全链路主键（P0-3 要求） |
| span_id | string | 否 | 产生该事件的内部 span，审计反查用（决议 C12） |
| ts | string | 是 | RFC 3339 UTC |
| payload | object | 是 | 按事件名判别，见 §4 / events.schema.json |

## 3. 五类 18 事件总表

| # | 类 | 事件 | 触发 | terminal |
|---|---|---|---|---|
| 1 | 生命周期 | task.created | 路由判定完成（kernel.route.decided） | 否 |
| 2 | 生命周期 | task.finished | 任务成功完成 | 是 |
| 3 | 生命周期 | task.failed | 任务失败 | 是 |
| 4 | 生命周期 | task.cancelled | 用户取消 / 确认拒绝 / 超时 / 系统 | 是 |
| 5 | 生命周期 | task.resumed | POST /tasks/{id}/resume 从 Checkpoint 恢复 | 否 |
| 6 | 阶段 | step.started | 能力节点开始 | 否 |
| 7 | 阶段 | step.finished | 能力节点结束 | 否 |
| 8 | 阶段 | step.failed | 能力节点失败 | 否 |
| 9 | 内容流 | delta | LLM 流式输出增量 | 否 |
| 10 | 内容流 | tool.call | MCP 工具调用前 | 否 |
| 11 | 内容流 | tool.result | 工具返回后 | 否 |
| 12 | 等待/中断 | task.confirmation_required | 高危工具等待确认 | 否 |
| 13 | 等待/中断 | task.confirmation_resolved | 确认被批准/拒绝/超时 | 否 |
| 14 | 等待/中断 | task.yielded | CUA 让位（预留，v0.1.0 无官方发射器） | 否 |
| 15 | 等待/中断 | task.degraded | 能力降级（CAP-3001 等） | 否 |
| 16 | 安全/验证 | safety.check_started | 注入检测/工具风险/输出过滤开始 | 否 |
| 17 | 安全/验证 | safety.check_finished | 安全检查结束 | 否 |
| 18 | 安全/验证 | safety.denied | 安全检查拦截动作 | 否 |

direction 全部为 server_to_client；emitted_by 均为 kernel（代各能力模块转发）。内部事件（direction=internal）见 §6 映射表。
计数说明（决议 C1）：提案表列 17 个与宣称 18 差 1，补 task.resumed（M1-4 resume 的 UI 可见性必需）凑齐 18。

## 4. 载荷字段定义

module 枚举（决议 C7，用 v3 实际模块 ID）：router / llama_index / langgraph / crewai / mcp_tool / memory / llm / safety
risk_level 枚举：read-only / write-scoped / write-host / irreversible（P0-2 manifest 契约）
error 对象：{ code, message, retryable, retry_after_ms?, detail? }，code 遵循 errors.md

| 事件 | payload 字段（* 必填） |
|---|---|
| task.created | session_id*、mode*(chat/search/workflow/collaborate/tool)、title |
| task.finished | final_result*{content*、citations[]、usage{prompt_tokens,completion_tokens}}、duration_ms* |
| task.failed | error*、duration_ms |
| task.cancelled | reason*(user_request/confirmation_rejected/timeout/system)、error、completed_steps |
| task.resumed | checkpoint_id*、skipped_steps*、resumed_from_event_id |
| step.started | step_id*、module*、name*、index |
| step.finished | step_id*、module*、status*(done/skipped)、duration_ms* |
| step.failed | step_id*、module*、error* |
| delta | text*、index*（text 沿用 v3 字段名，决议 C9） |
| tool.call | call_id*、tool_name*、args_summary*（脱敏）、risk_level*、requires_confirmation* |
| tool.result | call_id*、tool_name*、status*(ok/error/denied/timeout)、truncated*、content_preview（脱敏 ≤2KB）、duration_ms* |
| task.confirmation_required | call_id*、tool_name*、risk_level*、summary*（脱敏影响摘要）、confirmation_token*、expires_at*(RFC3339) |
| task.confirmation_resolved | call_id*、decision*(approved/rejected)、resolved_by*(user/timeout/system)；不回显 token |
| task.yielded | reason*(user_input_preempt)、hint |
| task.degraded | reason_code*(UH-*)、module*、description* |
| safety.check_started | check_id*、check_type*(injection/tool_risk/output_filter)、target*(retrieval_content/tool_args/model_output/memory_content) |
| safety.check_finished | check_id*、check_type*、result*(pass/block)、duration_ms* |
| safety.denied | check_id*、check_type*、reason*、action_taken*(exclude_content/block_tool/fail_task)、related_error_code |

隐私收紧（决议 C6）：完整工具参数与完整返回永不进事件流；tool.call 只带 args_summary，tool.result 只带 content_preview（§9.7 数据最小化 / §9.8 日志不落全文）。

## 5. 顺序约束（因果序，event_id 全序之上）

1. task.created 必为事件流第一个事件（event_id=1）。
2. 终态事件必为最后一个，且三选一仅一次（task-flow.md 决议 F4）。
3. 同 step_id：step.started → step.finished | step.failed。
4. 同 call_id：tool.call → tool.result；需确认时 tool.call → task.confirmation_required → task.confirmation_resolved → 执行或 task.cancelled。
5. 同 check_id：safety.check_started → safety.check_finished；safety.denied 仅在 result=block 后。
6. safety.denied 不必然终态（决议 C11）：action_taken=exclude_content → 继续或 task.degraded；block_tool → step.failed；fail_task → task.failed。
7. task.resumed 后跳过已完成节点（skipped_steps>0），不重复执行（E2E-04）。

## 6. 内部事件 → 外部事件映射表

规则：外部事件必须能在内部找到来源；内部事件可以多于外部（审计粒度更细）。内部新增事件同样只增不改，命名遵循 §6.6（2/3 段兼容问题由 M1-1 裁决，P0-2 已登记）。

| 内部事件（总线） | 外部事件（WS） | 条件 |
|---|---|---|
| kernel.request.received | —（仅内部审计） | 任务创建审计；不推 UI（决议 C2） |
| kernel.route.decided | task.created | 总是；mode 入 payload（v3 L551：触发=路由判定完成） |
| capability.invoked | step.started | 按 module 映射 |
| capability.finished（新增） | step.finished | 总是 |
| capability.failed | step.failed | 总是 |
| memory.write | —（仅内部审计） | §9.8 |
| llm.delta（新增） | delta | 流式期间 |
| mcp.tool.call（新增） | tool.call | 总是 |
| mcp.tool.result（新增） | tool.result | 总是 |
| tool.confirm（§9.8） | task.confirmation_required | 总是 |
| tool.confirm.resolved（新增） | task.confirmation_resolved | 总是 |
| kernel.task.cancelled（新增） | task.cancelled | 总是 |
| kernel.task.resumed（新增） | task.resumed | resume 时 |
| kernel.task.degraded（新增） | task.degraded | 降级路径（§6.6 degraded:true） |
| kernel.task.finished（新增） | task.finished | 总是 |
| kernel.task.failed（新增） | task.failed | 总是 |
| safety.check_started / finished / denied（新增，§9.5/§9.6） | 同名外部事件 | 总是 |
| cua.yield（预留，CUA 插件） | task.yielded | CUA 启用时（决议 C4） |

信封联动（M1-1）：内部信封增加 event_id（bus_seq，总线内单调）；外部信封 span_id 指向来源内部 span；审计经 trace_id + task_id + span_id 反查 UI 事件流（§9.8 溯源链）。

## 7. 回放语义（与 task-flow.md §5 互为引用）

1. 游标：after_event_id=N 重放 (N, latest]；event_id 任务内单调无空洞；last_event_id 为兼容别名（deprecated_in v0.1.0，v0.2.0 移除）。
2. 终态保证：任务已终态时重连直推终态 + final_result/error，不重放中间事件；终态事件持久化至任务 TTL。
3. 窗口：默认 5 分钟热回放；超窗发 replay_unavailable + close 4408，回退 /tasks/{id}/steps 全量。
4. 对齐：steps[].seq = 该步骤 step.started 的 event_id（决议 C8）；steps 与事件流可合并；窗口外 delta 级内容不回放。
5. 审计：内部总线全量落 append-only 存储（§9.8），保留 180/365 天；审计重建 UI 事件流经映射表反演。

## 8. 决议记录（对提案与 v3 的核验结论）

| # | 决议 | 依据 |
|---|---|---|
| C1 | 提案表列 17 个 ≠ 宣称 18；补 task.resumed 凑齐 18 | M1-4 resume 需 UI 可见性；只增允许 |
| C2 | task.created 触发点 = kernel.route.decided；request.received 仅内部审计 | v3 L551 已定义"路由判定完成"；提案映射行 1 有误——request 时 mode 尚不可知 |
| C3 | event_id 任务内单调递增整数（≥1，无空洞）；游标参数 after_event_id，last_event_id 兼容别名 | 提案"非 UUID"落地；v3 L575 用 last_event_id |
| C4 | task.yielded 预留：v0.1.0 无 CUA 官方发射器 | §8.10 范围边界；契约可先于发射器定义 |
| C5 | 确认拒绝 → task.cancelled(reason=confirmation_rejected, error.code=UH-USR-1002) | M1-8"优雅结束保留步骤"；终态语义比 failed 准确 |
| C6 | tool.call/result 只带脱敏摘要与 ≤2KB 预览 | §9.7/§9.8；v3 载荷摘要 args/content 收紧 |
| C7 | module 枚举用 v3 模块 ID（llama_index 等），不用 Coopanion 脑区名 | v3 §6.3.2 示例 / §6.6 from/to |
| C8 | steps[].seq = step.started 的 event_id | v3 steps 已有 seq；两路数据合并 |
| C9 | delta.text 沿用 v3；step.finished.duration 规范为 duration_ms | 发布前冻结，统一毫秒单位 |
| C10 | 提案状态机 YAML 重复键（thinking×3 / any×2）不合法；重写为 from/on/to/guard 转移表；degraded 为正交标记非状态 | ui-state-machine.md |
| C11 | safety.denied 不必然终态，由 action_taken 决定后续 | 注入检测可剔除内容后降级继续（§4.3） |
| C12 | 内部信封加 event_id（bus_seq），外部信封 span_id 反查审计 | 提案 M1-1 联动；§9.8 溯源链 |

## 9. Issue 落点

| Issue | 动作 |
|---|---|
| P0-3 | 扩范围：本字典 + task-flow + schema + 状态机为契约交付 |
| M1-9（新增） | 字典冻结 + CI 校验 + 总线映射实装 + UI 状态机接入；Blocked by P0-3, M1-1；Blocks M1-7 |
| M1-1 | 信封加 event_id（bus_seq）；内部新增事件登记；命名 2/3 段裁决 |
| M1-7 | WS 事件 8 → 18；回放游标与终态保证实装 |
| M1-8 | 确认必产生 confirmation_required/resolved 事件；confirm 端点按 task-flow §4 |
| M2-5 | 钩子可订阅 safety.* / task.yielded 等新事件 |
| M3-6 | Desktop 与 Web 共用同一状态机，双端一致 |
