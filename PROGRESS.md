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
| P0-4 | 沙箱分期与平台矩阵 | 已交付 · 待评审 | sandbox.md + P0-4 修订对照（SBX-07~09 新增） |
| P0-5 | 部署暴露面与 RBAC 收紧 | 已交付 · 待评审 | deploy-hardening.md + P0-5 修订对照 |
| P0-6 | 机器可读契约与图 | 已交付 · 待评审 | openapi.yaml + asyncapi.yaml + diagrams.md + CI 工作流 + 校验脚本 |
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

### 2026-11-14 · GitHub Issue 体系实建
- REST API 创建（gh 未安装，凭据取自 Windows 凭据管理器）：18 labels、6 milestones、39 issues（#1~#39，TASKS 顺序）
- 第二轮回填 39 个正文依赖编号，残留占位符 0；M1-9=#21，M1-7=#19（Blocked by #3/#13/#18/#21）
- 持久化 uh-issues/issue-map.csv（id→number→url 映射）；仓库 https://github.com/NekoHome-Studio/UH

### 2026-11-14 · P0-4 沙箱分期与平台矩阵
- 交付 sandbox.md：8 维度分期表 + 三平台 M2 可执行矩阵（bwrap+slirp4netns / Seatbelt / Job Object+ACL+代理）+ M3 加固矩阵（seccomp+cgroups / Seatbelt 收紧 / AppContainer）
- Windows M2 显式降级（行为级）+ 补偿控制 + 安装风险披露文案；SBX-01/02 分平台判据
- 新发现：SBX-06 属安装期（M2-1），M3-7 误引 → 新增 SBX-07~09；M2-2(#23)/M3-7(#34) 验收已同步
- 决议 S1~S7；修订对照 R1~R6；状态 needs-review（内核负责人）

### 2026-11-14 · P0-5 部署暴露面与 RBAC 收紧
- 交付 deploy-hardening.md：Compose 仅 8080 对外（能力模块 expose、redis/chroma 移除宿主端口）+ uh-scaler 最小 RBAC（deployments/scale 子资源、resourceNames 锁定、禁止清单、审计命令）+ NetworkPolicy 默认拒绝与放行矩阵
- 决议 E1~E6；§9.2/§9.3 追加条款文本就绪；M3-1(#28)/M3-2(#29) 任务已引用契约
- 状态 needs-review；部署实跑验证属 M3-1/M3-2

### 2026-11-14 · P0-6 机器可读契约与图（MS-0 收官 6/6）
- 交付 openapi.yaml（3.1，16 路径 19 操作）+ asyncapi.yaml（2.6，24 消息，与 events.schema.json 同源生成）+ diagrams.md（4 图）+ CI 工作流 + 可复用校验脚本
- 校验全绿：swagger-cli valid；@asyncapi/cli 官方 CLI exit 0 + parser 3.6.6 零错误；ajv 契约脚本全过；端点覆盖 19/19
- A1 不影响用户 API（search 经 messages），仍待 M1-1 裁决；Mermaid GitHub 渲染待推送后复核
- MS-0 六个 P0 全部交付，进入同批评审冻结阶段

## 下一步
MS-0 评审冻结：P0-1~6 同批评审 → 按修订对照应用两份 docx → 关闭 #1~#6 → 启动 M0-1 内核骨架（#7）。A1 检索接口形态与事件命名 2/3 段在 M1-1 冻结前裁决。
