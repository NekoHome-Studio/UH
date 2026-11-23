# Brief 0018：M3-3 Prometheus 指标 + Alertmanager 告警（#30）

## 角色与约定
你是实现者（Codex），先读 AGENTS.md。只加/改代码与测试，**不要 git commit**（DSH 审后提交）。
**M3-1/M3-2 已完成**（compose + K8s，309 测试，verify-all **10 项 A~J**）。本任务 #30 承接。

## 硬约束
- **禁 pip install**（如需 prometheus_client 先查 pyproject 是否已有；确需新增依赖请在报告说明理由，DSH 决定是否批准）。
- 禁写 `$null`/`%VAR%`/`~` 未展开路径；密钥严禁明文。
- **契约冻结只增不改**；不得削弱现有 309 测试。

## 必读
- uh-issues/issues/M3-3.md（#30 任务/验收/风险）
- **docs/source/ 上游 v3 基线 §7.5.1 指标全表 + §7.5.2 告警规则**（docx 或 deepseek_markdown_*.md，**你需自行提取完整指标清单**——契约 openapi.yaml:707 只引用章节号未列表）
- kernel/main.py:522-531（现有 /metrics 仅 2 个 gauge：uh_memory_short_term_keys / uh_memory_long_term_entries）
- kernel/logging.py（结构化日志，指标可复用其事件流）、kernel/tasks.py（任务/步骤事件）、kernel/plugins.py（崩溃计数 crash_count/quarantined）、kernel/llm.py（token 统计 uh_token_usage_total）
- deploy/k8s/（M3-2 清单，Prometheus/Alertmanager 部署清单放同目录或 deploy/observability/）

## 需求
### 1. /metrics 暴露 §7.5.1 全部指标
- 先从上游提取完整清单（请求量/错误率/延迟分位/任务状态/插件崩溃/token 用量/记忆键数等），在报告中列出你提取到的指标名与类型。
- 现有 2 个 gauge 保留兼容。补齐 counter/histogram/gauge，**命名遵循 `uh_<域>_<对象>_<单位>`**。
- **标签基数控制（风险点，CI 要卡口）**：**禁止 session_id/task_id/trace_id/user_id 等高基数标签**；允许 method/route(模板化非原始路径)/status_code/module/model/plugin_name(受控集合)/outcome 等低基数维度。
- 路由标签须用**路由模板**（如 `/api/v1/sessions/{id}/messages`）而非原始 URL，防路径参数炸基数。

### 2. Alertmanager 三条规则
`deploy/observability/alert-rules.yaml`（Prometheus rule 格式）：
- **HighErrorRate**：错误率阈值告警，**分母为 0 时不得触发**（用 `> 0` 守卫或 `clamp_min`，明确写法）。
- **MemoryPressure**：内存压力（进程 RSS / 容器 limit 比例，或短期记忆键数增长）。
- **PluginCrashLoop**：插件崩溃循环（crash_count 增速 / quarantined 状态）。
- 每条含 expr / for / labels.severity / annotations.summary+description。
- 配套 `deploy/observability/prometheus.yaml`（scrape config 抓 kernel:8080/metrics）+ alertmanager 路由占位（webhook 接收器指向可配 env，不落真值）。

### 3. 高基数 CI 卡口（风险明确要求）
`scripts/verify-metrics-cardinality.mjs`：静态扫描指标注册点，断言无禁用标签名（session/task/trace/user/id 等）、路由标签走模板；接入 verify-all 新增 **K 项**（保持既有 10 项不变）。

### 4. 测试
`tests/unit/test_metrics.py`：
- /metrics 返回全部指标名与正确 TYPE 行
- 计数器随请求/错误递增；直方图有 bucket
- **无高基数标签**（解析文本断言标签键集合）
- 路由模板化（带 id 的请求不产生新时间序列）
- **告警规则单测**：解析 alert-rules.yaml，用模拟样本验证三条规则在阈值上触发、**分母 0 时不触发**（可用纯 Python 实现 expr 的等价判定逻辑或 promql 解析简化版；若无 promql 引擎则做表达式结构断言 + 边界数值用例）

## 验收（#30）
- 指标可在 Prometheus 查询（格式合规 + scrape 配置正确）
- 三条告警可人工触发（给出触发步骤与预期，测试覆盖阈值边界）
- 无高基数指标（K 项卡口 + 测试断言）

## 门禁（跑并报真实数字）
- `python -m pytest -q`（≥309 全绿）
- `node scripts/verify-all.mjs --full`（全绿含新 K 项）
- `node scripts/secret-scan.mjs`（rc=0）

## 报告
**你提取到的 §7.5.1 指标全表**（名/类型/标签）+ 新增改动文件 + 三条告警 expr 与分母守卫写法 + 基数控制手段 + 三门禁真实数字 + 未实测部分（无 Prometheus 实例）如实标注。**不要提交**。
