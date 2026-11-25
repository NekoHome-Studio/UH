# UH!（Ultra Harness）

[![Release](https://img.shields.io/github/v/release/NekoHome-Studio/UH?display_name=tag&sort=semver)](https://github.com/NekoHome-Studio/UH/releases/tag/v0.1.0)
[![Tests](https://img.shields.io/badge/tests-421%20passed-brightgreen)](https://github.com/NekoHome-Studio/UH/blob/main/docs/reviews/2026-11-25-R1-%E5%8F%91%E5%B8%83%E5%9B%9E%E5%BD%92%E6%8A%A5%E5%91%8A.md)
[![Gates](https://img.shields.io/badge/gates-15%2F15-brightgreen)](https://github.com/NekoHome-Studio/UH/blob/main/scripts/verify-all.mjs)
[![Coverage](https://img.shields.io/badge/coverage-80%25-green)](https://github.com/NekoHome-Studio/UH/blob/main/RELEASE.md)

> 📢 **[v0.1.0 发布公告](https://github.com/NekoHome-Studio/UH/issues/40)** · Watch 本仓库可收到后续版本通知


> 集百家之长，做用户之尚 —— 个人 AI Agent 运行底座
> 当前阶段：v0.1.0 · M1 能力期完成（9/9）+ 审查修复 · 计划基线 20 周

本仓库当前承载 **契约与任务基线**：架构文档、机器可读契约、39 个 GitHub Issue 交付包与执行进度。代码实装按里程碑推进（M0 骨架 → M1 能力 → M2 插件 → M3 生产）。

## 目录导航

| 路径 | 内容 |
|---|---|
| [docs/contracts/](docs/contracts/) | 契约唯一事实源：错误码、manifest schema、事件字典、任务流、UI 状态机 |
| [docs/contracts/revisions/](docs/contracts/revisions/) | 架构文档逐字修订对照 + v3 基线差异评估 |
| [docs/source/](docs/source/) | 上游架构文档基线（v0.1.0 与 v3 整合版 docx） |
| [uh-issues/](uh-issues/) | 39 个 Issue 交付包：正文、labels/milestones/批量创建脚本、CSV |
| [PROGRESS.md](PROGRESS.md) | 执行进度日志（随任务推进更新） |
| [Docker Compose 部署](docs/deployment/docker-compose.md) | 服务启动、密钥注入、端口核对和签名插件安装 |

## 契约状态（已冻结 · 2026-11-15 评审通过 · tag contract-v0.1.0）

| 任务 | 交付物 | 校验 |
|---|---|---|
| P0-1 错误码事实源 | [errors.md](docs/contracts/errors.md)：13 有效码 + 4 deprecated | grep 一致性全过 |
| P0-2 manifest schema | [manifest.schema.json](docs/contracts/manifest.schema.json) + 四类 type schema | ajv 7/7 |
| P0-3 REST→WS 闭环（扩范围） | [task-flow.md](docs/contracts/task-flow.md) · [events.md](docs/contracts/events.md) · [events.schema.json](docs/contracts/events.schema.json) · [ui-state-machine.md](docs/contracts/ui-state-machine.md) | ajv 25/25 |

## 上游基线

《Ultra-Harness技术文档_整合版_v3（润色校对）》为主基线（2026-11-14 切换），《UH!架构设计文档_v0.1.0》保留对照；差异与裁决见 [基线变更-v3-差异评估.md](docs/contracts/revisions/基线变更-v3-差异评估.md)。

## 批量创建 Issues

见 [uh-issues/README.md](uh-issues/README.md) 第 9 节与 [ADDENDUM.md](uh-issues/ADDENDUM.md)（范围变更记录，任务总数 38→39）：

```bash
REPO=NekoHome-Studio/UH bash uh-issues/labels.sh
REPO=NekoHome-Studio/UH bash uh-issues/milestones.sh
REPO=NekoHome-Studio/UH bash uh-issues/create-issues.sh
```

> ⚠️ 39 个 Issue / 18 labels / 6 milestones 已于 2026-11-14 经 API 实建（编号映射见 [uh-issues/issue-map.csv](uh-issues/issue-map.csv)）。重跑 create-issues.sh 会产生重复 Issue；增量变更请直接在 GitHub 编辑或走 API。

## 下一步

M1 生产链路二次验收（REST 全局鉴权方案待定）→ M2 插件期：M2-1 插件 SDK/manifest（#22 起，基于 contract-internal-v1）→ 沙箱/钩子/插件加载。

> 里程碑：MS-0 契约冻结（#1~#6）→ M0 骨架（#7~#12）→ M1 能力（#13~#21）均已完成；三模型审查修复见 docs/reviews/。冻结 tag：contract-v0.1.0 / contract-internal-v1 / contract-events-v1 / m1-v0.1.0。

## 发布与变更

- **[RELEASE.md](RELEASE.md)** —— v0.1.0 发布说明（亮点、质量数据、**已知限制**、快速开始）
- **[CHANGELOG.md](CHANGELOG.md)** —— 新增 / 变更 / 修复 / 弃用四类变更，弃用项标注替代方案与移除版本
- **[benchmarks/README.md](benchmarks/README.md)** —— 压测基准与 SLO 达成情况

### 部署手册
| 场景 | 文档 |
|---|---|
| Docker Compose | [docker-compose.md](docs/deployment/docker-compose.md) |
| Kubernetes | [kubernetes.md](docs/deployment/kubernetes.md) |
| 多进程调优 | [multiprocess.md](docs/deployment/multiprocess.md) |
| 安全加固 | [security-hardening.md](docs/deployment/security-hardening.md) |
| 加密与脱敏 | [security-m3-8.md](docs/deployment/security-m3-8.md) |
| 链路追踪 | [tracing.md](docs/deployment/tracing.md) |
| 协作模块 | [collaboration.md](docs/deployment/collaboration.md) |
| 无 root Linux | [kylin-userland.md](docs/deployment/kylin-userland.md) |

> **已知限制**：100 并发 P95 未达 SLO（错误率达标），详见 RELEASE.md。低并发（≤10）表现正常。
