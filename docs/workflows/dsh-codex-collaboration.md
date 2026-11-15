# DSH × Codex 协作工作流（跨 harness）

> 目标：DSH（DeepSeek，编排/评审）+ Codex CLI（OpenAI，实现）双 harness 协作，
> 把 UH! 里程碑实现任务交给 Codex 在沙箱里做，DSH 写契约、审 diff、跑门禁、落提交。
> 技能依据：codex-delegate（编排循环）+ codex-cli-calling（调用机制）。

## 1. 角色分工

| 角色 | 承担 | 不承担 |
|---|---|---|
| DSH（我） | 架构/契约/决议、写 brief、审 diff、跑门禁、git 提交、部署、判断 | 不替 Codex 写实现细节 |
| Codex | 有界实现任务、沙箱内改码、跑测试、产出 result.json | 不提交（DSH 落）、不做架构决策 |

原则：**契约与判断归 DSH，打字与验证归 Codex**。每个 brief 只装一个任务。

## 2. 五步循环（每个任务）

1. **写 brief**（DSH）：目标 / 现状 / 改什么 / 不动什么 / 真实门禁命令 / 报告契约 / 声明 Codex 不提交。Codex 只看 brief 文本，无仓库记忆。
2. **派发**：`node <codex-delegate skill>/scripts/relay.mjs --brief briefs/NNNN-*.md --cd <repo>`，包装 codex exec 并写 result.json。
3. **读结果**：result.json（状态/会话 id/版本）+ `git diff` 看改动。
4. **验证**（DSH）：跑门禁（`python -m pytest`、`node scripts/verify-all.mjs`），必要时 `codex exec resume <id>` 追加修正。
5. **落地**（DSH）：审过则 git commit + push；不过则退回步骤 1 重写 brief。

## 3. 连通前置（当前阻塞点）

Codex 走 ChatGPT 后端，本机需代理才能到 chatgpt.com / api.openai.com。

- 现状（2026-11-15 实测）：codex-cli 0.166.0 已装、ChatGPT 已登录；但系统代理关闭、无代理端口监听、chatgpt.com 直连超时 → **连不上**。
- 解法 A（推荐）：启动代理工具后，`scripts/codex-connect.ps1` 自动探测端口并设 HTTPS_PROXY 重试。
- 解法 B：直接告知代理地址 host:port，DSH 为 codex 进程注入 HTTPS_PROXY/HTTP_PROXY。
- 解法 C（免代理）：提供 OpenAI 兼容中转端点 + 密钥，经 CODEX_API_KEY + base_url 走中转（密钥只注入单进程，不落仓库/日志）。
- 降噪：codex config 里 psd2live MCP（127.0.0.1:23871）未运行会刷错；派发可加 `--ignore-user-config` 跳过 ambient MCP。

## 4. 任务路由（什么给 Codex）

| 给 Codex（有界实现） | DSH 自留 |
|---|---|
| M0-x/M1-x 模块实现、bug 修复、补测试 | 契约编写/修订、决议登记、评审 |
| 重构（带明确边界与门禁） | 架构变更、跨模块设计 |
| 样板/迁移/批量改动 | git 提交、部署、对外 API 决策 |

## 5. 门禁（brief 必须写明真实命令）

- Python：`python -m pytest -q`（当前 24 用例，M0-2 端点修复后应全绿）
- 契约：`node scripts/verify-all.mjs`（fast）/ `--full`
- 交叉一致性：`node scripts/review-precheck.mjs`

## 6. 安全边界

- Codex 沙箱默认 `workspace-write`（只读分析用 read-only）；不用 `--dangerously-bypass-approvals-and-sandbox`。
- Codex 不提交；DSH 审 diff 后落。
- 密钥只注入单进程，不写脚本/仓库/日志。
- 服务器部署仍由 DSH 经 ssh ipc 执行（Codex 不碰生产）。

## 7. 发起方式（A / B 二选一）

- **A · CLI 派发**：DSH 经 relay/codex exec 派发，会话在 Codex CLI（`codex resume <id>` 查看）。已验证（brief 0001）。
- **B · GUI 发起（应用可见）**：DSH 备好 brief → 用户在 Codex 桌面应用的 UH! 项目里新建会话贴入 brief 发起 → 会话显示在应用列表 → Codex 完成后 DSH 审 diff + 门禁 + 落提交。
  - 原因：Codex 桌面 GUI 与 CLI 是两套会话存储，CLI exec 会话不进 GUI 列表；经 GUI 项目发起才在应用可见。
  - 步骤：Codex 应用 → 左侧 UH! 项目 → 新聊天 → 贴 briefs/NNNN-*.md 全文 → 发送 → 完成后通知 DSH 审查。

## 8. 首个协作任务（已备好，待连通）

briefs/0001-m0-2-route-endpoint.md：修复 M0-2 dev/route 端点 3 个失败测试（路由分类已 100/100，仅端点层 bug）。
这是验证协作链路的最小真实任务：Codex 修 → DSH 审 → 跑 pytest → 落提交 → 部署服务器。
