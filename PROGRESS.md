# UH! 执行进度

> 任务基线：uh-issues/（39 个 Issue，含新增 M1-9）· 计划 20 周 · 本文档随执行更新
> 当前阶段：MS-0 契约冻结周（W1）
> 上游基线：docs/source/《Ultra-Harness技术文档_整合版_v3（润色校对）.docx》（2026-11-14 切换，v0.1.0 架构文档保留对照）

## 状态总览

| ID | 标题 | 状态 | 交付物 |
|---|---|---|---|
| P0-1 | 统一错误码事实源 | 已交付 · 待评审 | errors.md + P0-1 修订对照（含 v3 修正 R8/R9） |
| P0-2 | 四类插件 manifest schema | 已交付 · 待评审 | manifest.schema.json + manifest/ + P0-2 修订对照 |
| P0-3 | REST→WS 任务流闭环（扩范围） | 已交付 · 待评审 | task-flow.md + events.md + events.schema.json + ui-state-machine.md + P0-3 修订对照 |
| P0-4 | 沙箱分期与平台矩阵 | 下一步 | — |
| P0-5 | 部署暴露面与 RBAC 收紧 | 待开始 | — |
| P0-6 | 机器可读契约与图 | 待开始（依赖 P0-1~4；先决 A1 检索接口形态） | — |
| M1-9 | 事件字典冻结 + 总线映射 + UI 状态机（新增） | 待开始（MS-2，Blocked by P0-3/M1-1） | — |

## 执行日志

### 2026-11-14 · P0-1 统一错误码事实源
- 决议 D1~D4；errors.md（13 有效码 + 4 deprecated）；校验全过；v3 笔误并入 R8/R9

### 2026-11-14 · P0-2 四类插件 manifest schema
- 入口 + common + 四类 schema；ajv 7/7 全过；签名策略统一；事件命名/权限缺口登记

### 2026-11-14 · 基线切换：v3 整合版纳入
- 旧冲突全部存续，决议不变；v3 新笔误 2 处（UH-CAP-50210 / 缺连字符）；实质差异 A1~A5 登记

### 2026-11-14 · P0-3 REST→WS 闭环（扩范围，Coopanion 对照提案落地）
- 核验提案与 v3 原文：task.created 触发点以 v3 L551 为准（提案映射行 1 有误）；提案 17≠18，补 task.resumed
- 交付：task-flow.md（202 响应/WS 鉴权/关闭码/confirm 端点/回放）、events.md（18 事件字典+映射+回放）、events.schema.json、ui-state-machine.md（10 状态）、P0-3 修订对照（R1~R6）
- 校验：ajv 25/25 全过（18 正例 + 7 负例）
- 决议 C1~C12 + F1~F5 全部登记；隐私收紧（工具参数/返回脱敏）、确认拒绝→cancelled、yielded 预留
- Issue 体系：新增 M1-9；M1-1/7/8 扩任务；M2-5/M3-6 受益；create-issues.sh + issues.csv 同步（39）；ADDENDUM.md 记录变更
- 待办：与 P0-1/P0-2 同批评审（含时序图评审）→ 修订两份 docx → 关闭 P0-3

## 下一步
P0-4 沙箱分期与平台矩阵：读 v3 §5.5 / §9.3，输出 M2 最小版 / M3 加固版分期表 + Linux/macOS/Windows 平台矩阵，标注目标态与本期交付态。
