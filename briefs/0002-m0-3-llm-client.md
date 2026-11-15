# Brief 0002：M0-3 LLM 调用封装（流式 / 超时重试 / Token 统计）

## 角色与约定
你是实现者（Codex），在 UH! 仓库（Python 3.12 + FastAPI）工作。只改代码与测试，**不要 git commit**（编排者 DSH 负责审 diff 后提交）。本任务用 mock provider 测试，不需要真实 API 密钥。

## 目标
实现内核的 LLM 调用封装层（kernel/llm.py），供后续 chat/search 链路使用：OpenAI 兼容协议、流式输出、超时与指数退避重试、错误映射到 UH-CAP-4xxx、Token 统计。

## 必读上下文（仓库内）
- docs/contracts/errors.md：错误码事实源。LLM 上游错误归 CAP-4xxx：
  - UH-CAP-4001 Token 超限（HTTP 400，retryable=false）
  - UH-CAP-4002 上游限流（429，retryable=true，带 retry_after_ms）
  - UH-CAP-4003 上游服务错误（502，retryable=true）
- kernel/logging.py：用 log_event(logger, level, event, **fields) 记结构化日志（发射时绑定 trace_id）。
- kernel/tracing.py：trace_id 上下文。
- kernel/main.py、kernel/router.py：现有代码风格参考。
- tests/unit/：现有测试风格（pytest + FastAPI TestClient）。

## 需求
1. Provider 无关的客户端接口（如 LLMClient），默认实现 OpenAI 兼容（POST {base_url}/chat/completions，stream=true 走 SSE）。
2. 配置经环境变量：UH_LLM_BASE_URL、UH_LLM_API_KEY（或密钥引用名）、UH_LLM_MODEL、UH_LLM_TIMEOUT_MS。**严禁把密钥硬编码进代码/测试/日志**。
3. 流式：异步生成器逐块 yield delta；保证完整无截断；解析 SSE data: 行与 [DONE]。
4. 超时 + 重试：请求超时（默认 UH_LLM_TIMEOUT_MS），指数退避重试（仅对可重试错误：429/5xx/网络超时；400/401 不重试）；最大重试次数可配。
5. 错误映射：上游错误 → 上述 CAP-4xxx，抛出带 code/retryable/retry_after_ms 的结构化异常（与 errors.md 对齐）。
6. Token 统计：累计 prompt_tokens/completion_tokens（按 model），暴露 uh_token_usage_total 指标结构（M0 可先记日志 + 内存计数，/metrics 完整暴露留 M3-3）。
7. 适配层隔离：业务只依赖 LLMClient 接口，厂商协议差异不穿透。

## 测试（必须，mock provider，无需真实密钥）
- 用本地 mock（如 httpx MockTransport 或内嵌 aiohttp/pytest fixture 起一个假 SSE 服务）覆盖：
  - 流式完整拼接（多块 delta → 完整文本，含 [DONE]）
  - 超时 → 重试 → 最终 UH-CAP-4003
  - 429 → UH-CAP-4002 + retry_after_ms + 退避重试
  - 400 token 超限 → UH-CAP-4001 不重试
  - Token 统计正确累加
- 不得削弱现有 24 个测试。

## 不动什么
- docs/contracts/ 全部契约、openapi.yaml/asyncapi.yaml。
- kernel/tracing.py、logging.py 的既有行为；/health、/ready、router、dev/route。
- 不新增公开 REST 端点（LLM 封装是内部层）。

## 门禁（必须跑并报告）
- python -m pytest -q  （期望全绿，含新增测试）
- node scripts/verify-all.mjs  （契约不被破坏，期望 VERIFY_ALL_PASSED）

## 报告契约
完成后输出：新增/改动文件清单 + 设计要点一句话 + pytest 结果（通过/失败数）+ verify-all 结果。不要提交。
