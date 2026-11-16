# Brief 0007：M1-6 错误码响应包裹（{data,meta,error} 统一 + Idempotency-Key + 分页）

## 角色与约定
你是实现者（Codex），在 UH! 仓库（Python 3.12 + FastAPI）工作，先读 AGENTS.md。只改代码与测试，**不要 git commit**（DSH 审查后提交）。这是轻量契约实装，重点是统一与一致，不引入新后端。

## 目标
所有业务 REST 响应统一 {data, meta, error} 包裹，meta.trace_id 必带，错误对象对齐 errors.md，支持 Idempotency-Key 幂等与分页 total/has_more。前端/SDK/插件共同契约。

## 必读上下文（仓库内）
- AGENTS.md；docs/contracts/errors.md（错误码事实源，code/message/retryable/retry_after_ms/detail）；docs/contracts/openapi.yaml（Envelope/ErrorEnvelope/Meta 已定义，page/page_size/total/has_more，Idempotency-Key 头）。
- kernel/main.py：现有端点——sessions/messages/tasks/dev-route 已部分用 {data,meta,error}；/health /ready /metrics 是 ops 端点（openapi 定义为_plain_ Health，**保持 plain 不包裹**）；trace 中间件已生成 trace_id 并写 X-Trace-Id 头。
- kernel/protocol.py / llm.py / mcp.py / retrieval.py：错误对象结构参考（MCPError/LLMError/OrchestrationError 的 to_dict）。

## 需求
1. 统一包裹助手：成功 {data, meta:{trace_id,...}, error:null}；失败 {data:null, meta:{trace_id}, error:{code,message,retryable,retry_after_ms,detail}}。所有**业务** REST 端点一致；ops（/health /ready /metrics）保持 plain（契约如此）。
2. meta.trace_id 必带（取自 trace 中间件/contextvars）。
3. 错误对象字段与 errors.md 完全对齐（code/message/retryable/retry_after_ms/detail）；异常统一映射（如 UH-USR-1003 参数错、UH-KRN-5001 未捕获）。
4. Idempotency-Key：POST 带该头 → 按 (key, 端点) 缓存首次结果，重复请求回放首次结果（M1 内存缓存即可）；无头则正常处理。
5. 分页：GET 列表端点（/sessions/{id}/messages、/plugins）支持 page/page_size，返回 total/has_more。
6. 不破坏现有端点行为与 84 个测试（/health 等 plain 响应测试保持）。

## 测试（必须）
- 契约测试覆盖成功/失败两类：业务端点响应结构 {data,meta,error} 一致，meta.trace_id 必带。
- 失败响应 error.code 与 errors.md 对齐（构造参数错/未捕获等）。
- Idempotency-Key 重复 POST 回放首次结果（断言两次响应一致/副作用不重复）。
- 分页 total/has_more 正确。
- ops 端点保持 plain（回归）。
- 不得削弱现有 84 个测试。

## 不动什么
- docs/contracts/ 冻结契约（openapi 已定义包裹，实装它而非改契约）+ ADR。
- kernel/protocol.py / llm.py / mcp.py / retrieval.py / orchestration.py / long_term_memory.py 核心逻辑；trace 中间件 trace_id 生成。
- 不新增公开端点。

## 门禁（必须跑并报告）
- python -m pytest -q  （期望全绿）
- node scripts/verify-all.mjs  （期望 VERIFY_ALL_PASSED）

## 报告契约
输出：新增/改动文件清单 + 设计要点（包裹助手/异常映射/幂等缓存/分页）+ pytest 结果 + verify-all 结果 + 成功失败契约/幂等回放/分页如何验证。不要提交。
