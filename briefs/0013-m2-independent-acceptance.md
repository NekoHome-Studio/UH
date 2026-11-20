# Brief 0013：M2 插件期独立验收（第三方·跨机·MS-3 结项口径）

## 角色与独立性
你是**独立验收审查者**（非实现方 DSH/Codex）。只读审查代码/契约/运行时，**不修改任何受版本控制源码/契约/服务器配置**，只新增你的验收报告。
**未阅读其他审查者的 M2 验收材料**；结论独立从「代码 + 冻结契约 + 跨机真实运行时」得出。可参考 M2 收尾验收报告与 Issue 卡作为待验基线。

## 基线
- commit **067e7a2** · tag 参照 contract-v0.1.0 / contract-internal-v1 / contract-events-v1
- 门禁声明：195/195（本机+服务器），verify-all full 8/8
- 服务器：192.168.0.28:8080（ssh ipc，D:\uh，生产鉴权 B + 验收期 UH_DEV_MODE=1）；本机 192.168.0.14
- 凭据：服务器 D:\uh\.auth-token；本机 .work/server-auth-token.txt（gitignored，报告不含 token 原文）

## 必读材料
- docs/reviews/2026-11-20-M2-收尾验收.md（本地自验基线 + 各任务结项待补项）
- uh-issues/issues/M2-1.md ~ M2-6.md（#22~#27 任务与验收标准）；uh-issues/README.md §11 验收门禁速查（MS-3 出口）
- docs/contracts/sandbox.md §5（SBX-01~09 验收·平台·分期矩阵）
- docs/sdk/python-tool.md（SDK 指南，第三方上手计时依据）
- docs/contracts/manifest/（插件 manifest schema）；kernel/plugins.py / sandbox.py / hooks.py / themes.py / plugin_sdk.py

## 验收范围（MS-3 结项口径）

### A. 门禁复现（亲测）
- python -m pytest -q（195）；node scripts/verify-all.mjs --full（8/8）；服务器 server-test.ps1（195）。

### B. 六任务逐项核验
- **M2-1 驱动**：签名不符拒装（SBX-06）、激活失败回滚、停用超时强杀、运行中内核签名 Hook 装卸、审计/订阅清理；**补**已安装工具从生产能力目录调用闭环 + 内核启动后插件重激活。
- **M2-2 沙箱**：SDK 权限检查、进程启动超时回收、连续崩溃三次隔离；**补 SBX 平台矩阵**——Win SBX-01/02（SDK/代理端拒绝 + 审计 + 出网静态扫描）、SBX-04 实机内存限额、Linux/macOS SBX-01~05（留存拦截/回收结果 + 错误码 + 审计）。
- **M2-3 SDK**：uh plugin validate / dev --once MCP 握手 + 工具枚举；**第三方依 docs/sdk 从创建插件计时到实际调用成功（目标 ≤30 分钟，留存耗时）**；plugin dev 热重载实测；签名工具注册与调用；web-search proxy_required 现状登记。
- **M2-4 主题**：签名包安装、Web 切换 <200ms（E2E-06，留存多次耗时）、非法 CSS/资产拒绝、刷新与内核重建恢复；WOFF2 渲染样本；Desktop 同步按 M3-6（登记不阻断）。
- **M2-5 钩子**：签名 Hook 处理生产消息入口路由事件，trace_id/span_id/event_id 对照；慢执行/异常/卸载/超时回收回归；跨平台进程行为 + 服务重启后 Hook 恢复策略。
- **M2-6 CLI**：本地包/目录验证、market 本地镜像、Git 主机白名单、audit 权限矩阵；**实跑受信 Git 源安装** + 目录/市场/Git 安装验收 + 静态扫描越界/危险调用拒绝证据。

### C. 跨机插件链路（本机→服务器）
- 跨机插件 install/activate/uninstall（签名包）；主题跨机切换；钩子跨机审计事件。
- 探针先隔离存储/插件根到临时目录，防污染服务器运行态。

### D. 安全专项
- 主题包 JS 注入拦截（script/事件处理器/CSS url/javascript/expression）。
- 插件签名验签（Ed25519，信任库 UH_PLUGIN_TRUSTED_KEYS_FILE）；未签名/篡改拒装。
- 沙箱越权拒绝 + 审计；钩子异常隔离不拖主链路。
- secret 扫描（scripts/secret-scan.mjs）无明文密钥。

## 已知局限（登记不阻断，知悉即可）
- web-search 返回 proxy_required，真实搜索代理待接通。
- 主题 REST（GET/PUT /api/v1/themes）OpenAPI 登记待评审（只增后续版本）。
- Desktop 主题同步 M3-6 验收。
- 单共享 token 模型 per-user principal 待 M3。

## 纪律
- 探针前 UH_PLUGIN_ROOT/UH_PLUGIN_REGISTRY/存储 env 指临时目录；清库类操作慎用。
- 不改源码/契约/服务器配置；只新增报告。
- 生产链路可观测为准，fixture 全绿不作验收依据。
- SDK 上手计时须真实第三方视角（依文档从零创建到调用成功），记录起止时间。

## 报告契约
输出 docs/reviews/2026-11-20-M2-独立验收报告-<模型名>.md：
- 门禁复现（亲测）+ 部署一致性（内核文件哈希双端对比）
- 六任务逐项判定（通过/部分/未过 + 证据）+ SBX 平台矩阵证据表
- SDK 上手计时（起止 + 耗时 + 步骤）
- 跨机插件链路 + 安全专项结果
- 新发现（按严重性 + file:line + 复现）
- **验收结论**：通过 / 有条件通过 / 不通过 + MS-3 结项建议 + 签署模型名
- 探针副作用记录

> 通过后 DSH 审 6 任务卡证据 → GitHub 关闭 #22~#27 → MS-3 结项 → M3 生产期。
