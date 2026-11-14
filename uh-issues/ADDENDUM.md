# uh-issues 附录：范围变更（2026-11-14）

> 本附录记录交付包生成后的正式范围变更；README.md 保持原始规格不动，以本附录为准。

## 变更 1：P0-3 扩范围（Coopanion 事件流对照提案）
- 原范围：补 task_id/stream_url、WS 重连示例、终态定义
- 新范围：+ 事件字典（5 类 18 事件）+ 内部→外部映射表 + 回放语义（游标/终态保证/seq 对齐/审计反查）+ UI 状态机契约
- 交付：docs/contracts/task-flow.md、events.md、events.schema.json、ui-state-machine.md、revisions/P0-3-修订对照.md
- 校验：ajv 25/25 全过（18 正例 + 7 负例）

## 变更 2：新增 M1-9（MS-2，任务总数 38 → 39）
- 标题：事件字典冻结 + 总线映射 + UI 状态机
- Labels：type/contract area/kernel P1 size/M
- Blocked by：P0-3、M1-1；Blocks：M1-7
- 位置：M1-1 之后、M1-7 之前（先字典后 WS 实装，防返工）

## 变更 3：联动调整
| Issue | 调整 |
|---|---|
| M1-1 | 信封加 event_id（bus_seq）；新增内部事件登记；命名 2/3 段裁决 |
| M1-7 | 事件 8 → 18；after_event_id 游标 + 终态保证；Blocked by 增加 M1-9 |
| M1-8 | 确认必产生 confirmation_required/resolved 事件；confirm 端点按 task-flow §4；拒绝 → cancelled |
| M2-5 | 可订阅新增内部事件（safety.* / kernel.task.* / mcp.tool.*） |
| M3-6 | 与 Web 共用事件驱动状态机 |

## 创建顺序说明
M1-7 的新依赖 M1-9 在 TASKS 数组中晚于 M1-7 创建；create-issues.sh 第二轮统一回填依赖编号，不受创建顺序影响。README 第 9 节"Blocked by 一定先创建"的表述自此不再严格成立，以两轮机制为准。

## 基线说明
上游基线已切换为《Ultra-Harness技术文档_整合版_v3（润色校对）》（见 docs/contracts/revisions/基线变更-v3-差异评估.md）；本包章节引用两版一致，无需改动。

## 变更 4：GitHub 实建（2026-11-14）
- 经 REST API 创建：18 labels、6 milestones（MS-0~MS-5）、39 issues（#1~#39，按 TASKS 顺序）
- 第二轮全量回填依赖编号（39/39），残留 {{ID}} 占位符 0
- 编号映射持久化于 issue-map.csv；P0-1=#1、P0-3=#3、M1-9=#21、R-3=#39
- create-issues.sh 自此仅作历史参考，重跑会产生重复 Issue
- 修复原脚本回填 bug：正文 #{{ID}} 自带井号，原替换 {{ID}}→#编号 产生 ##编号（GitHub 不识别为引用）；已改为 {{ID}}→编号，线上 39 个正文的 ##N 全量修正为 #N
