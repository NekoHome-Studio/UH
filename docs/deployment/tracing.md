# OpenTelemetry 链路追踪

内核默认关闭追踪。启用时，运行环境需包含 `otel` 可选依赖，同时设置 `UH_OTEL_ENABLED=1` 和 `OTEL_EXPORTER_OTLP_ENDPOINT`。缺少可选依赖时内核会继续运行。容器构建使用 `UH_INSTALL_EXTRAS=compose,otel`，独立 Python 环境可安装 `.[otel]`。

## 配置

| 环境变量 | 用途 |
| --- | --- |
| `UH_OTEL_ENABLED` | 显式开关，默认关闭。接受 `1`、`true`、`yes`、`on`。 |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP 通用端点。gRPC 示例为 `http://jaeger:4317`，HTTP 示例为 `http://jaeger:4318`。HTTP 自动追加 `/v1/traces`。 |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `grpc`（默认）或 `http/protobuf`。 |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | 可选的 trace 专用端点。HTTP 使用完整 URL（含 `/v1/traces`），仍需配置通用端点来开启追踪。 |
| `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL` | 可选的 trace 专用协议，优先于通用协议。 |
| `OTEL_EXPORTER_OTLP_HEADERS`、`OTEL_EXPORTER_OTLP_TRACES_HEADERS` | SDK 读取的可选请求头配置。凭据通过部署环境注入。 |
| `UH_OTEL_SAMPLE_RATIO` | 本地根 trace 的成功链路采样比例，范围 `0` 到 `1`，默认 `1.0`。生产建议从 `0.1` 开始。 |
| `UH_OTEL_TAIL_SAMPLING` | `local`（默认）在单个服务内保留错误，`collector` 由共享 Collector 做跨服务保留决定。 |

内核的 Resource 使用 `service.name=uh-kernel` 和包的 `service.version`。HTTP 入口优先续接有效的 W3C `traceparent`，其次使用有效的 `X-Trace-Id`。非法或全零 ID 会生成新 ID。MCP HTTP 客户端注入 `traceparent` 和可用的 `tracestate`。进程内能力 RPC 沿用当前上下文。独立检索服务可用 `python -m uvicorn kernel.retrieval:create_mcp_app --factory --host 0.0.0.0 --port 8080` 启动，在私有网络暴露 `/mcp`，并由内核的 `UH_RETRIEVAL_MCP_URL` 指向该地址。检索服务使用同一组追踪配置，服务名为 `uh-retrieval`。stdio 工具在内核侧有调用 span。

`uh_trc_` 后的 32 位十六进制字符串就是 Jaeger 的 trace ID。请求启动的后台任务和 WebSocket 事件沿用该 ID。幂等重放返回缓存中的原始响应和原始 trace ID。重放请求本身有独立的入口 span。W3C 保留完整 64 位 span ID，信封内 `sp_<8hex>` 对应其低 32 位，`parent_span_id` 采用同一映射。

## 采样与告警

- 成功链路使用 `ParentBased(TraceIdRatioBased)`。本地根节点按 trace ID 作确定性判断，跨服务续接遵循上游采样位。仅就无远端父 span 的成功链路而言，比例 `0` 丢弃，比例 `1` 保留。
- `local` 模式在请求与后台任务执行期间暂存未选中的 span。HTTP 4xx/5xx、未处理异常、最终 MCP 错误、能力降级或异步任务失败都触发本服务完整片段的保留，包括此前完成的 span。请求与后台任务均完成后释放仍未选中的 span。
- 多服务完整错误链路需要把所有可控服务设为 `collector` 并发送到同一套尾采样 Collector。启动 OpenTelemetry Collector Contrib 时，将 `UH_TRACE_BACKEND_OTLP_HTTP_ENDPOINT` 指向 Jaeger 或 Tempo 的 OTLP HTTP 基础端点。服务上报所有 span，并附加低基数的成功采样候选标记。Collector 在 8 分钟内合并同一 trace ID，保留含 ERROR span 或成功采样候选的链路。多实例部署时按 trace ID 路由，按并发调整 `num_traces`，按最长任务与重试预算调整 `decision_wait`。缺少 span、超过窗口或缓存容量都会影响完整性。上游服务也需要记录并发送错误判定所需的 span。[Collector 配置](../../deploy/observability/otel-collector.yaml)
- Prometheus 指标按全量请求、错误和能力调用计数，Alertmanager 规则继续读取这些指标。在 Jaeger 中直接以抽样 trace 计算错误率会高估整体错误率，因为错误链路会被优先保留。

§7.6.2 描述的操作名为 `router.decide`、`capability.retrieve`、`mcp.call`、`memory.write`。入口使用 `http.request`，检索 HTTP 端使用 `mcp.receive`。其它内部操作使用 `capability.generate`、`capability.orchestrate`、`capability.call`、`task.resume`、`task.failed`。HTTP 路由采用模板或 `unmatched`。附加属性限于路由模板、HTTP 状态码和 Collector 候选标记。

## Compose 启动与验证

先配置 Compose 密钥文件，再从仓库根目录运行以下命令。验证夹具使用内核及它的依赖，检索能力由内核内置 MCP Server 提供。[Compose 部署说明](docker-compose.md)

```sh
docker compose -f docker-compose.yaml -f deploy/observability/jaeger.yaml config --quiet
docker compose -f docker-compose.yaml -f deploy/observability/jaeger.yaml up -d --build uh-kernel
```

追踪 overlay 会构建带 SDK 的内核并通过 Compose 网络连接 Jaeger。Jaeger UI 在 `127.0.0.1:16686`，OTLP 端口只在 Compose 网络内使用。Jaeger 本地实例使用内存存储。

1. 向 `POST /api/v1/sessions` 发送已鉴权请求取得 `data.session_id`。保留返回的 `uh_client_id` cookie，与相同的鉴权头一起用于后续请求。
2. 向 `POST /api/v1/sessions/{session_id}/messages` 提交 `{"content":"MCP","mode":"search","context":{"memory_recall":false}}`。记录 `202` 响应的 `data.trace_id`，并等任务完成及下一次 SDK 批量导出。
3. 去掉 `uh_trc_` 前缀，打开 `http://127.0.0.1:16686/trace/<32hex>`。应能看到 `http.request` 下的 `router.decide`、`capability.retrieve` 和 `memory.write`，以及 `capability.retrieve` 下的 `mcp.call`。通过独立检索服务时，`mcp.call` 下还会出现 `mcp.receive`。
4. 可将 `UH_OTEL_SAMPLE_RATIO` 设为 `0` 后重建内核容器，分别请求 `GET /health` 和带鉴权的 `GET /api/v1/tasks/missing`。后者返回 `404`，用响应中的 `X-Trace-Id` 查询应仍能找到链路。两次请求都会反映在 `/metrics`。
