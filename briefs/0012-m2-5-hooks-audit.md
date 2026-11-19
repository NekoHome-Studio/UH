# Brief 0012：M2-5 钩子插件 + 审计日志示例（#26）

## 角色与约定
你是实现者（Codex），在 UH! 仓库工作，先读 AGENTS.md。只改代码与测试，**不要 git commit**（DSH 审后提交）。复用 M2-1 PluginDriver + EventBus（kernel/protocol.py）。

## 目标
钩子插件让审计/计费/内容审核不动内核接入。事件总线订阅机制 + 官方审计日志钩子示例 + 钩子异常隔离 + 异步超时策略。

## 必读上下文
- AGENTS.md；kernel/protocol.py（EventBus.subscribe/publish，订阅者异常隔离已有）；kernel/event_mapping.py（内部→外部映射，三段命名 ADR-002）。
- kernel/plugins.py（PluginDriver/PluginManifest，plugin_type 枚举；hook 类型）；kernel/plugin_sdk.py。
- docs/contracts/events.md §6（内部事件总线映射：kernel.request.received/route.decided/capability.*/memory.write/safety.*/kernel.task.*/mcp.tool.*）。
- v3 §5.4 / §9.8（钩子插件 + 审计反查链）。

## 需求
1. **事件总线订阅机制**：hook 插件经 PluginDriver 注册订阅 EventBus；可订阅 kernel.request.received / kernel.route.decided / capability.invoked / capability.failed / memory.write 及 events.md §6 新增内部事件（safety.* / kernel.task.* / mcp.tool.*）。订阅经 manifest 声明（events 列表）+ 签名校验。
2. **官方审计日志钩子示例**：examples/plugins/official/audit-log/（main.py + manifest.yaml），订阅 route.decided 等全量事件，写结构化审计日志（含 trace_id/span_id/event_id，§9.8 反查链）。示例可直接复用。
3. **钩子异常隔离**：钩子抛错不影响主链路（EventBus 已有 except 隔离，强化 + 测试）。
4. **异步 + 超时策略**：钩子异步执行（不阻塞主链路），单钩子超时上限（如 100ms）超时取消；明确同步钩子禁用或隔离。钩子背压/丢弃策略（事件洪水不拖垮）。
5. 复用 PluginDriver 签名/沙箱；审计日志密钥经 env。

## 测试（必须）
- 钩子订阅：audit-log 钩子记录 route.decided 全量事件（含 trace_id/span_id）。
- 钩子抛错主链路正常（隔离）。
- 钩子超时取消（慢钩子不阻塞）。
- 订阅 manifest 校验（非法事件名拒绝，三段命名）。
- 示例 audit-log 可 install + activate + 产出审计日志。
- 不得削弱现有 172 测试。

## 不动什么
- docs/contracts/ 冻结契约；kernel/protocol.py EventBus 核心（可扩展超时/背压）；event_mapping.py 映射。

## 门禁（必须跑并报告）
- python -m pytest -q（期望全绿）
- node scripts/verify-all.mjs --full（期望 8/8）

## 报告契约
输出：新增/改动文件 + 设计要点（订阅机制/异步超时/异常隔离/审计示例）+ pytest + verify-all + 各验收如何验证。不要提交。
