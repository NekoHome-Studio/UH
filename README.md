# UH!（Ultra Harness）

> 集百家之长，做用户之尚 —— 个人 AI Agent 运行底座
> 当前阶段：v0.1.0 · MS-0 契约冻结周（W1）· 计划基线 20 周

本仓库当前承载 **契约与任务基线**：架构文档、机器可读契约、39 个 GitHub Issue 交付包与执行进度。代码实装按里程碑推进（M0 骨架 → M1 能力 → M2 插件 → M3 生产）。

## 目录导航

| 路径 | 内容 |
|---|---|
| [docs/contracts/](docs/contracts/) | 契约唯一事实源：错误码、manifest schema、事件字典、任务流、UI 状态机 |
| [docs/contracts/revisions/](docs/contracts/revisions/) | 架构文档逐字修订对照 + v3 基线差异评估 |
| [docs/source/](docs/source/) | 上游架构文档基线（v0.1.0 与 v3 整合版 docx） |
| [uh-issues/](uh-issues/) | 39 个 Issue 交付包：正文、labels/milestones/批量创建脚本、CSV |
| [PROGRESS.md](PROGRESS.md) | 执行进度日志（随任务推进更新） |

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

P0-4 沙箱分期与平台矩阵 → P0-5 部署暴露面与 RBAC → P0-6 机器可读契约与图（OpenAPI/AsyncAPI）。
