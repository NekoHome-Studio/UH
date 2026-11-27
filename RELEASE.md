# UH! v0.1.0 发布说明

> **发布日期**：2026-11-25 · **首个公开版本**
> 仓库：https://github.com/NekoHome-Studio/UH

## 这是什么

**UH!（Ultra Harness）** 是个人 AI Agent 运行底座——「集百家之长，做用户之尚」。

微内核 + 能力层 + 记忆层四层架构，五个设计取向：
- **本地优先**：无云端依赖也能跑（本地 Ollama + 文件检索）
- **模块化可插拔**：能力经 MCP 接入，插件热装热卸
- **低占用渐进增强**：协作模块平时 replicas 0，按需 0→1→0
- **可换肤**：CSS 变量契约，Web / Desktop 双端同步
- **可观测**：指标 / 告警 / 链路追踪 / 结构化日志全链路

## 亮点

### 契约先行，门禁护住
错误码、事件、manifest、任务流转、UI 状态机全部冻结成机器可读契约（OpenAPI 3.1 + AsyncAPI 2.6）。
**16 项自动门禁** `verify-all.mjs` 覆盖 schema、事件映射双向校验、契约端点实现覆盖（D2，非自证）、K8s RBAC 越权检测、指标高基数卡口、供应链 SBOM。

### 安全不是口号
- **沙箱真隔离**：Linux bubblewrap（PID / net namespace + seccomp + 只读根 + cgroup pids 限额），Windows Job Object
- **L2 敏感数据 AES-GCM 加密**，明文不落盘、不进语义索引、缺密钥 fail-closed
- **日志双轨脱敏**：6 类正则 + Shannon 熵检测，但 trace_id 永不脱敏（保排查能力）
- **最小 RBAC**：扩缩容 ServiceAccount 只能改一个 Deployment 的 scale 子资源
- **高危工具确认门**：一次性 token + 任务绑定，无确认通道一律拒绝

历经 M1 五轮 + M2 八轮 + M3 中期深审 + v0.1.0 独立第三方审查的**多模型独立交叉审查**，累计 27 个静态扫描绕过样本全部堵死（原 19 个 + 独立审查新增 8 个 exec 族）。

### 三种部署形态
| 形态 | 适用 | 入口 |
|---|---|---|
| 单机 dev | 个人试用 | `uh dev up --ui web` |
| Docker Compose | 小团队 | `docker compose up -d` |
| Kubernetes | 生产 | `kubectl apply -f deploy/k8s/` |

外加 **Tauri 2 桌面壳**（托盘 / 全局快捷键 / 通知）与**成本三档模板**（light / standard / enterprise）。

### 性能可测量
`uh bench run --scenario chat --concurrency 100` 输出 P50/P95/P99 + SLO 判定 + **双端硬件基线**（客户端与服务端分列；服务端硬件经 /api/v1/bench/baseline 采集，原始 JSON 随包见 benchmarks/kylin-4worker/）。

## 质量数据

| 项 | 结果 |
|---|---|
| 单元 / 集成测试 | **527 passed, 16 skipped**（R2 修复后实测；另有 2 例环境 fixture 在本机稳定失败（git http-backend、插件崩溃计数），均已在基线 HEAD 独立复现，非产品缺陷） |
| 质量门禁 | **16 项全绿** |
| 密钥扫描 | **0 命中** |
| 全量覆盖率 | **80%** |
| M3 新增模块覆盖率 | **95%** |
| E2E-01~08 | 7 passed, 1 skipped |
| SBX-01~09 | Windows 全绿 + **Linux 实机 6 passed** |
| SEC-01~05 | **14 例全绿** |

16 个 skip 均为环境依赖（OTel SDK 未装 11 / 需 Linux 实机 4 / 主题未注册 1），非掩盖失败。

## 已知限制（请务必阅读）

### ⚠️ 100 并发 P95 未达 SLO
| 指标 | 目标 | 实测 | 判定 |
|---|---|---|---|
| 错误率 | <1% | **0%** | ✅ 达标 |
| P95 延迟 | ≤2000ms | **6.85~7.42s** | ❌ 未达标 |

**低并发（≤10）表现正常**（5 并发 P95 ≈186ms，0% 错误）。

根因：单进程内请求提交与任务执行共享事件循环，无法真正隔离。已实施六轮优化
（线程池卸载 / embedding 缓存 / 倒排索引 / delta 不入总线 / 长记忆 offload / 有界执行闸），
**相对初始基线改善 8~9 倍**（search 55.8s→7.2s）。

突破需**独立执行进程池**（任务记录 + 事件 pub/sub 重构），计划 **v0.2.0**。
详见 [延迟根因诊断](docs/reviews/2026-11-25-延迟根因诊断.md)。

> 压测数字基于 **mock LLM provider**（CPU-bound 逐字符输出），
> 真实 LLM 的 IO 等待会让出事件循环，实际表现可能不同。**绝对数值不可外推到生产 LLM**。

### 其他限制
- **Compose / K8s 真环境实跑未验证**（清单与静态门禁齐备，缺 Docker / 集群）
- **Desktop 二进制未编译**（源码配置齐备，见 `desktop/README.md` 构建步骤）
- **macOS 沙箱矩阵未采证**（无主机）
- **K8s 多副本需客户端亲和**：任务状态与幂等缓存仍进程内，Service 已配 `sessionAffinity: ClientIP` 止血；经 ingress 时源 IP 改写会使亲和失效，需改用 cookie 亲和。根治待 v0.2.0 状态外置
- **单用户假设**：当前单共享 token，per-user principal 待 v0.2.0
- **L2 主密钥轮换未实现**
- **静态扫描是审查辅助不是安全边界**；真实边界为签名信任 + 沙箱运行时

## 快速开始

```bash
git clone https://github.com/NekoHome-Studio/UH.git && cd UH
python -m venv .venv && .venv/bin/pip install -e .
uh dev up --ui web        # http://127.0.0.1:8080
```

无 LLM 密钥时自动回退 mock provider，无 Redis 时回退 fakeredis——**开箱即跑**。

接真实模型：
```bash
export UH_LLM_API_KEY=sk-...
export UH_LLM_MODEL=gpt-4o-mini
# 或本地 Ollama（离线）
export UH_LLM_BASE_URL=http://localhost:11434/v1
export UH_LLM_MODEL=qwen2.5:3b
export UH_LLM_API_KEY=ollama
```

## 文档导航

| 想做什么 | 看哪里 |
|---|---|
| 了解架构与规则 | [AGENTS.md](AGENTS.md) |
| 查契约（错误码/事件/API） | [docs/contracts/](docs/contracts/) |
| Docker 部署 | [docker-compose.md](docs/deployment/docker-compose.md) |
| K8s 部署 | [kubernetes.md](docs/deployment/kubernetes.md) |
| 多进程调优 | [multiprocess.md](docs/deployment/multiprocess.md) |
| 安全加固 | [security-hardening.md](docs/deployment/security-hardening.md) · [security-m3-8.md](docs/deployment/security-m3-8.md) |
| 链路追踪 | [tracing.md](docs/deployment/tracing.md) |
| 协作模块 | [collaboration.md](docs/deployment/collaboration.md) |
| 无 root Linux 部署 | [kylin-userland.md](docs/deployment/kylin-userland.md) |
| 插件开发 | [docs/sdk/python-tool.md](docs/sdk/python-tool.md) |
| 压测与基准 | [benchmarks/README.md](benchmarks/README.md) |
| 变更历史 | [CHANGELOG.md](CHANGELOG.md) |
| 项目进度 | [PROGRESS.md](PROGRESS.md) |

## 弃用提醒

4 个错误码自本版起停止产出，**v0.2.0 移除**：
`UH-KRN-1001`→`UH-KRN-2001`、`UH-LLM-4001`→`UH-CAP-4001`、`UH-LLM-4002`→`UH-CAP-4002`、`UH-LLM-5001`→`UH-CAP-4003`。

首版无存量客户端，故无兼容期。详见 [CHANGELOG 弃用节](CHANGELOG.md#弃用)。

## 下一版计划（v0.2.0）

- **独立执行进程池**：彻底解耦提交与执行，冲 P95 SLO
- per-user principal 多用户支持
- L2 主密钥轮换
- AST 污点分析替代静态黑名单
- TLS + WS 短时票据
- macOS 沙箱矩阵采证
- 移除 4 个弃用错误码

## 致谢

本版经 GPT-6 / Claude Sonnet / Tencent Hunyuan / DeepSeek 多模型独立交叉审查。
M3 中期深审的 4 个生产阻断由 Hunyuan 发现，已全部修复验证。
