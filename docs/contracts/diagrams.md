# P0-6 契约图集（4 张 Mermaid，GitHub 原生渲染）

> 对应 v3 §3.1 分层模型 / §3.4 请求生命周期 / §5.3 插件生命周期 / §7.x 部署拓扑。
> 事实源：本图集与 openapi.yaml / asyncapi.yaml / events.md 同批评审；修改走契约 PR 流程。

## 1. 分层模型（§3.1 / §3.2）

```mermaid
flowchart TD
  subgraph UI["UI 层"]
    WEB["Web 前端"]
    DESK["Desktop 壳 Tauri"]
    CLI["CLI uh"]
    THEME["主题插件 CSS 变量"]
  end
  subgraph KERNEL["内核层 微内核"]
    ROUTER["路由调度 Router"]
    PLUGIN["插件驱动 六阶段生命周期"]
    BUS["事件总线 + 内部 RPC"]
    SANDBOX["沙箱与权限"]
  end
  subgraph CAP["能力层 独立服务 懒加载"]
    LG["LangGraph 编排"]
    LI["LlamaIndex 检索"]
    CA["CrewAI 协作 按需 0到1"]
    MCP["MCP 工具接口"]
  end
  subgraph MEM["记忆层"]
    REDIS["短期记忆 Redis"]
    CHROMA["长期向量 Chroma"]
    PG["Checkpoint PostgreSQL"]
  end
  LLM["上游 LLM API"]
  UI -->|"REST 8080 + WS 事件流"| KERNEL
  ROUTER --> BUS
  BUS --> CAP
  PLUGIN --> SANDBOX
  SANDBOX --> MCP
  LI --> CHROMA
  LG --> PG
  LG --> REDIS
  CAP --> LLM
  KERNEL --> REDIS
```

## 2. 请求生命周期（§3.4，含 trace_id 与事件流）

```mermaid
sequenceDiagram
  participant U as 用户
  participant K as 内核
  participant R as 路由
  participant C as 能力模块
  participant M as 记忆层
  participant L as LLM
  U->>K: POST /sessions/{id}/messages
  K->>K: 生成 trace_id
  K->>R: 意图识别
  R-->>K: kernel.route.decided
  K-->>U: 202 task_id + stream_url + trace_id
  K-->>U: WS task.created
  K->>C: 按需分派 内部 RPC 信封
  C->>M: 检索短期/长期记忆
  C->>L: 增强生成 流式
  L-->>C: delta
  C-->>K: 结果 + 状态
  K->>M: 写入短期缓存 关键结果入向量库
  K-->>U: WS delta / step.* / 终态事件
```

## 3. 插件生命周期（§5.3，任一阶段失败幂等回滚）

```mermaid
stateDiagram-v2
  [*] --> install: uh plugin install
  install --> load: SHA-256 + 签名校验通过
  install --> [*]: 校验失败 拒绝 SBX-06
  load --> activate: manifest 校验 + 依赖就绪
  activate --> execute: 注册能力总线 / 订阅事件
  execute --> execute: 按请求调用 timeout_ms + rate_limit
  execute --> deactivate: SIGTERM
  deactivate --> uninstall: 5s 内 flush 状态
  deactivate --> uninstall: 超时强杀
  uninstall --> [*]: 清理沙箱目录 + 注销注册
  load --> install: 失败回滚
  activate --> load: 失败回滚
  note right of execute: 连续 3 次崩溃进入 quarantined
```

## 4. 部署拓扑（§7.x，暴露面契约见 deploy-hardening.md）

```mermaid
flowchart LR
  subgraph HOST["Docker Compose 单宿主机"]
    K1["uh-kernel 8080 唯一对外"]
    LI1["uh-llama-index expose 8080"]
    LG1["uh-langgraph expose 8080"]
    CA1["uh-crewai expose 8080 profiles collaborate"]
    R1["redis 无宿主端口"]
    C1["chroma 无宿主端口"]
    P1["postgres 无宿主端口"]
    K1 --> LI1
    K1 --> LG1
    K1 --> CA1
    K1 --> R1
    K1 --> C1
    K1 --> P1
  end
  subgraph K8S["Kubernetes uh-prod"]
    K2["uh-kernel Deployment 副本≥2"]
    HPA["HPA CPU 70%"]
    CA2["uh-crewai replicas 0到1到0"]
    SCALER["uh-scaler SA 最小 RBAC"]
    STS["StatefulSet + PVC redis/chroma/postgres"]
    NP["NetworkPolicy 默认拒绝"]
    HPA --> K2
    SCALER --> CA2
    K2 --> STS
  end
  USER["用户"] -->|"8080"| K1
  USER -->|"Ingress 8080"| K2
```
