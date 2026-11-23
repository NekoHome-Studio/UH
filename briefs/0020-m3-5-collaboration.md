# Brief 0020：M3-5 CrewAI 按需启停（#32）

## 角色与约定
你是实现者（Codex），先读 AGENTS.md。只加/改代码与测试，**不要 git commit**（DSH 审后提交）。
**M3-1~4 已完成**（compose/K8s/指标/OTel，331 passed + 11 skip，verify-all **11 项 A~K**）。本任务 #32 承接。

## 硬约束
- **禁 pip install 到本机环境**；新依赖走 optional extra 并说明理由。
- 禁写 `$null`/`%VAR%`/`~` 未展开路径；密钥严禁明文。
- **契约冻结只增不改**；不得削弱现有 331 测试（11 skip 为 OTel 未装，属正常）。

## 必读
- uh-issues/issues/M3-5.md（#32 任务/验收/**风险：0→1 冷启动 30s 硬指标，镜像体积与预热策略要提前优化**）
- **kernel/scaler.py（M3-2 已产出，直接复用）**：CrewAIScaler.scale(target, replicas, trace_id) → ScaleResult(ok/rejected/degraded)，_TARGET=uh-crewai、replicas∈{0,1}、timeout≤30s、审计 audit.scaler.result、无集群 degraded
- kernel/orchestration.py（编排服务，协作任务入口）、kernel/router.py（路由判定 task_type）、kernel/protocol.py（EventBus + CapabilityRegistry + StorageGuard）
- docker-compose.yaml（uh-crewai `profiles: [collaborate]`）、deploy/k8s/{deployments,hpa}.yaml（crewai replicas 0，HPA 上下限 1）
- docs/contracts/sandbox.md（模块存储隔离）、kernel/plugins.py（StorageGuard 命名空间声明）

## 需求
### 1. 生命周期编排（核心）
`kernel/collaboration.py`（或扩展 orchestration）：**CollaborationManager**
- **触发**：路由判定 task_type=collaborate（或编排需要多智能体）时，调 CrewAIScaler 扩为 1。
- **就绪等待**：轮询 readiness（K8s status.readyReplicas / Compose 健康检查），**30s 内就绪**（硬指标）；超时则降级返回 degraded 并审计，不阻塞主链路。
- **缩回**：任务完成/失败/取消后缩回 0；**并发保护**——多个协作任务在跑时不能提前缩容（引用计数或 in-flight 集合），最后一个完成才缩 0。
- **空闲态**：无协作任务时副本恒 0（验收项）。
- 全程发事件总线事件（如 `collaboration.scaled_up` / `ready` / `scaled_down` / `timeout`，遵循 ADR-002 三段命名），带 trace_id。

### 2. 交互边界（验收：不直接读写其他模块存储）
- CrewAI **只经 EventBus + CapabilityRegistry RPC** 与内核交互；**禁止直接访问 retrieval/memory/checkpoint 存储**。
- 用 **StorageGuard** 声明 crewai 命名空间并断言跨模块访问被拒（复用 M1-1 机制）。
- 若需检索/记忆，经 RPC 调用内核能力，不自己开库。

### 3. 冷启动优化（风险项，必须给策略）
- 分析并落地：**镜像瘦身**（多阶段/精简依赖/slim 基础镜像）、**预热**（imagePullPolicy IfNotPresent、可选 idle 预拉取）、**就绪探针调优**（initialDelaySeconds/periodSeconds 让 30s 内可判定）。
- Compose 侧：profiles collaborate 按需 `docker compose --profile collaborate up -d uh-crewai`，文档给命令。
- **报告须给出 30s 预算分解**（拉镜像/启动/就绪各占多少，以及无缓存首次拉取超 30s 时的降级口径）。

### 4. 部署清单更新
- deploy/k8s：crewai Deployment 探针参数调优 + resources 合理（平时 0 不占资源）；HPA 保持上下限 1。
- compose：确认 profiles 正确、健康检查存在。
- `docs/deployment/collaboration.md`：触发条件、扩缩流程、30s 预算、故障降级、存储边界说明。

### 5. 测试（不依赖真集群/真 CrewAI）
`tests/unit/test_collaboration.py`：
- 触发扩容：路由 collaborate → scaler.scale(1) 被调用
- **空闲副本 0**：无任务时不扩、任务结束缩 0
- **并发保护**：两任务并行，先完成的不缩容，最后一个才缩 0
- **30s 超时降级**：模拟就绪永不达成 → degraded + 审计事件，主链路不崩
- **存储边界**：crewai 跨模块访问 retrieval/memory 被 StorageGuard 拒（KRN-2010）
- 事件总线：scaled_up/ready/scaled_down/timeout 事件发出且带 trace_id
- 无集群时整体 degraded 而非异常

## 验收（#32）
- 空闲时副本 0（测试断言）
- 触发后 30s 内就绪（就绪轮询 + 超时降级；真实冷启动计时待集群，如实标注）
- 协作模块不直接读写其他模块存储（StorageGuard 测试）

## 门禁（跑并报真实数字）
- `python -m pytest -q`（≥331 全绿，skip 数说明）
- `node scripts/verify-all.mjs --full`（11 项全绿；新增校验项须保持既有不变）
- `node scripts/secret-scan.mjs`（rc=0）

## 报告
新增改动文件 + 生命周期设计（触发/就绪/并发保护/缩容）+ **30s 冷启动预算分解与优化手段** + 存储边界证明 + 三门禁真实数字 + 未实测部分（无集群/无真 CrewAI 镜像）如实标注。**不要提交**。
