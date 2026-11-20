# Brief 0014：M2 结项功能缺口（独立验收 P0 已部署后补完）

## 角色与约定
你是实现者（Codex），先读 AGENTS.md。只改代码与测试，**不要 git commit**（DSH 审后提交）。复用现有 plugins/sandbox/hooks/plugin_sdk。

## 背景
M2 独立验收（Claude/DeepSeek/Hunyuan）不通过。P0 服务器未部署已由 DSH 修复。DSH 已修：flaky 崩溃测试（轮询）、SBX-02 静态扫描（validate 拒危险 exec+出网告警）、SDK 文档补全。**剩余架构级缺口需你补完**：

## 必读
- docs/reviews/2026-11-20-M2-独立验收报告-*.md（三份，缺口证据）+ M2-独立验收总结与模型能力分析.md
- kernel/plugins.py（PluginDriver/records/loader）、kernel/tasks.py（_run_tool→MCPHost.call_tool）、kernel/main.py（startup register_capabilities/retrieval_host/dev_tool_task）、kernel/sandbox.py、kernel/plugin_sdk.py
- uh-issues/issues/M2-1.md/M2-2.md/M2-3.md/M2-6.md；docs/contracts/sandbox.md §5

## 需求（按优先级）
1. **M2-1 生产能力调用闭环**：已安装工具插件须能从生产任务路径调用。tasks.py 工具任务当前固定走 retrieval_host；改为经 plugin registry 查找已安装工具→路由到该插件的 MCP host（spawn 插件子进程 stdio MCP，复用 plugin_sdk）。dev/tool-task 与 workflow 工具调用均须支持已安装插件工具。
2. **M2-1 启动重激活**：main.py startup 遍历 plugin registry，对 status=active 的工具/钩子插件重激活（spawn MCP host / 注册 HookManager 订阅）；重启后已安装插件恢复 active 与钩子订阅。
3. **M2-2 沙箱审计 + 错误码透出**：沙箱拒绝（fs 越界/出网未声明）须落审计日志（结构化，含 plugin/permission/violation/trace_id）；MCP 工具调用返回须含 UH-KRN-2010 错误码（当前仅文本）。
4. **M2-3 生产态工具运行**：签名工具子进程须能导入 kernel（PYTHONPATH 含仓库根，或 SDK 解耦 kernel 导入）；安装后工具子进程不再因 ModuleNotFoundError 崩溃→quarantined。
5. **M2-6 安装来源端到端**：受信 Git 源实跑安装 + 目录/市场/Git 三种来源端到端测试 + 静态扫描越界/危险调用拒绝证据（DSH 已加静态扫描，补 install 路径联动 + 测试）。
6. 可选：uh plugin pack/sign 子命令（规范化摘要 + Ed25519，参考 _canonical_digest/_make_package）。

## 测试（必须）
- 已安装工具经生产任务路径调用成功（闭环）。
- 重启后 active 插件重激活 + 钩子订阅恢复。
- 沙箱拒绝审计落盘 + MCP 返回 UH-KRN-2010。
- 签名工具安装后子进程正常运行（不崩溃）。
- Git/目录/市场安装端到端 + 静态扫描拒绝。
- 不得削弱现有测试（含 DSH 新加静态扫描/flaky 修复）。

## 不动什么
- docs/contracts/ 冻结契约（主题 REST OpenAPI 登记走只增评审）；DSH 已修的静态扫描/flaky/SDK 文档。

## 门禁
- python -m pytest -q（全绿）；node scripts/verify-all.mjs --full（8/8）。

## 报告
输出：改动文件 + 设计要点（闭环路由/重激活/审计/错误码/PYTHONPATH）+ 门禁结果 + 各缺口验证证据。不要提交。
