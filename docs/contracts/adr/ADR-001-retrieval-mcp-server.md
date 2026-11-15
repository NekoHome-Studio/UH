# ADR-001：检索模块经 MCP Server 暴露

- 状态：**Accepted**（2026-11-15，用户裁决「走 MCP Server」）
- 关联：A1（基线变更-v3-差异评估）· 影响 M1-1 / M1-2 / M1-3
- 取代：基线评估 A1 原建议（内部 RPC 优先）

## 背景
v3 文档自相矛盾：§4.3 标题为「检索模块（LlamaIndex MCP Server）」并描述暴露 search / index_document / query 工具；但 §6.6 模块间通信协议与 §11.1 术语表（L1700）称能力模块「通过内部协议与内核通信」。检索接口形态必须在 M1-1 冻结内部协议前定案。

## 决策
检索模块（LlamaIndex）实现为**独立 MCP Server**，暴露 search / index_document / query 工具；内核作为 **MCP Host** 经 MCP 协议（tools/list + tools/call）调用检索，与外部工具统一链路。

## 备选
- A · 内部 RPC（原建议）：检索作为能力模块经 §6.6 信封调用，与编排/协作/记忆一致。
- B · MCP Server（选定）：检索复用 MCP 工具链路，对外暴露形态统一，生态可替换检索实现。

## 后果
1. M1-2 MCP Host 成为检索前置依赖：检索复用 tools/list、tools/call、超时取消、256KB 截断、幂等重试。
2. M1-3 检索服务 = MCP Server：实现 search/index_document/query 三工具；不再走内部 RPC 信封。
3. M1-1 内部协议信封仍用于编排/协作/记忆等非 MCP 能力模块；检索经 MCP 适配层隔离。
4. 事件映射：检索调用经 mcp.tool.call / mcp.tool.result；capability.invoked/finished 用于非 MCP 能力。检索无结果 UH-CAP-3001（降级不阻断），超时 UH-CAP-5021（detail.module=retrieval）。
5. 部署：deploy-hardening NetworkPolicy 不变——llama_index MCP 端口仍 expose，仅内核可达。
6. 文档修订（M1 阶段）：v3 §6.6 / §11.1 术语表检索通信表述需同步为 MCP；§4.3 保留。
7. 用户 API 不变：search 仍经 POST /sessions/{id}/messages（mode=search），不新增公开 REST 端点；OpenAPI/AsyncAPI 不受影响。

## 仍待裁决
- 事件命名 2/3 段（capability.invoked 两段 vs §6.6 三段约定）——M1-1 冻结前定案（与 A1 独立）。
