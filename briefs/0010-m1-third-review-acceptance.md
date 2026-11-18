# Brief 0010：M1 三审验收（独立第三方·生产链路口径）

## 角色与独立性
你是**独立验收审查者**（非实现方 DSH）。只读审查代码/契约/运行时，**不修改任何受版本控制源码**，只新增你的审查报告。
**未阅读其他审查者的三审报告**，结论独立从「代码 + 契约 + 实测」得出。可参考一审/二审报告与修复回应作为基线对照。

## 基线
- commit **cb4d1e6** · tag **m1-review2-fixed-v0.1.0**
- 门禁声明：142/142（本地+服务器），verify-all full 8/8，CI validate+test 双绿，13 条绕过回归
- 你的首要任务：**独立复现门禁**，并验证二审修复在生产链路下真实生效、无新引入绕过。

## 必读材料（仓库内）
- docs/reviews/2026-11-17-独立验收就绪指南.md（逐项 finding→fix→回归→生产验证 + 8 项端到端脚本 + 结论模板）
- docs/reviews/2026-11-17-审查修复回应.md（五轮修复台账）
- 三份二审报告（GPT-6 / DeepSeek-V4-Flash / Hunyuan-Hy）作为待验证基线
- docs/contracts/（冻结契约 + ADR-001/002/003）

## 验收范围（生产链路可观测，非 fixture）
1. **门禁复现**：python -m pytest -q（142）；node scripts/verify-all.mjs --full（8）。
2. **鉴权**：UH_LOCAL_MODE=0 + UH_AUTH_REQUIRED=1 + token；无凭据业务端点（含 /memory/* /plugins 别名）全 401，ops 200；单设 UH_AUTH_REQUIRED=1 即生效；401 带 meta.trace_id。
3. **WS 实时流**：带 token 建任务收 task.created→delta→finished，event_id 连续；断线按 after_event_id 重连不丢；换 token 重校归属。
4. **工具确认 inline**：irreversible 任务 → confirmation_required → /confirm approved→tool.call/result→finished；rejected→cancelled；超时→resolved_by=timeout+cancelled。
5. **工具确认 RPC**：register_capabilities irreversible 经 RPC → fail-closed USR-1002 不执行；风险级不可被调用方降级。
6. **取消**：运行中 cancel → step.failed 闭合 + task.cancelled + 令牌失效；终态再 cancel/resume → 409。
7. **超窗回退**：超 5min 重连 → 4408 → GET /steps 含 task_id 全量阶段。
8. **隔离/隐私**：多用户幂等键跨客户端不复用、无 Set-Cookie 泄漏；StorageGuard 跨模块拒绝；L3 拒入（含 override）；L2 加密不语义索引；无痕不写。
9. **UI**：确认弹窗批准/拒绝生效；游标重连；4408 渲染 steps；degraded 徽标；4401 正确报错。

## 重点揪新回归
二审修复本身可能引入新洞（前几轮已发生：别名绕过/Host 注入/Cookie 泄漏/形式接线）。**特别检查修复路径的绕过面**：
- 鉴权豁免清单是否漏端点；dev 门禁是否全覆盖；fail-closed 是否有旁路；
- 幂等 in-flight 锁是否真防并发；归属守卫在单共享 token 下的实际效力；
- StorageGuard.access 是否真接入所有存储路径（含 Chroma/LlamaIndex 后端，非仅 LocalVectorStore）；
- checkpoint_id 稳定后 resume 语义；trace token reset 嵌套正确性。

## 已知设计局限（非缺陷，知悉即可，勿当 blocker）
- REST 完全多用户隔离需 per-user principal（当前单共享 UH_AUTH_TOKEN 单用户+口令模型）。
- 记忆 scope 默认 global 是用户 ADR-003 裁决（session 可选）。
- 4004 retryable 服务端裁定（契约允许）。

## 报告契约
输出 docs/reviews/2026-11-18-M1-三审验收报告-<模型名>.md：
- 门禁复现结果（亲测）
- 二审逐项验收判定（已修/部分/未修 + 证据）
- 新发现（按严重性，带 file:line + 复现）
- 生产链路 8 项逐条结论
- **验收结论**：通过 / 有条件通过 / 不通过 + 签署模型名
- 探针副作用记录（如有，须先隔离存储到临时目录）

## 纪律
- 探针前先 UH_LONG_TERM_STORE/UH_RETRIEVAL_STORE 指临时目录，防误清运行态。
- 不改源码/契约；只新增报告。
- 以生产链路可观测为准，fixture 全绿不作验收依据。
