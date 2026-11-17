# P0-5 契约：部署暴露面与 RBAC 收紧

> 版本：v0.1.0（契约已冻结）· 状态：frozen（MS-0 评审通过 2026-11-15，tag contract-v0.1.0；只增不改）
> 基线：v3 §7.3.1 / §7.4.1 / §9.2 / §9.3 · Blocks：M3-1（#28）/ M3-2（#29）
> 原则：内部服务零宿主暴露；扩缩容权限最小化且可审计；网络默认拒绝、按需放行。

## 1. Compose 暴露面契约（决议 E1）

| 服务 | v3 现状 | 契约 | 理由 |
|---|---|---|---|
| uh-kernel | ports 8080:8080 | **保留**（唯一对外端口） | 用户 API 入口 |
| uh-llama-index | ports 8081:8080 | expose ["8080"] | 内部 RPC，仅内核可达 |
| uh-langgraph | ports 8082:8080 | expose ["8080"] | 同上 |
| uh-crewai | ports 8083:8080 | expose ["8080"]，profiles: collaborate | 同上 + 按需启动 |
| redis | ports 6379:6379 | **移除 ports**（内网服务名访问） | 数据存储不出容器网络 |
| chroma | ports 8000:8000 | **移除 ports** | 同上 |
| postgres | 无 ports | 保持 | 已合规 |

修正后的 compose 骨架（M3-1 实装基线；version 字段已废弃，移除，决议 E6）：

```yaml
services:
  uh-kernel:
    image: ultra-harness/kernel:0.1.0
    ports: ["8080:8080"]            # 唯一对外端口
    depends_on: [redis, chroma, postgres]
    environment:
      - LLM_PROVIDER=${LLM_PROVIDER}
      - LLM_API_KEY=${LLM_API_KEY}  # 由 .env / secret 注入，仓库不落明文（§9.2）
  uh-llama-index:
    image: ultra-harness/llama-index:0.1.0
    expose: ["8080"]                # 仅容器网络可达
    depends_on: [chroma]
  uh-langgraph:
    image: ultra-harness/langgraph:0.1.0
    expose: ["8080"]
    depends_on: [redis, postgres]
  uh-crewai:
    image: ultra-harness/crewai:0.1.0
    expose: ["8080"]
    profiles: ["collaborate"]       # 默认不启动
  redis:
    image: redis:7-alpine
  chroma:
    image: chromadb/chroma:latest
    volumes: ["chroma-data:/chroma/db"]
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: uh_checkpoints
      POSTGRES_USER: uh
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    volumes: ["pg-data:/var/lib/postgresql/data"]
volumes:
  chroma-data:
  pg-data:
```

验证（P0-5 验收 1）：docker compose up -d 后，宿主机监听仅 8080（ss -tlnp / docker port 逐服务核对）；外部 nmap 宿主仅 8080 开放。

## 2. K8s 扩缩容最小 RBAC（决议 E2/E3/E5）

v3 §7.4.1 只写"通过 K8s API 扩为 1"，未约束权限。契约如下：

- 独立 ServiceAccount **uh-scaler**（namespace uh-prod），内核 Pod 经 serviceAccountName 使用；内核默认 SA 不携带任何扩缩容权限。
- 权限走 **deployments/scale 子资源**（比 patch 整个 Deployment 更精确），resourceNames 锁定 uh-crewai，动词仅 get/patch/update；另授 deployments get（仅 uh-crewai）用于就绪确认。
- 数值约束（replicas ∈ {0,1}、目标仅 uh-crewai、超时放弃）由 scaler 代码强制 + 审计事件；RBAC 无法表达数值上限，ValidatingAdmissionPolicy 为 M3+ 加固项（决议 E5）。
- v0.1.0 内核 Pod 复用 uh-scaler SA；独立 scaler 控制器（内核经 RPC 调用）为 M3+ 选项（决议 E3）。

```yaml
apiVersion: v1
kind: ServiceAccount
metadata: { name: uh-scaler, namespace: uh-prod }
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata: { name: uh-crewai-scaler, namespace: uh-prod }
rules:
  - apiGroups: ["apps"]
    resources: ["deployments/scale"]
    resourceNames: ["uh-crewai"]
    verbs: ["get", "patch", "update"]
  - apiGroups: ["apps"]
    resources: ["deployments"]
    resourceNames: ["uh-crewai"]
    verbs: ["get"]                  # 读 status 确认就绪
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata: { name: uh-crewai-scaler, namespace: uh-prod }
subjects:
  - { kind: ServiceAccount, name: uh-scaler, namespace: uh-prod }
roleRef: { kind: Role, name: uh-crewai-scaler, apiGroup: rbac.authorization.k8s.io }
```

禁止清单（审计红线）：cluster-admin / 任何 ClusterRole 绑定 uh-scaler / secrets 读写 / pods/exec / RBAC escalate·bind / 通配符 * 资源或动词 / 跨 namespace 权限。

审计命令（P0-5 验收 2）：

```bash
kubectl auth can-i --list -n uh-prod \
  --as=system:serviceaccount:uh-prod:uh-scaler
# 期望恰好：deployments/scale get/patch/update (uh-crewai) + deployments get (uh-crewai)
kubectl auth can-i get secrets -n uh-prod --as=system:serviceaccount:uh-prod:uh-scaler   # no
kubectl auth can-i patch deployments uh-kernel -n uh-prod --as=system:serviceaccount:uh-prod:uh-scaler  # no
kubectl get clusterrolebindings -o json | jq '.items[] | select(.subjects[]?.name=="uh-scaler")'  # 空
```

## 3. NetworkPolicy：默认拒绝、按需放行（决议 E4）

uh-prod 命名空间默认拒绝 Ingress + Egress，放行矩阵：

| 源 | 目标 | 端口 | 说明 |
|---|---|---|---|
| ingress-controller（独立 ns） | uh-kernel | 8080 | 唯一入口 |
| uh-kernel | uh-llama-index / uh-langgraph / uh-crewai | 8080 | 内部 RPC |
| uh-kernel | redis / chroma / postgres | 6379 / 8000 / 5432 | 内核直连存储 |
| uh-langgraph | redis / postgres | 6379 / 5432 | checkpoint |
| uh-llama-index | chroma | 8000 | 向量库 |
| uh-kernel / uh-crewai | 外部 LLM API | 443 | 出网（建议经出网代理） |
| 全部 Pod | kube-dns | 53/UDP+TCP | DNS 解析 |
| 其余任意 | 任意 | — | **拒绝** |

骨架（M3-2 补全标签选择器细节）：

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: { name: default-deny, namespace: uh-prod }
spec:
  podSelector: {}
  policyTypes: [Ingress, Egress]
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: { name: allow-dns, namespace: uh-prod }
spec:
  podSelector: {}
  policyTypes: [Egress]
  egress:
    - to: [{ namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: kube-system } },
             podSelector: { matchLabels: { k8s-app: kube-dns } } }]
      ports: [{ port: 53, protocol: UDP }, { port: 53, protocol: TCP }]
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: { name: capability-ingress-from-kernel, namespace: uh-prod }
spec:
  podSelector: { matchLabels: { tier: capability } }
  policyTypes: [Ingress]
  ingress:
    - from: [{ podSelector: { matchLabels: { app: uh-kernel } } }]
      ports: [{ port: 8080 }]
# kernel-ingress / kernel-egress / capability-egress-datastores / llm-egress 同构补全（M3-2）
```

验证（P0-5 验收 3）：无标签测试 Pod wget 能力模块 → 超时；kernel Pod 内 wget → 通；kubectl get netpol -n uh-prod 含 default-deny。
注意：NetworkPolicy 不替代内部 RPC 认证（§9.2 mTLS/共享密钥），纵深防御两层都要。

## 4. 写入 §9.2 / §9.3 的权限约束（任务第 3 项）

§9.2 追加条款：

> K8s 扩缩容权限：仅 uh-scaler ServiceAccount 持有 uh-prod 内 uh-crewai 的 deployments/scale get/patch/update（+ deployments get），namespace 级 Role，无 ClusterRole 绑定；权限清单经 kubectl auth can-i --list 审计，禁止 cluster-admin。scaler 代码强制 replicas ∈ {0,1}。

§9.3 追加交叉引用：

> 插件沙箱（进程级）与 K8s RBAC（集群级）同为最小权限原则的两层；部署暴露面与扩缩容权限契约见 docs/contracts/deploy-hardening.md。

## 5. 决议记录

| # | 决议 | 理由 |
|---|---|---|
| E1 | Compose 仅 8080 对外；能力模块 expose；redis/chroma 移除宿主端口 | 内部服务零暴露；v3 映射 5 个多余端口 |
| E2 | 扩缩容走 deployments/scale 子资源 + resourceNames 锁定 | 比任务卡"get/patch replicas"更精确，杜绝整 Deployment 改写 |
| E3 | v0.1.0 内核 Pod 复用 uh-scaler SA；独立 scaler 控制器 M3+ | 最小可行；爆炸半径=仅能扩缩 crewai（资源消耗，无数据访问） |
| E4 | NetworkPolicy 默认拒绝 Ingress+Egress，按矩阵放行；不替代 mTLS | 任务要求；纵深防御 |
| E5 | replicas 数值上限由代码+审计强制；VAP 为 M3+ | RBAC 无法表达数值约束，诚实声明 |
| E6 | 移除 compose version 字段 | Compose v2 已废弃 |
