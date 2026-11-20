# Brief 0015：M2 独立复检（第三方·确认 P0 与缺口闭合·MS-3 结项）

## 角色与独立性
你是**独立复检审查者**（非实现方 DSH/Codex）。只读审查，**不修改任何受版本控制源码/契约/服务器配置**，只新增复检报告。未阅读其他审查者本轮材料；结论独立从代码+契约+双端运行时得出。

## 基线（已更新）
- commit **3354126** · 门禁声明 **205/205（本机+服务器）**，verify-all full 8/8
- 服务器 192.168.0.28:8080（ssh ipc，D:\uh，生产鉴权 B + 验收期 UH_DEV_MODE=1）；本机 192.168.0.14
- 凭据：服务器 D:\uh\.auth-token；本机 .work/server-auth-token.txt（gitignored，报告不含 token 原文）

## 上轮（brief 0013）阻断与本轮处置（待你独立核实）
| 上轮问题 | 声称处置 | 复检要点 |
|---|---|---|
| **P0 服务器仍 M1 部署** | DSH 已 scp M2 全量+pyyaml，服务器 205/205 | **双端 kernel/*.py SHA256 一致性**（21 文件）+ 服务器测试集 205 + 跨机 GET /themes 200 / POST /plugins 正常 / themes.js 200 |
| M2-1 生产调用闭环 | tasks.plugin_host 路由 | 已安装签名工具经 dev/tool-task + workflow 真实调用成功 |
| M2-1 启动重激活 | startup register_capabilities | 重启后 active 工具可调用 + Hook 订阅恢复 + trace_id 一致 |
| M2-2 沙箱审计/错误码 | plugin-sandbox-audit.jsonl + UH-KRN-2010 | 越界/未声明出网→审计落盘含 plugin/permission/violation/trace_id + MCP 返回 UH-KRN-2010 |
| M2-2 出网静态扫描 | _static_scan（DSH）+ install 联动 | validate/install 拒危险 exec（os.system/subprocess/eval/exec）+ 出网导入告警 |
| M2-3 生产态工具运行 | 子进程注入仓库根 PYTHONPATH | 签名工具安装后子进程不崩溃（crash_count=0）+ 握手调用成功 |
| M2-6 安装来源 | 目录/市场/受信 Git 端到端 | 三种来源安装+启动后调用 + 静态扫描拒绝证据 |
| flaky 崩溃测试 | 轮询至 quarantined | 多次重跑稳定（非时间敏感） |
| SDK 文档断链 | manifest 模板/--no-verify/打包签名补全 | 第三方依文档自助上手计时（≤30 分钟） |

## 已知局限（登记不阻断）
- **SBX Linux/macOS 平台矩阵**：无 Linux/macOS 主机，**登记 M3 补**（用户裁决）；本轮仅验 Windows SBX-01~05。
- web-search proxy_required（真实搜索代理待接通）。
- 主题 REST OpenAPI 登记待评审（只增）；Desktop 主题同步 M3-6。
- 单共享 token per-user principal 待 M3。

## 复检范围
### A. 门禁 + 部署一致性（P0 核心）
- 本机 pytest 205 + verify-all full 8/8 + secret-scan；服务器 server-test.ps1 205。
- **双端 kernel/*.py 逐文件 SHA256 对比**（须全 SAME）；服务器含 plugins/sandbox/hooks/themes/plugin_sdk 五模块。
### B. 跨机 M2 链路（本机→服务器）
- 跨机插件 install/activate/uninstall（签名包）+ 主题切换 <200ms + 钩子审计 trace/span。
- 跨机已安装工具生产调用 + 重启重激活。
### C. 上轮缺口逐项闭合核验（见上表）
### D. 安全专项
- 签名/篡改/不受信密钥拒装；主题 JS 注入矩阵；沙箱越权 KRN-2010 + 审计；静态扫描拒绝；钩子异常隔离；secret 扫描。
### E. Windows SBX 矩阵
- SBX-01/02（SDK 拒绝+审计+错误码+出网静态扫描）、SBX-03 超时 kill、SBX-04 内存限额实机、SBX-05 连续崩溃隔离。Linux/macOS 登记 M3。

## 纪律
- 探针前 UH_PLUGIN_ROOT/UH_PLUGIN_REGISTRY/存储 env 指临时目录；防污染服务器运行态。
- 不改源码/契约/服务器配置；只新增报告。生产链路可观测为准。
- SDK 上手计时真实第三方视角，记录起止。

## 报告契约
输出 docs/reviews/2026-11-20-M2-独立复检报告-<模型名>.md：
- 门禁复现 + **双端哈希一致性表**（P0 闭合证据）
- 上轮缺口逐项闭合判定（通过/未过 + 证据）
- 跨机 M2 链路 + 安全专项 + Windows SBX 矩阵
- SDK 上手计时
- 新发现（严重性 + file:line + 复现）
- **复检结论**：通过 / 有条件通过 / 不通过 + MS-3 结项建议 + 签署模型名
- 探针副作用记录

> 通过后 DSH 审 6 任务卡 → GitHub 关闭 #22~#27 → MS-3 结项 → M3（含 SBX Linux/macOS 补证）。
