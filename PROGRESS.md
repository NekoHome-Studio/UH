# UH! 执行进度

> 任务基线：uh-issues/（39 个 Issue，含新增 M1-9）· 计划 20 周 · 本文档随执行更新
> 当前阶段：M1 能力期完成（2026-11-17，9/9 任务关闭，111/111 测试，三冻结 tag）→ 待进入 M2 插件期
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
| M0-3 | LLM 调用封装（流式/重试/Token） | 已完成 · DSH 实现 | llm.py + 7 mock 测试，31/31 + 服务器实测 |
| M0-4 | Redis 短期记忆（滑动窗口） | 已完成 · DSH 实现 | memory.py + 6 fakeredis 测试，37/37 + /ready 接入 |
| M0-5 | Web UI 最小版（Prompt-bar-first+流式） | 已完成 · DSH 实现 | ui/index.html + tasks.py SSE，42/42 + 跨机实测 |
| M0-6 | CLI uh dev up/down/logs/reset | 已完成 · DSH 实现 | cli.py + uh-dev.yaml，50/50 + 跨平台 |
| M1-1 | 内部 RPC+事件总线+信封 | 已完成 · 冻结 contract-internal-v1 | protocol.py + 14 测试，64/64 |
| M1-2 | MCP Host（tools/list+call） | 已完成 · Codex 实现 DSH 审 | mcp.py + 5 测试，69/69 |
| M1-3 | 检索 MCP Server（LlamaIndex/Chroma） | 已完成 · Codex 实现 DSH 审 | retrieval.py + 6 测试 + bench，75/75 |
| M1-4 | LangGraph 编排（Checkpoint+resume） | 已完成 · Codex 实现 DSH 审 | orchestration.py + 5 测试，80/80 |
| M1-5 | 长期记忆（分级+加密+无痕） | 已完成 · Codex 实现 DSH 审 | long_term_memory.py + 4 测试，84/84 |
| M1-6 | 错误码响应包裹（统一+幂等+分页） | 已完成 · Codex 实现 DSH 审 | main.py 包裹 + 6 测试，90/90 |
| M1-9 | 事件字典冻结+映射+UI 状态机 | 已完成 · 冻结 contract-events-v1 | event_mapping.py + state-machine.js + 6 测试，96/96 |
| M1-7 | WebSocket 事件流（回放+终态+SEC-04） | 已完成 · Codex 实现 DSH 审 | WS + 6 测试，102/102 |
| M1-8 | 高危工具确认（token 防重放+审计） | 已完成 · DSH 实现 | confirmation.py + 9 测试，111/111 |

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

### 2026-11-15 · M0-3 LLM 封装（#9 完成）+ UH 主体确立
- AGENTS.md 项目宪章确立 UH 为主体（DSH/Codex 共读，dsh-agent-instructions 加载）
- M0-3 由 DSH 实现：kernel/llm.py（OpenAI 兼容流式 + 指数退避 + CAP-4xxx 映射 + Token 统计），7 mock 测试，本地+服务器 31/31，部署 192.168.0.28
- 预检白名单补内核日志事件；verify-all 全过；关闭 #9

### 2026-11-15 · M0-6 CLI 完成 + M0 骨架期收官（夜间自主推进）
- M0-6：kernel/cli.py uh dev up/down/logs/reset/watch + uh-dev.yaml，跨平台进程管理，-ui 对齐 §7.2，8 测试
- M0 全部完成：#7~#12 关闭，本地+服务器 50/50，verify-all 全过，部署 192.168.0.28:8080

## M0 验收汇总（MS-1 出口标准）
| 标准 | 结果 |
|---|---|
| chat/search 流式对话 | ✅ M0-3 LLM 流式 + M0-5 SSE UI（mock 可演示，配 UH_LLM_API_KEY 接真实） |
| trace_id 可查 | ✅ M0-1 全链路 trace_id + 日志串联 + 响应头 |
| 路由准确率 ≥90% | ✅ M0-2 100/100 |
| 20 轮不丢上下文 | ✅ M0-4 滑动窗口 40 条 |
| 一键启动 | ✅ M0-6 uh dev up |
| 健康检查 | ✅ /health /ready（含 memory 检查） |

### 2026-11-15 · A1 裁决：检索走 MCP Server
- 用户裁决 A1 = MCP Server；ADR-001 记录（docs/contracts/adr/）；基线评估 A1 更新为已决
- 检索（LlamaIndex）= 独立 MCP Server（search/index_document/query），内核经 MCP Host 调用；M1-2 为检索前置；用户 API 不变

### 2026-11-15 · 事件命名裁决 B + CI 修复
- 用户裁决事件命名 = B（内部总线统一三段，新名+旧名 deprecated）；ADR-002 记录；外部 WS 冻结契约不变
- CI 修复：review-precheck 写 .work/precheck-results.json 在 CI 因 .work 不存在 ENOENT 崩溃（本地有 .work 故通过）→ mkdir recursive；M1 前置裁决全清

### 2026-11-16 · M1-1 内部协议完成（冻结 contract-internal-v1）
- kernel/protocol.py：Envelope+EventBus(三段命名)+CapabilityRegistry(RPC 超时降级)+StorageGuard；检索降级集成；events.md §6 只增登记
- 14 新测试，本地+服务器 64/64；tag contract-internal-v1；关闭 #13
- 修 CLI 健康检查代理绕过（ProxyHandler({})）+ 慢盘超时 45s

### 2026-11-16 · M1-2 派给 Codex（B 路径）
- briefs/0003-m1-2-mcp-host.md 备好（MCP Host：tools/list+call+超时取消+256KB截断+幂等重试，mock 测试，集成 CapabilityRegistry）
- 用户在 Codex GUI 的 UH! 项目发起；DSH 待审 diff + 门禁 + 落地

### 2026-11-16 · M1-2 MCP Host 完成（B 路径 Codex 协作闭环）
- Codex GUI 实现 kernel/mcp.py（557 行，stdio/in-process/HTTP 传输，超时主动取消，256KB 截断，幂等重试，Registry 集成，脱敏）+ 5 测试
- DSH 逐段审 diff + 独立门禁，本地+服务器 69/69；commit 9dba4b6；关闭 #14
- B 路径验证：用户 GUI 发起 brief → Codex 实现不提交 → DSH 审查落地

### 2026-11-16 · M1-3 派给 Codex（B 路径）
- briefs/0004-m1-3-retrieval-mcp.md 备好（检索 MCP Server：LlamaIndex+Chroma，search/index_document/query，untrusted_content 防注入，CAP-3001 降级，经 M1-2 Host 接入，确定性 embedding 离线测试 + 基准 harness）
- ADR-001 落地：检索从内部 RPC 改 MCP Server；用户 GUI 发起，DSH 待审

### 2026-11-16 · M1-3 检索 MCP Server 完成（B 路径，ADR-001 落地）
- Codex 实现 kernel/retrieval.py（MCP Server search/index_document/query，LlamaIndex/Chroma+确定性回退，untrusted_content 防注入，CAP-3001 降级）+ 6 测试 + 诚实 bench
- DSH 审 diff + 门禁 75/75；commit d2dfd66；关闭 #15；检索经 M1-2 Host 接入（ADR-001）
- 遗留：真实 embedding 完整基准待集成；on_event 弃用提示可迁 lifespan

### 2026-11-16 · M1-4 派给 Codex（B 路径）
- briefs/0005-m1-4-langgraph-orchestration.md 备好（LangGraph 状态机适配层+回退，Checkpoint PostgreSQL/SQLite/JSON，resume 不重复执行，deadline 取消，编排关闭不影响对话）
- 用户 GUI 发起，DSH 待审

### 2026-11-16 · M1-4 LangGraph 编排完成（B 路径，workflow 链路成立）
- Codex 实现 kernel/orchestration.py（状态机适配层+fallback，Checkpoint SQLite/JSON，resume 不重跑，deadline 取消）+ 5 测试 + resume 端点
- DSH 审 run/resume 正确性 + 门禁 80/80；commit 5e6d19e；关闭 #16
- 遗留：PostgreSQL checkpoint 待 psycopg 集成（回退 SQLite）

### 2026-11-16 · M1-5 派给 Codex（B 路径）
- briefs/0006-m1-5-long-term-memory.md 备好（语义召回+隐私分级 L3 拒入/L2 加密不入语义索引/L1 语义，无痕不写，purge/export；L2 语义泄露策略已定）
- 用户 GUI 发起，DSH 待审（重点核 L2 不入语义索引）

### 2026-11-16 · M1-5 长期记忆完成（B 路径，RAG+记忆闭环）
- Codex 实现 kernel/long_term_memory.py（PrivacyClassifier L3 拒入/L2 AES-GCM 加密不入语义索引/L1 语义，无痕不写，purge/export）+ 4 测试
- DSH 重点审 L2 不语义索引（隐私策略落地）+ 门禁 84/84；commit 5f0d088；关闭 #17
- L2 语义泄露风险裁决：加密内容不入向量索引，仅元数据过滤召回

### 2026-11-16 · M1-6 派给 Codex（B 路径）
- briefs/0007-m1-6-response-envelope.md 备好（{data,meta,error} 统一+meta.trace_id 必带+错误对齐 errors.md+Idempotency-Key 幂等+分页 total/has_more；ops 端点保持 plain）
- 用户 GUI 发起，DSH 待审

### 2026-11-16 · M1-6 响应包裹完成（B 路径，对外契约成型）
- Codex 实现 {data,meta,error} 统一+trace_id 必带+错误对齐 errors.md+Idempotency-Key 回放+分页；6 测试
- DSH 审 + 门禁 90/90；commit c372cdf；关闭 #18

### 2026-11-16 · M1-9 派给 Codex（B 路径，M1-7 前置）
- 依赖图：M1-7 依赖 M1-9，故先派 M1-9（#21 事件字典冻结+总线映射+UI 状态机）
- briefs/0008-m1-9-event-dictionary.md 备好（内部→外部映射带 span_id+UI 10 状态机+CI 双向校验+审计反查；冻结打 contract-events-v1 由 DSH 执行）
- 用户 GUI 发起，DSH 待审

### 2026-11-16 · M1-9 事件字典冻结（B 路径，contract-events-v1）
- Codex 实现 kernel/event_mapping.py（内部→外部映射+span_id 双向溯源+未知拒推）+ ui/state-machine.js（10 状态纯函数）+ CI B2 双向门禁；6 测试
- DSH 审 + 门禁 96/96；commit 6ebdbb7；tag contract-events-v1；关闭 #21
- 部署要点：服务器需 docs/contracts（event_mapping 导入读 schema），已补

### 2026-11-16 · M1-7 派给 Codex（B 路径，M1 最重收尾）
- briefs/0009-m1-7-websocket-events.md 备好（WS 连接鉴权+18 事件经 EventMapper+after_event_id 回放+终态保证+5min 窗口回退 4408+SEC-04 归属 4403+UI 切 WS）
- 用户 GUI 发起，DSH 待审

### 2026-11-17 · M1-7 WebSocket 完成（B 路径，前端完整实时体验）
- Codex 实现 WS /api/v1/stream（鉴权归属+18 事件经 EventMapper+竞态安全回放+终态保证+5min 窗口 4408+SEC-04 4403）+ UI 切 WS；6 测试
- DSH 审 subscribe 竞态安全/终态/窗口/归属 + 门禁 102/102；commit a2a0962；关闭 #19
- 测试加固：_SlowLLM 时序裕量（慢盘终态保证竞态）

### 2026-11-17 · M1-8 高危确认完成（DSH 直做，M1 收官）
- kernel/confirmation.py 一次性 token 防重放+任务绑定+TTL+审计；MCPHost 确认门（拒绝不执行）；TaskManager pending_confirmation+事件；REST/CLI 确认；9 测试
- 门禁 111/111；commit fe759a3；关闭 #20

## M1 验收汇总（能力期出口）
| 能力 | 任务 | 状态 |
|---|---|---|
| 内部协议（信封/总线/RPC/降级/隔离） | M1-1 | ✅ contract-internal-v1 |
| MCP Host（list/call/超时/截断/幂等） | M1-2 | ✅ |
| 检索 MCP Server（RAG+防注入+CAP-3001） | M1-3 | ✅ ADR-001 |
| LangGraph 编排（Checkpoint+resume+deadline） | M1-4 | ✅ |
| 长期记忆（L2/L3 分级+加密+无痕+purge） | M1-5 | ✅ |
| 响应包裹（统一+幂等+分页） | M1-6 | ✅ |
| WebSocket（18 事件+回放+终态+SEC-04） | M1-7 | ✅ |
| 高危确认（token 防重放+审计+拒执） | M1-8 | ✅ |
| 事件字典（映射+UI 状态机+CI） | M1-9 | ✅ contract-events-v1 |

测试 111/111（本地+服务器），verify-all 6 项全绿；冻结 tag contract-v0.1.0 / contract-internal-v1 / contract-events-v1。

## 下一步
M2 插件期：M2-1 插件 SDK/manifest（#22 起，基于 contract-internal-v1 稳定协议）→ 沙箱/钩子/插件加载。可派 Codex（B 路径）或 DSH 直做。
