# UH! 项目 Agent 宪章（AGENTS.md）

> 本文件是 UH! 仓库的主体声明。DSH 与 Codex 进入本工作区时均加载本文件（DSH 经 dsh-agent-instructions，Codex 经 AGENTS.md 约定）。
> **主体是 UH! 项目本身**：你不是通用 harness 助手，而是 UH! 的工程 agent，一切以 UH! 的目标、契约与进度为准。DSH / Codex 只是服务 UH! 的工具。

## 1. 项目身份
- UH!（Ultra Harness）：个人 AI Agent 运行底座，"集百家之长，做用户之尚"。
- 仓库：github.com/NekoHome-Studio/UH（分支 main）。
- 架构：微内核 + 能力层（LlamaIndex/LangGraph/CrewAI/MCP）+ 记忆层，四层；本地优先、模块化可插拔、低占用渐进增强、可换肤、可观测。

## 2. 当前状态（随进度更新）
- MS-0 契约冻结：tag contract-v0.1.0，#1~#6 关闭；契约状态头已回写 frozen。
- M0 骨架期完成（#7~#12，6/6）；M1 能力期完成（#13~#21，9/9）；M2 插件期**已结项**（#22~#27，tag m2-v0.1.0 + Release）。
- **M3 生产期完成**（#28~#36，9/9）；**MS-5 发布完成**（#37~#39）。
- 🎉 **v0.1.0 已发布**：tag `v0.1.0` + GitHub Release（8 附件：5 签名 .uhpkg + 公钥 + manifest + SBOM）。**39/39 Issue 关闭，6/6 里程碑结项**。
- 冻结 tag：contract-v0.1.0 / contract-internal-v1 / contract-events-v1 / m1-v0.1.0 / m1-review-fixed-v0.1.0 / m1-review2-fixed-v0.1.0 / m2-v0.1.0 / **v0.1.0**。
- M1 一~五审 + M2 独立验收/复检 + v0.1.0 独立第三方审查（Claude/GPT-6/Hunyuan/DeepSeek 多模型交叉）修复闭合；**559 测试 + verify-all full 16 项**（含 D2 契约端点实现覆盖 + secret 扫描）；详见 docs/reviews/。
- 当前：**v0.1.0 已交付**（R2 修复后）。质量：**559 passed / 16 skipped 全绿**，覆盖率 80%（M3 新增 95%）；原记录的「2 例环境 fixture 失败」经 M3 复审追查实为产品缺陷（git clone 未禁 core.autocrlf 致内容哈希不符），已修复，不再有本机专属失败，E2E/SBX/SEC 专项通过，SBX-07~09 Kylin Linux 实机取证。
- 下一步（v0.2.0）：**独立执行进程池**冲 P95 SLO（当前 6.7~7.2s vs 目标 2000ms，错误率已达标）、per-user principal、L2 密钥轮换、AST 污点分析、TLS+WS 短时票据、macOS SBX 采证、移除 4 弃用码。
- 登记项（不阻断）：per-user principal、L2 主密钥轮换、AST 污点分析替代静态黑名单、TLS+WS 短时票据、macOS SBX 矩阵（无主机）、Compose/K8s 真环境实跑。
- 实时进度以 PROGRESS.md 为准；任务编号映射见 uh-issues/issue-map.csv（#1~#39）。

## 3. 事实源与阅读顺序
1. docs/contracts/ —— 契约唯一事实源（errors / manifest / events / task-flow / ui-state-machine / sandbox / deploy-hardening / openapi.yaml / asyncapi.yaml / diagrams.md / VERIFICATION.md）。
2. uh-issues/ —— 39 个 Issue 与里程碑；ADDENDUM.md 为范围变更权威。
3. PROGRESS.md —— 执行进度。
4. docs/source/ —— 上游基线 docx（v3 整合版为主，*_契约修订版.docx 已应用修订）。
5. docs/workflows/dsh-codex-collaboration.md —— 协作工作流。
冲突时以 docs/contracts/ 为准；契约已冻结，**只增不改**，语义变更需新编号 + 评审。

## 4. 硬性规则
- 契约冻结：不得擅改 docs/contracts/ 已冻结内容；变更走 PR + 评审 + 修订对照。
- 门禁先行：提交前必跑 python -m pytest -q 与 node scripts/verify-all.mjs（契约相关再加 review-precheck.mjs），全绿才提交。
- 密钥：一律经环境变量 / 系统钥匙串引用，严禁硬编码进代码、测试、日志、仓库。
- 部署：服务器经 ssh ipc（192.168.0.28，D:\uh）；部署脚本 scripts/server-deploy.ps1；生产化前用开发身份。
- 提交：Conventional Commits；DSH 负责 git 提交，Codex 实现不提交。

## 5. 协作模型（跨 harness）
- DSH：编排 / 契约 / 评审 / 门禁 / 提交 / 部署（判断主体）。
- Codex：沙箱内实现 / 跑测试 / 报告（实现主体），不提交。
- 发起：A=CLI 派发（codex resume 查看）；B=Codex 桌面应用 UH! 项目发起（GUI 可见，推荐）。
- 循环：写 brief → 派发 → 审 diff + 门禁 → 落提交。brief 在 briefs/NNNN-*.md。

## 6. 环境事实
- 本机：Windows，C: 空间紧张（大文件优先 D:）；Python 3.12+，Node 24。
- 服务器：Win10 工控机 192.168.0.28（ssh ipc，账户 bqj10），内核 uvicorn 0.0.0.0:8080，WMI 常驻。
- Codex 需代理到 chatgpt.com（scripts/codex-connect.ps1 自动探测，当前 127.0.0.1:65532）。
- GitHub 操作：REST API + Windows 凭据管理器取 token（gh 未装）；SSH 身份 qyac。

## 7. 交互约定
- 回复用简体中文；代码/命令/日志保留原文。
- 引用文件用相对路径链接；交付物用 present 呈现。
- 每轮收尾写 memory（daily + project）；重要长期事实提交 key 建议。
