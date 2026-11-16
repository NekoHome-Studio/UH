# Brief 0004：M1-3 检索 MCP Server（LlamaIndex + Chroma，ADR-001 经 MCP Host 接入）

## 角色与约定
你是实现者（Codex），在 UH! 仓库（Python 3.12 + FastAPI）工作，先读 AGENTS.md。只改代码与测试，**不要 git commit**（DSH 审查后提交）。功能测试用小型语料 + 确定性 embedding，无需真实 API 密钥/大模型下载。

## 目标
实现检索能力为**独立 MCP Server**（ADR-001 裁决，取代任务卡原「内部 RPC」表述），暴露 search / index_document / query 工具；内核经 M1-2 MCPHost 调用。落地 RAG 检索 + 提示注入防护。

## 必读上下文（仓库内）
- AGENTS.md；docs/contracts/adr/ADR-001-retrieval-mcp-server.md（检索=MCP Server，search/index_document/query）。
- kernel/mcp.py：M1-2 MCPHost/Transport（InProcessTransport 可挂 mock server；StdioTransport 起子进程 server）。检索 server 须与 Host 协议兼容（JSON-RPC 2.0，initialize/tools/list/tools/call）。
- kernel/protocol.py：CapabilityRegistry（检索注册为 mcp.tool.search 等）。
- kernel/main.py：当前 _retrieval_cap 是 mock，search 模式经 RPC 调 retrieval.search——本任务用真实 MCP 检索替换。
- docs/contracts/errors.md：无结果 UH-CAP-3001（降级不阻断）；超时 UH-CAP-5021。
- docs/contracts/events.md / ADR-002：内部事件三段命名。
- v3 §4.3 / §9.5：检索结果 untrusted_content 包裹防注入；PDF/Office 剥离宏与外链。

## 需求
1. 检索 MCP Server（capabilities/retrieval/ 或 kernel/retrieval.py）：实现 MCP server 端（initialize/tools/list/tools/call），暴露 search(query,top_k) / index_document(path或content) / query 工具。可用 mcp SDK server 或自实现最小 JSON-RPC server（与 M1-2 Host 兼容）。
2. LlamaIndex 集成做索引/检索；向量库本地 Chroma。embedding 提供方可配（env UH_EMBEDDING_*，OpenAI 兼容或本地），**必须含确定性 fallback embedding（如词袋哈希向量）供离线测试**，不依赖网络/大模型下载。
3. 文档解析：先支持 PDF / Markdown（Office 延后，见风险）；解析时剥离宏与外链（§9.5）。
4. 安全：检索结果送 LLM 前经 untrusted_content 包裹（§9.5 提示注入防护）；注入样本（如「忽略以上指令」）不改变模型行为（作为不可信数据而非指令）。
5. 无结果 → UH-CAP-3001，降级不阻断对话。
6. 集成：把检索 MCP Server 接入 M1-2 MCPHost（register_capabilities），main.py search 模式经 Host 调真实检索（替换 mock _retrieval_cap）；检索不可用仍降级（M1-1 路径）。
7. 配置经 env/manifest，严禁硬编码密钥；适配层隔离（LlamaIndex/Chroma 细节不穿透内核）。

## 测试（必须，小语料 + 确定性 embedding，离线）
- index_document → search 往返命中；top_k 生效。
- untrusted_content 包裹正确；注入样本被当数据处理（断言包裹/隔离，不执行注入指令）。
- 无结果 → UH-CAP-3001。
- MCP tools/list 含 search/index_document/query；tools/call 经 M1-2 Host 端到端。
- 检索 server 不可用 → search 降级（task.degraded）对话仍完成。
- 不得削弱现有 69 个测试。

## 基准（提供 harness，诚实测量）
- scripts/bench_retrieval.py：自建文档集召回命中率 + 向量检索 P95。目标 500 篇召回≥70%、10k 文档 P95<500ms（v3 §10.4）。
- 用配置的 embedding 跑小规模演示并报告实测数字；**确定性 fallback embedding 的召回仅供基线，真实语义召回≥70% 需真实 embedding 模型**——如实报告，不得用 mock 冒充达标。完整 500/10k 基准需真实 embedding + 数据集，标注为待集成运行。

## 不动什么
- docs/contracts/ 冻结契约 + ADR；kernel/mcp.py / protocol.py / tracing.py / logging.py 既有行为；/health /ready router 既有端点。
- 不新增公开 REST 端点（检索是内部 MCP 能力）。

## 门禁（必须跑并报告）
- python -m pytest -q  （期望全绿）
- node scripts/verify-all.mjs  （期望 VERIFY_ALL_PASSED）

## 报告契约
输出：新增/改动文件清单 + 设计要点（MCP server 实现方式/embedding fallback/解析库选型）+ pytest 结果 + verify-all 结果 + 基准实测数字（注明 embedding 类型）+ untrusted_content/注入隔离/CAP-3001 如何验证。不要提交。
