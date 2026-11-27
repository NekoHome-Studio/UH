# Kubernetes 部署与验收

使用支持 NetworkPolicy 的 CNI、提供 `metrics.k8s.io` 的指标服务，以及 Linux 节点。用本仓库的 Dockerfile 构建并发布内核镜像。能力镜像与 Compose 同名，监听 TCP 8080 并实现 `/health` 和 `/ready`。CrewAI 的镜像构建见 [协作部署](collaboration.md#kubernetes)。Ingress 控制器运行在 `ingress-controller` 命名空间，后端指向 `uh-prod/uh-kernel:8080`。DNS 策略选择 `kube-system` 中标签为 `k8s-app=kube-dns` 的 Pod。

Redis、Chroma、PostgreSQL 各有一个 StatefulSet 和独立 PVC。PVC 使用集群默认 StorageClass。如需指定，请在首次部署前为相应的 `spec.volumeClaimTemplates[].spec.storageClassName` 设置集群中的存储类名称。内核 Pod 的本地会话、任务和插件状态各自独立。

## 首次部署

应用 Deployment 前，在所有可调度目标节点安装 seccomp profile 并配置 Pod PID 限额。运行需要子进程的插件前配置内核的 bubblewrap 启动 profile。 [系统级安全加固](security-hardening.md#compose-和-kubernetes)

由部署环境创建 `uh-prod`，再从外部密钥源注入 [Secret 模板](../../deploy/k8s/secrets.yaml) 的占位符。注入器应保留字符串类型和换行，缺少必填值时应停止。信任库的值为 JSON，暂无受信插件时使用空对象。使用 mock LLM 时把 `UH_LLM_API_KEY` 注入为空字符串。渲染后的对象直接通过 stdin 交给 `kubectl create -f -`，无需写入文件。下面的例子依赖项目已有的 PyYAML，并从已注入的环境变量读取值。

```bash
kubectl apply -f deploy/k8s/namespace.yaml
python - <<'PY' | kubectl create -f -
import json
import os
from pathlib import Path

import yaml

secret = yaml.safe_load(Path("deploy/k8s/secrets.yaml").read_text(encoding="utf-8"))
for key, reference in secret["stringData"].items():
    name = reference[2:-1]
    value = os.environ.get(name)
    if value is None or (name != "UH_LLM_API_KEY" and not value.strip()):
        raise SystemExit(f"Missing required environment variable {name}")
    secret["stringData"][key] = value
print(json.dumps(secret))
PY
```

真实 LLM 出网前，将 `llm-egress` 中的文档示例地址替换为已批准的外部 TLS 端点或出网网关的 IPv4 `/32` 或 IPv6 `/128`。确认这些地址属于集群外，并将内核及 CrewAI 的模型地址和供应商设为实际配置。标准 NetworkPolicy 按 IP 和端口匹配，供应商地址变化时需要更新清单。

Scaler 默认使用 Kubernetes 注入的 Service 地址和 Pod 的 ServiceAccount 凭据。部署时应确认内核 Pod 在默认拒绝下仍可访问控制面。一种部署方法是在运行内核的节点上提供同节点 TLS 控制面入口，通过 Downward API 的 `status.hostIP` 给内核设置 `UH_K8S_API_HOST`，并通过 `UH_K8S_API_PORT` 注入监听端口。入口证书须由 Pod 挂载的集群 CA 信任并覆盖所用地址。这个入口由集群运维提供。若 API 不可达，scaler 返回 `degraded` 并写审计记录。 [Kubernetes NetworkPolicy](https://kubernetes.io/docs/concepts/services-networking/network-policies/)

```bash
kubectl apply -f deploy/k8s/rbac.yaml -f deploy/k8s/networkpolicies.yaml
kubectl apply -f deploy/k8s/services.yaml -f deploy/k8s/datastores.yaml
kubectl apply -f deploy/k8s/deployments.yaml -f deploy/k8s/hpa.yaml
kubectl get deployments,hpa,statefulsets,pvc,networkpolicy -n uh-prod
```

## 验收

- LlamaIndex 与 LangGraph 的 HPA 下限为 2、上限为 5，CPU 目标为 70%。待两个副本就绪后，在验收环境模拟其中一个副本崩溃，检查内核 `/health` 和 `/ready`、另一副本及其他能力服务。独立能力镜像应在不就绪时让 `/ready` 返回非成功 HTTP 状态。
- 对 LlamaIndex 或 LangGraph 施压，检查 `kubectl get hpa -n uh-prod -w` 的 CPU 和副本变化。CrewAI 从 0 启动，由 scaler 设置 1 或 0，HPA 上下限为 1。CPU HPA 在目标为 0 时暂停。部署文件包含 CrewAI 的初始副本数，重新应用该文件会把目标设回 0。 [Kubernetes HPA](https://kubernetes.io/docs/concepts/workloads/autoscaling/horizontal-pod-autoscale/)
- 从无标签验收 Pod 对能力模块发 HTTP 请求，预期超时。用内核 Pod 确认能力端口和三个存储端口可达，另核对 DNS、Ingress 与配置后的 LLM TLS 端点。LangGraph 只获得 Redis 和 PostgreSQL 的存储通路，LlamaIndex 只获得 Chroma 的存储通路。
- 从消息入口提交 `mode=collaborate` 任务，确认 `audit.scaler.result` 中出现同一 trace 下的扩容与回收记录。通过 Deployment 的 `status.readyReplicas` 确认 30 秒内就绪，任务结束后观察副本数回到 0。缓存未命中时的超时降级和验收预算见 [协作部署](collaboration.md#冷启动预算)。

## RBAC 审计

使用有 impersonation 权限的运维身份，纳入 ServiceAccount 自带组，核对 [冻结契约 §2](../contracts/deploy-hardening.md#2-k8s-扩缩容最小-rbac决议-e2e3e5) 的允许项和禁止项。

```bash
scaler=(
  --as=system:serviceaccount:uh-prod:uh-scaler
  --as-group=system:serviceaccounts
  --as-group=system:serviceaccounts:uh-prod
  --as-group=system:authenticated
)
kubectl auth can-i --list -n uh-prod "${scaler[@]}"
for verb in get patch update; do
  kubectl auth can-i "$verb" deployments.apps/uh-crewai --subresource=scale -n uh-prod "${scaler[@]}"  # yes
done
kubectl auth can-i get deployments.apps/uh-crewai -n uh-prod "${scaler[@]}"  # yes
kubectl auth can-i patch deployments.apps/uh-crewai -n uh-prod "${scaler[@]}"  # no
kubectl auth can-i patch deployments.apps/uh-kernel --subresource=scale -n uh-prod "${scaler[@]}"  # no
kubectl auth can-i patch deployments.apps/uh-crewai --subresource=scale -n default "${scaler[@]}"  # no
kubectl auth can-i get secrets -n uh-prod "${scaler[@]}"  # no
kubectl auth can-i create pods --subresource=exec -n uh-prod "${scaler[@]}"  # no
kubectl auth can-i bind clusterroles.rbac.authorization.k8s.io/cluster-admin -n uh-prod "${scaler[@]}"  # no
kubectl auth can-i escalate roles.rbac.authorization.k8s.io -n uh-prod "${scaler[@]}"  # no
kubectl get clusterrolebindings,rolebindings -A -o json |
  jq '.items[] | select(any(.subjects[]?;
    (.kind == "ServiceAccount" and .name == "uh-scaler" and .namespace == "uh-prod") or
    (.kind == "User" and .name == "system:serviceaccount:uh-prod:uh-scaler"))) |
    {kind, name: .metadata.name, namespace: .metadata.namespace, roleRef}'
```

直接绑定查询应只返回 `uh-prod/uh-crewai-scaler` 的 RoleBinding。权限列表中与工作负载相关的条目应只有 CrewAI 的两个授权规则。若集群向 ServiceAccount 组授予其他权限，应一并审计这些组绑定。 [kubectl auth can-i](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_auth/kubectl_auth_can-i/)


## apiserver-egress（二轮复检阻断①修复）

`uh-scaler` 经内核 Pod 调 `deployments/scale`，需要 Egress 到 kube-apiserver。
`networkpolicies.yaml` 的 `kernel-egress-apiserver` 已预留规则，但 **cidr 是文档占位地址 `192.0.2.10/32`，部署前必须替换**：

```bash
# 取实际控制面地址
kubectl get endpoints kubernetes -n default -o jsonpath='{.subsets[0].addresses[0].ip}'
# 替换后 apply
sed -i 's|192.0.2.10/32|<REAL_APISERVER_IP>/32|' deploy/k8s/networkpolicies.yaml
kubectl apply -f deploy/k8s/networkpolicies.yaml
```

同时需在集群侧启用 **ServiceAccount token 自动挂载**（默认开启）并提供 CA：
内核容器已读 `/var/run/secrets/kubernetes.io/serviceaccount/`，`UH_K8S_API_HOST` 默认取 `KUBERNETES_SERVICE_HOST`。

未替换 cidr 时 scaler 调用会超时 → 协作降级为普通对话（`collaboration.worker.degraded`），**不会崩主链路**。

## 可观测性 K8s 落地（阻断②修复）

`deploy/k8s/observability-k8s.yaml` 提供：
- `uh-kernel-metrics` Service（Prometheus 抓取入口，9090→8080）
- `otel-collector` Deployment + Service（4317 gRPC / 4318 HTTP）
- `otel-collector-config` ConfigMap（OTLP → logging exporter，生产替换为 Jaeger/Tempo）
- NetworkPolicy 放行 kernel→collector 4317/4318

内核 Pod 已注入 `UH_OTEL_ENABLED=1` + `OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317` + `UH_OTEL_SAMPLE_RATIO=0.1`。

## cgroup 委派（阻断③修复）

内核 Pod 挂载 `cgroup: { path: uh-plugins, resources: { limits: { pids: 64 } } }` 到 `/sys/fs/cgroup/uh-plugins`，
并通过 `UH_SANDBOX_CGROUP_ROOT` 告知 PluginCgroup。**需节点启用 cgroup v2 + 内核 5.10+**；
未委派时 PluginCgroup 记 `plugin.sandbox.cgroup_unconfigured` 告警并回退 `ulimit -v`（仅内存，无 pids 防护）。


## 能力远程调用（M3 复审 P1）

原实现里检索走 `InProcessTransport`、编排直接实例化进程内服务——**uh-llama-index /
uh-langgraph 的副本数对业务路径毫无意义**，OOM 演练与 HPA 扩缩证明不了隔离。

现已接通远程 MCP：

| 能力 | 内核配置 | 服务端启动 |
|---|---|---|
| 检索 | `UH_RETRIEVAL_MCP_URL=http://uh-llama-index:8080/mcp` | `command: ["python","-m","kernel.retrieval"]` + `UH_MCP_TRANSPORT=http` |
| 编排 | `UH_ORCHESTRATION_MCP_URL=http://uh-langgraph:8080/mcp` | `command: ["python","-m","kernel.orchestration"]` + `UH_MCP_TRANSPORT=http` |

要点：
- **编排事件回放**：`emit` 回调不可跨进程，远程编排把事件随结果返回（`events` 数组），
  内核侧逐条回放到任务事件流，外部契约不变。
- **探针**：两个能力 HTTP app 都提供 `/health` 与 `/ready`（此前检索 app 缺失，
  K8s 探针会让 Pod 永不就绪）。
- **本地实例仍保留**：`app.state.orchestration` 供 TaskManager 的 resume/checkpoint 查询；
  远程模式下 checkpoint 不在本进程，**resume 需 sticky 会话**或后续把 checkpoint 外置。

### 仍未完成（需真集群）
- HPA 随负载扩缩的实测证据
- 单能力 Pod OOM/崩溃时业务不中断的演练证据
- 远程 MCP 连不通时的降级路径实测（代码已返回 degraded，未在集群验证）

> 在此之前，不应把「能力独立扩缩已验证」写进对外声明。
