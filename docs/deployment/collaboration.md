# CrewAI 按需启停（#32）

创建消息任务时，路由命中 `task_type=collaborate`（包括显式 `mode=collaborate`），内核通过 `CapabilityRegistry` 调用 `collaboration.run`。Worker 用一个分析 Agent 和一个综合 Agent 顺序执行，并把答案返回任务流。现有 `workflow` 继续走 LangGraph。

## Kubernetes

从仓库根目录构建镜像，发布到所有候选节点可访问的仓库，并在 Deployment 使用同一个版本标签或镜像摘要。

```sh
docker build -f deploy/crewai/Dockerfile -t ultra-harness/crewai:0.1.0 .
```

空闲时目标副本为 0。CPU HPA 上下限都是 1，目标为 0 时暂停调节。首个任务使用 M3-2 `CrewAIScaler` 请求 `0→1`，等 Deployment 观察到当前 generation 且 `status.readyReplicas=1`，再通过 Service 确认 `/ready` 后发起 RPC。两个内核 Pod 在 Redis 的协作命名空间共享在途租约，并串行执行扩缩容。多个协作调用可以重叠，最后一个调用结束、失败或取消后异步请求缩回 0。租约最长 180 秒，覆盖有界的 RPC。内核每 10 秒检查一次待回收状态，处理异常退出留下的租约及可恢复的缩容失败。Redis 不可达时协作启动降级。待 Redis 和 Kubernetes API 恢复后，内核继续回收在 Redis 中有记录的 Worker。 [Kubernetes HPA](https://kubernetes.io/docs/concepts/workloads/autoscaling/horizontal-pod-autoscale/#implicit-maintenance-mode-deactivation)

`collaboration.worker.scaled_up`、`collaboration.worker.ready`、`collaboration.worker.scaled_down`、`collaboration.worker.timeout` 和 `collaboration.worker.degraded` 经内部 EventBus 发布，携带 `trace_id` 和 `task_id`。Kubernetes 缩放由 `audit.scaler.result` 记录，协作失败由 `audit.collaboration.result` 记录。部署、就绪等待或 Worker 执行失败时，消息任务发出 `task.degraded` 后继续普通对话。启动超时代码为 `UH-CAP-5021`。后台缩容确认与对话回退并行。

## Compose

默认启动保留 `collaborate` profile 关闭，由宿主机按需管理容器。先构建或预拉镜像，再启动协作服务并提交协作任务。

```sh
docker compose --profile collaborate build uh-crewai
docker compose --profile collaborate up -d uh-crewai
docker compose --profile collaborate ps uh-crewai
```

`ps` 可查看容器健康状态。内核轮询容器健康检查使用的同一个 `/ready`，就绪后调用协作 RPC。任务都结束后由宿主机执行 `docker compose --profile collaborate stop uh-crewai`。启动时可加 `--wait --wait-timeout 30` 让宿主命令等到 `healthy`。Compose 保持 Docker 控制面在宿主机，自动扩缩由 Kubernetes 部署提供。 [Docker Compose up](https://docs.docker.com/reference/cli/docker/compose/up/)

## 冷启动预算

| 阶段 | 规划预算 | 优化措施 |
| --- | ---: | --- |
| 镜像拉取及解包 | 15 秒 | 多阶段 `python:3.12-slim-bookworm`，镜像只安装可选 `crewai` 运行依赖和 Web 服务，避开构建缓存、字节码、工具和嵌入扩展。`IfNotPresent` 复用节点缓存。 |
| 控制面、调度和进程启动 | 10 秒 | 预先构建 wheel 层。启动时加载 SDK 和检查模型配置，任务到达时才发模型请求。CrewAI Pod 请求 `250m` CPU 和 `256Mi` RAM。 |
| 探针与状态传播 | 5 秒 | Kubernetes 就绪探针无初始延迟，每 2 秒检查一次，单次 1 秒超时。Scaler 每 0.5 秒查状态。Compose 健康检查每 2 秒执行，内核每 0.5 秒轮询 `/ready`。 |

在可调度节点空闲时可用其容器运行时预拉发布镜像，例如 `sudo crictl pull ultra-harness/crewai:0.1.0`。预拉保留 0 个 CrewAI Pod。Compose 可在宿主机执行 `docker compose --profile collaborate pull uh-crewai` 预填镜像缓存。缓存命中、镜像压缩体积和真实冷启动时间需要在验收环境实测。无缓存首次下载、调度排队或探针失败若使总时间超过 30 秒，内核返回协作降级并继续普通对话。Kubernetes 侧会回收可能已经接受的扩容。

## 存储与调用边界

内核为 `collaboration.run` 声明 `crewai` 命名空间。`StorageGuard` 对该能力访问 `retrieval`、`memory`、`long_term`、`orchestration`、`checkpoint` 返回 `UH-KRN-2010`。Worker RPC 只接收任务输入和关联信封。Worker 镜像不含内核存储适配器，CrewAI 内建记忆和缓存关闭。Compose 独立网络仅连接内核和 Worker，Kubernetes 数据存储 NetworkPolicy 不授予 Worker 通路，部署也不为 Worker 挂载内核存储或注入存储凭据。未来如需检索或记忆，由内核能力 RPC 提供数据。

Worker 默认使用 `mock`。生产配置可选择 `UH_LLM_PROVIDER=openai`，并通过环境变量或部署 Secret 引用配置模型、端点和密钥。Kubernetes 真实模型流量受现有 LLM egress 策略约束。 `crewai` 可选 extra 固定 Worker 专用 SDK 版本，内核常规部署和单测无需安装。 [CrewAI LLM 配置](https://docs.crewai.com/en/learn/llm-connections)
