# MS-0 评审预检报告（交叉一致性扫描）

> 日期：2026-11-14 · 工具：scripts/review-precheck.mjs（可复用，已接入 CI）
> 范围：P0-1~6 全部交付物 + uh-issues 任务包 + README/PROGRESS
> 结论：**8/8 通过，提交人工评审**。预检不替代评审，仅排除机器可查的交叉矛盾。

## 检查结果

| # | 检查项 | 结果 | 说明 |
|---|---|---|---|
| 1 | 错误码注册一致 | PASS | 全部引用 ∈ 13 有效码；4 个 deprecated 码仅出现在历史/修订语境（errors.md / revisions / uh-issues / PROGRESS），契约正文零误用 |
| 2 | 事件名三源一致 + 引用受控 | PASS | events.md §3 表 = events.schema.json enum = asyncapi.yaml 消息名（18）；内部事件按 §6 登记受控 |
| 3 | 枚举一致 | PASS | module（8 值）/ risk_level（4 值）/ mode（5 值）在 manifest schema、events schema、openapi 三处完全一致 |
| 4 | 文档内链可达 | PASS | 全部相对链接 0 死链 |
| 5 | 数量基准 | PASS | issues 39 / issues.csv 39 / issue-map.csv 39 / 事件正例 18 / 负例 7 |
| 6 | 信封必填字段一致 | PASS | event_id/event/version/task_id/trace_id/ts/payload，schema 与 asyncapi 一致 |
| 7 | 契约区无占位符残留 | PASS | docs/contracts 无 {{ID}}（uh-issues/issues 按设计保留供脚本回填） |
| 8 | SBX 引用受控 | PASS | 01~09 均在 sandbox.md 定义；M2-2/M3-7 引用无越界 |

## 扫描器校准记录（三轮）

1. 首轮：§3 表格提取正则列位错误 + 事件词法扫描误报 9 处（权限名 memory.short_term.read、cgroups 参数 memory.max/cpu.max、文件名 tool.schema.json、配置路径 tool.network_allow_hosts、字段路径 step.finished.duration、通配族 kernel.task）。
2. 二轮：修正表格正则；引入非事件 token 分类（权限枚举 / 文件扩展名 / 已知事件上的字段路径 / 通配族前缀）；余 1 处 mcp.tool 通配族。
3. 三轮：补 mcp.tool 族前缀，8/8 通过。校准规则已固化在脚本 isNonEvent()，后续新增契约自动受检。

## 评审材料清单（人工门禁）

| 任务 | 契约 | 修订对照 | 机器校验 |
|---|---|---|---|
| P0-1 | errors.md | P0-1-修订对照.md（含 v3 R8/R9） | grep 一致性 |
| P0-2 | manifest.schema.json + manifest/ | P0-2-修订对照.md | ajv 7/7 |
| P0-3 | task-flow.md / events.md / events.schema.json / ui-state-machine.md | P0-3-修订对照.md | ajv 25/25 |
| P0-4 | sandbox.md | P0-4-修订对照.md | SBX 判据表 |
| P0-5 | deploy-hardening.md | P0-5-修订对照.md | RBAC/NetPol YAML |
| P0-6 | openapi.yaml / asyncapi.yaml / diagrams.md | —（本身即机器契约） | swagger-cli + asyncapi CLI + ajv + 渲染 4/4 |

另：基线变更-v3-差异评估.md（A1~A5 登记）；A1 检索接口形态与事件命名 2/3 段为 M1-1 冻结前裁决项，不阻塞 MS-0 评审。

## 评审通过后动作

1. 按 6 份修订对照应用两份 docx（v3 主 / v0.1.0 对照），执行各对照的应用核对清单
2. 关闭 GitHub #1~#6，契约打 tag contract-v0.1.0
3. 启动 M0-1 内核骨架（#7）
