# UH! 执行进度

> 任务基线：uh-issues/（39 个 Issue，含新增 M1-9）· 计划 20 周 · 本文档随执行更新
> 当前阶段：MS-0 已冻结（2026-11-15 评审通过，tag contract-v0.1.0）→ 进入 M0 骨架期
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
| M0-1 | 内核骨架：trace_id + JSON 日志 + health/ready | 已完成 · 服务器实测 | kernel/ + tests 15/15 + 192.168.0.28:8080 常驻 |
| M0-2 | 规则版路由（chat/search/tool） | 已完成 · 首个 Codex 协作 | router.py 100/100 + 24/24 + 跨机实测 |

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
- A1 不影响用户 API（search 经 messages），仍待 M1-1 裁决；Mermaid GitHub 渲染 4/4 浏览器实测通过（iframe 沙箱渲染）
- MS-0 六个 P0 全部交付，进入同批评审冻结阶段

### 2026-11-14 · MS-0 评审预检（人工评审前置）
- 交付 scripts/review-precheck.mjs（8 类交叉一致性检查，已接入 CI）+ revisions/MS-0-评审预检报告.md
- 8/8 通过：错误码注册/事件三源/枚举/内链/数量/信封/占位符/SBX；扫描器经三轮校准（非事件 token 分类固化）
- 评审材料清单就绪，提交人工评审

### 2026-11-15 · MS-0 冻结（评审通过）
- 修订应用：python-docx 生成两份契约修订版 docx（原件保留），v0.1.0 33 处 / v3 36 处操作，22 项标记核对全过（冲突码清零、expose/RBAC/SBX-07~09/目标态标注齐全）
- 关闭 GitHub #1~#6，打 tag contract-v0.1.0
- 用户已备服务器，待提供连接方式后接入测试（M0 开发环境 / M3 部署靶机候选）

### 2026-11-15 · M0-1 内核骨架（#7 完成，首个实现任务）
- kernel/ 三模块（tracing/logging/main）+ 15 单测；trace_id 发射时绑定记录、外部 X-Trace-Id 透传、响应回写
- 部署 Win10 工控机 192.168.0.28:8080（ssh ipc，D:\uh），WMI 常驻；跨机 health/ready 200 + 日志链路串联实测通过
- 固化 scripts/server-deploy.ps1 + docs/runbooks/server-m0.md（NO_PROXY pip + WMI 两坑）
- 关闭 #7；M0-2/M0-3/M0-4/M0-6/M1-1 解锁

### 2026-11-15 · M0-2 规则路由（#8 完成，首个 DSH×Codex 协作闭环）
- 梯子开启后 Codex 连通（代理 127.0.0.1:65532，CODEX_LINK_OK）
- 协作链路跑通：DSH brief → Codex 持久会话实现（01a1df25-ce11-7900-a0ef-38a7f0240625）→ DSH 审 diff + 门禁 → 落提交 ef5eb67
- Codex 精准定位 dev/route 422 根因（内联模型+延迟注解）并最小修复；100/100 分类 + 24/24 套件 + 跨机实测
- 工作流文档 docs/workflows/dsh-codex-collaboration.md + 连通助手 scripts/codex-connect.ps1 就位

## 下一步
M0-3 LLM 封装（#9，需 OpenAI 兼容端点+密钥）/ M0-4 Redis 短期记忆（#10）。后续实现任务可继续派给 Codex（DSH 审）。A1 与事件命名待 M1-1 裁决。
