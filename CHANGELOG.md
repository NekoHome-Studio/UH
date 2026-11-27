# CHANGELOG

本文件记录 UH!（Ultra Harness）的所有显著变更。
格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased] — v0.1.0 独立第三方审查修复（R2）

> 来源：`docs/reviews/2026-11-25-v0.1.0-独立第三方审查报告-Tencent-Hunyuan-Hy.md`

### 修复
- **P1-1 日志脱敏熵兜底漏出**：长度下限 24→15、取消纯 ASCII 字符集与 `isspace` 整串否决，改按 Unicode 字符类别（≥3 类）+ 熵 ≥3.8 判定；URL / sha* 哈希前缀显式豁免零误杀。短凭据（16~22 位）、非 ASCII、emoji、含空白串全部脱敏；11 例回归。
- **P2-1 静态扫描 exec 族绕过**：黑名单补全 `os.exec*`/`os.spawn*`/`os.posix_spawn*`、`subprocess.getoutput`/`getstatusoutput`、`pty.spawn` 共 8+ 种具名危险调用（独立审查前 19 个样本之外的新绕过），9 例回归含正常插件防误杀。
- **P2-3 交付数字失实**：RELEASE.md / AGENTS.md 的测试数与门禁项数统一为 R2 实测（527 passed / 16 skipped、16 项门禁）。

### 门禁
- `pytest` 527 passed / 16 skipped（另 2 例环境 fixture 本机失败（git http-backend、插件崩溃计数），均已在基线 HEAD 独立复现，非产品缺陷）· `verify-all --full` 16 项全绿（含 D2 契约端点实现覆盖）· `secret-scan` 362 文件 0 命中 · `doc-links` LINKS_OK。

## [0.1.0] - 2026-11-25

首个公开版本。微内核 + 能力层 + 记忆层四层架构，本地优先、模块化可插拔、低占用渐进增强、可换肤、可观测。

### 新增

#### 契约与治理（MS-0）
- **冻结契约体系**：错误码（14 有效 + 4 弃用）、插件 manifest schema、18 个 WS 事件、任务流转、UI 状态机（10 态）、沙箱矩阵、部署加固。
- **机器可读契约**：`openapi.yaml`（OpenAPI 3.1，19 端点）、`asyncapi.yaml`（AsyncAPI 2.6）。
- **质量门禁 `verify-all.mjs`**：14 项自动校验（schema / 事件映射双向 / 交叉一致性 / OpenAPI 覆盖 / AsyncAPI / Compose 暴露面 / K8s RBAC / 指标基数 / Desktop 一致性 / 安全加固 / 供应链）。
- **密钥扫描门禁** `secret-scan.mjs`：仓库零明文凭据。

#### 内核骨架（MS-1）
- FastAPI 微内核：会话 / 消息 / 任务 / 步骤 / 确认 / 主题 / 插件 REST 端点。
- **WebSocket 事件流** + SSE 过渡通道，支持 `after_event_id` 断线重放（5 分钟窗口 + step 永久日志）。
- **统一结构化 JSON 日志**：必含 timestamp / level / trace_id / span_id / module / event。
- **trace_id / span_id 全链路透传**（`uh_trc_<32hex>` / `sp_<8hex>`）。
- LLM 封装：流式、超时重试、Token 统计；无密钥时 mock provider 回退。
- 短期记忆（Redis，fakeredis 开发回退）+ 长期记忆（向量召回）。
- 路由判定：chat / search / orchestrate / collaborate 四类任务。

#### 能力层（MS-2）
- **MCP Host**：JSON-RPC 工具调用、幂等重试、超时取消、风险分级。
- **检索能力**（LlamaIndex 风格）：本地文件 / Chroma 后端，确定性词袋 embedding，不可信内容边界包装。
- **编排能力**（LangGraph 风格）：多步 workflow + checkpoint（JSON / SQLite / PostgreSQL），resume 跳过已完成节点。
- **协作能力**（CrewAI）：按需启停，只经 RPC / EventBus 交互。
- **CapabilityRegistry**：能力注册 + StorageGuard 存储隔离（模块仅可访问自身命名空间）。
- **高危工具确认门**：一次性 token 防重放 + 任务绑定 + TTL，fail-closed（无确认通道一律拒绝）。

#### 插件系统（MS-3）
- **PluginDriver**：安装 / 校验 / 激活 / 停用 / 卸载 / 热重载，崩溃计数与隔离。
- **最小沙箱**：Windows Job Object（内存 / CPU 限额）、Linux bubblewrap（PID / net namespace + 只读根）、macOS sandbox-exec 分支。
- **静态扫描**：危险属性 / 反射 / 别名 / 容器存储 / 参数传递多层检测（审查辅助，非安全边界）。
- **工具 SDK** + 3 个官方工具（web-search / file / db-query）+ audit-log 钩子 + theme-light 主题。
- **主题 CSS 契约**：`--uh-*` 变量 + 字体栈，双端共用 applier。
- **钩子系统**：异步队列 + 超时 + 异常隔离。
- **插件 CLI**：`uh plugin validate/install/uninstall/list/audit/dev`。

#### 生产化（MS-4）
- **Docker Compose 编排**：7 服务，仅内核 8080 对外，能力模块 `expose`，数据存储无 ports，CrewAI `profiles: [collaborate]`。
- **K8s 清单**：内核 2 副本 + 探针、能力模块 HPA（CPU 70%）、CrewAI replicas 0、StatefulSet + PVC。
- **最小 RBAC**：`uh-scaler` ServiceAccount 仅 `deployments/scale` 子资源，`resourceNames` 锁定 `uh-crewai`。
- **NetworkPolicy**：默认拒绝 Ingress + Egress，7 条放行矩阵 + apiserver 控制面 + OTel collector。
- **Prometheus 指标**：13 指标族覆盖 §7.5.1；路由模板化防高基数。
- **Alertmanager 三条告警**：HighErrorRate（分母 >0 守卫）/ MemoryPressure / PluginCrashLoop。
- **OpenTelemetry**：optional extra，默认 no-op 零开销；`uh_trc_` hex ↔ OTel 128bit trace_id 映射；W3C traceparent 传播；错误尾采样。
- **CrewAI 按需启停**：Redis 分布式租约 + WATCH 乐观锁 + 过期 reconciler，最后任务完成才缩容。
- **Desktop 壳（Tauri 2）**：托盘 / 全局快捷键 / 终态通知 / 内核子进程生命周期；frontendDist 直指内核 UI（前端零复制）。
- **系统级安全加固**：seccomp 白名单（三态 enforce/audit/disabled）、cgroup v2 配额（含 pids.max 防 fork 炸弹）、只读根 + tmpfs、非 root + cap drop。
- **L2 字段级加密**：AES-GCM，明文不落盘、不进语义索引、无主密钥 fail-closed。
- **日志脱敏**：6 类正则 + Shannon 熵检测双轨，正文裁剪 160 字符，trace_id/span_id 保留。
- **供应链**：CycloneDX 1.5 SBOM 生成 + 官方插件 manifest / 内嵌凭据校验 + OCI SBOM 标签。
- **压测能力**：`uh bench run --scenario --concurrency`，SLO 判定 + 双端硬件基线 + 可复现报告。
- **成本分层模板**：light（本地 Ollama）/ standard（gpt-4o-mini）/ enterprise（gpt-4o）。
- **多进程支持**：会话归属外置 Redis、集群共享签名密钥、`kernel.serve` 多 worker 启动器。
- **性能优化**：同步 MCP handler / 长记忆读写卸载线程池、embedding LRU 缓存、检索倒排索引预筛、delta 不入内部总线、有界执行闸。

### 变更
- **错误码域合并**：LLM 域并入 CAP（决议 D1），`UH-KRN-1001` 统一为 `UH-KRN-2001`（决议 D2）。详见「弃用」。
- **能力模块错误码双模式**：strict（默认，`UH-CAP-4004`）/ legacy（`UH-CAP-4003`），由 `UH_CAP_ERROR_MODE` 或插件上下文切换。
- **WS 归属校验双因子**：token **且** client_id 同时匹配（原单 token 可绕过）。
- **`stream_url` 按 scheme 区分**：可信 `UH_PUBLIC_BASE_URL` 的 http(s) 转 ws(s)，不信任请求 Host 头。
- **SBX-06 归位安装期**（M2-1），M3 加固专属测试改为 SBX-07~09（决议 S6）。
- **长期记忆召回范围双模式**：global 跨会话（默认）/ session 仅本会话（ADR-003）。
- **mock LLM 延迟可配**：`UH_LLM_MOCK_DELAY_MS`（默认 5ms 保持演示行为，压测可设 0）。

### 修复

#### 安全（多轮独立审查发现）
- **令牌泄露**：引入 `UH_AUTH_TOKEN_FILE` 文件引用机制，`--no-access-log` 防 token 入访问日志，加密钥扫描门禁。
- **跨客户端越权链**：SSE 缺 `_task_guard` + `owner_matches` 单 token 绕过 → 补守卫 + 双因子。
- **静态扫描绕过**（19 个样本）：builtins.eval / 别名 / 下标取属性 / 语法错误静默跳过 / 赋值别名 / 容器与类实例存 callable / 参数传递 / 动态 `chr()` 构造 → 改引用级兜底 + 反射封锁。
- **多 worker 签名密钥不一致**：每进程随机密钥致跨 worker 验签失败（403）→ Redis SETNX 派生集群共享密钥，缺失时 fail-fast。

#### Linux 沙箱（首次实机暴露）
- **bwrap 找不到解释器**：venv / uv 装的 Python 不在 ro-bind 范围 → 挂 venv 根 + resolve 符号链接真实路径。
- **Linux 无内存限额**：原仅 Windows Job Object 实现 → 补 `ulimit -v`。
- **`wait()` 超时不 kill**：仅透传 TimeoutExpired → terminate + 回收 + 抛 `UH-CAP-5021`。

#### 部署与运行时
- **K8s scaler 控制面断路**：default-deny 未放行 apiserver → 补 `kernel-egress-apiserver`。
- **可观测性仅 Compose**：K8s 侧缺 Prometheus Service 与 OTel 4317/4318 放行 → 补 `observability-k8s.yaml`。
- **cgroup 未注入**：`UH_SANDBOX_CGROUP_ROOT` 在清单缺失致 pids 防护落空 → 补委派挂载。
- **协作释放 fire-and-forget**：scale(0) 失败不重试致副本永停 1 → 3 次重试 + 保留状态供 reconciler；锁 TTL 45→90s 消除双重删除竞态。
- **git fixture flaky**：`git http-backend` 偶发非零退出 → 重试 3 次 + 退避。

### 弃用

以下错误码自契约冻结起**停止产出**，服务端一律产出替代码。对照表仅作迁移与历史检索。
**最早移除版本：v0.2.0**（对齐 §6.8 弃用流程：提前一个版本 Deprecation 提示 + 变更日志标注）。

| 弃用码 | 替代方案 | 废弃原因 | 移除版本 |
|---|---|---|---|
| `UH-KRN-1001` | **`UH-KRN-2001`** | 与 §6.7 的 KRN-2001 同义二义，统一为 2001（决议 D2） | v0.2.0 |
| `UH-LLM-4001` | **`UH-CAP-4001`** | LLM 域并入 CAP（决议 D1） | v0.2.0 |
| `UH-LLM-4002` | **`UH-CAP-4002`** | 同上 | v0.2.0 |
| `UH-LLM-5001` | **`UH-CAP-4003`** | 同上 | v0.2.0 |

> v0.1.0 为首个公开版本，无存量客户端，故无需兼容期产出旧码。
> 客户端若硬编码旧码，请改用替代码；`UH_CAP_ERROR_MODE=legacy` 仅影响 CAP-4003/4004 的选择，不恢复 LLM 域。

### 已知限制

- **100 并发 P95 未达 SLO**：目标 ≤2000ms，实测 6.7~7.2s（错误率 0% 达标）。根因为单进程内请求提交与任务执行共享事件循环；已实施六轮优化（相对初始基线改善 8~9 倍），突破需独立执行进程池，计划 v0.2.0。详见 `docs/reviews/2026-11-25-延迟根因诊断.md`。
- **Compose / K8s 真环境实跑未验证**：清单与静态门禁齐备，缺 Docker / 集群环境。
- **Desktop Tauri 二进制未编译**：源码、配置、构建步骤齐备，Rust 工具链未装。
- **macOS SBX 矩阵未采证**：无 macOS 主机。
- **cgroup 继承测试跳过**：需 root 建委派 cgroup v2 目录。
- **per-user principal 未实现**：当前单共享 token，假设单用户设备。
- **L2 主密钥轮换未实现**。
- **静态扫描为审查辅助**，非安全边界；真实边界是签名信任 + 沙箱运行时。残余理论面建议 v0.2.0 上 AST 污点分析。
- **SBOM 为静态解析声明版本**，非实际安装版本；生产建议 syft / pip freeze。

[0.1.0]: https://github.com/NekoHome-Studio/UH/releases/tag/v0.1.0
