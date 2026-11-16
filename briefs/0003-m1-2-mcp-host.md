# Brief 0003：M1-2 MCP Host（tools/list + tools/call + 超时取消 + 截断 + 幂等重试）

## 角色与约定
你是实现者（Codex），在 UH! 仓库（Python 3.12 + FastAPI）工作，先读 AGENTS.md 宪章。只改代码与测试，**不要 git commit**（编排者 DSH 审 diff 后提交）。用 mock MCP server 测试，无需真实外部服务。

## 目标
实现 MCP Host（kernel/mcp.py）：UH! 作为 Host 统一接入外部 MCP Server 工具，不感知工具实现。供 M1-3 检索（ADR-001：LlamaIndex 走 MCP Server）与官方工具复用。

## 必读上下文（仓库内）
- AGENTS.md：主体/硬规则/门禁。
- kernel/protocol.py：M1-1 内部协议（Envelope/EventBus/CapabilityRegistry/StorageGuard）。MCP 工具应能注册进 CapabilityRegistry，经内部 RPC 调用。
- kernel/llm.py：适配层隔离 + 工厂模式参考（create_llm_client / MockLLMClient）。
- docs/contracts/errors.md：超时 UH-CAP-5021（detail.module=tool）；上游服务错误 UH-CAP-4003。
- docs/contracts/manifest/tool.schema.json：config.timeout_ms（单次调用超时上限）、risk_level。
- docs/contracts/events.md §6：mcp.tool.call / mcp.tool.result 内部事件（三段）。
- docs/contracts/adr/ADR-001-retrieval-mcp-server.md：检索走 MCP Server。
- docs/contracts/adr/ADR-002-event-naming-three-segment.md：内部事件三段命名。

## 需求
1. MCP Host 客户端：支持连接 MCP Server（stdio 子进程传输为主，可选 streamable HTTP）；JSON-RPC 2.0；initialize 握手 + tools/list + tools/call。可用官方 mcp SDK（pip install mcp，服务器用清华镜像 + NO_PROXY=*）或自实现最小 JSON-RPC 客户端——自选，但须适配层隔离，MCP 协议细节不穿透内核。
2. tools/list：发现工具，返回工具名/描述/inputSchema 列表。
3. tools/call：调用工具，传 name + arguments，返回 content 数组（text/image/resource）。
4. 超时主动取消：取 manifest.config.timeout_ms（默认 5000），Host 侧计时，超时主动取消请求（非被动等待），抛/映射 UH-CAP-5021（detail.module=tool, tool_name, timeout_ms）。
5. 截断：返回体积超 256KB（262144 字节）截断并标 truncated:true（防上下文撑爆/提示放大）。
6. 幂等重试：仅幂等工具（manifest 声明或 read-only risk_level）自动重试（指数退避，最大次数可配）；非幂等（write-host/irreversible）禁止自动重试。
7. 集成：把 MCP 工具注册进 CapabilityRegistry（action 如 mcp.tool.<name>），调用经内部 RPC；发 mcp.tool.call / mcp.tool.result 内部事件（三段，EventBus）。
8. 配置经环境变量/manifest，严禁硬编码密钥。

## 测试（必须，mock MCP server，无需真实服务）
- 起一个 mock MCP Server（stdio 子进程或 in-process JSON-RPC handler），提供 web-search 工具（read-only，幂等）。
- 覆盖：tools/list 发现 web-search；tools/call 端到端返回内容；超时 → UH-CAP-5021（detail.module=tool）；>256KB 返回 → truncated:true 且体积受限；幂等工具失败自动重试、非幂等不重试；CapabilityRegistry 经 RPC 调 MCP 工具。
- 不得削弱现有 64 个测试。

## 不动什么
- docs/contracts/ 冻结契约（errors/events/manifest/openapi/asyncapi）；ADR 不改。
- kernel/protocol.py / tracing.py / logging.py 既有行为；/health /ready router dev/route 既有端点。
- 不新增公开 REST 端点（MCP Host 是内部层）。

## 门禁（必须跑并报告）
- python -m pytest -q  （期望全绿，含新增）
- node scripts/verify-all.mjs  （期望 VERIFY_ALL_PASSED）

## 报告契约
完成后输出：新增/改动文件清单 + 设计要点（传输方式/SDK 选型一句话）+ pytest 结果（通过/失败数）+ verify-all 结果 + 超时/截断/幂等重试如何验证。不要提交。
