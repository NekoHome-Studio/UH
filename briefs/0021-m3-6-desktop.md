# Brief 0021：M3-6 Desktop 壳（Tauri）+ 托盘/快捷键/通知/离线（#33）

## 角色与约定
你是实现者（Codex），先读 AGENTS.md。只加/改代码与测试，**不要 git commit**（DSH 审后提交）。
**M3-1~5 已完成**（compose/K8s/指标/OTel/CrewAI，342 passed + 11 skip，verify-all **11 项 A~K**）。本任务 #33 承接。

## 硬约束
- **禁 pip install / cargo install 到本机全局环境**；Rust/Tauri 工具链若不可用，**产出完整可构建源码 + 配置 + 文档，并如实标注未编译**（Issue 风险项明确「人力紧张时优先砍此项」，故允许源码交付而非强制二进制）。
- 禁写 `$null`/`%VAR%`/`~` 未展开路径；密钥严禁明文。
- **契约冻结只增不改**；不得削弱现有 342 测试（11 skip 为 OTel 未装属正常）。

## 必读
- uh-issues/issues/M3-6.md（#33 任务/验收/**风险：8.8 砍需求顺序第 1 位**）
- **ui/index.html + ui/state-machine.js（UMD `UHStateMachine`：STATES/initialState/transition/reduce）+ ui/themes.js（`createThemeApplier`）** —— **Desktop 必须复用这三者，不得另写一套**（验收：双端一致 ≥95%、主题同步、共用 ui-state-machine.md 状态机）
- docs/contracts/ui-state-machine.md（10 态事件驱动状态机，双端唯一事实源）
- kernel/main.py（静态资源路由 /static/*、UI 入口 /）、kernel/themes.py（主题 REST GET /api/v1/themes、PUT /api/v1/themes/active）
- kernel/llm.py:52（`UH_LLM_BASE_URL` 可配，默认 openai；**Ollama 提供 OpenAI 兼容端点 http://localhost:11434/v1**）、:187（无 key 回退 mock）

## 需求
### 1. Tauri 壳（desktop/ 目录）
- `desktop/src-tauri/`：Cargo.toml + tauri.conf.json + main.rs；前端**直接复用 ui/**（不复制粘贴，用构建脚本或 tauri frontendDist 指向 ui，说明方案）。
- 窗口配置、CSP（允许连本地内核 127.0.0.1:8080）、图标占位。
- **内核生命周期**：Desktop 启动时拉起本地内核子进程（或连接已运行实例），退出时清理；端口冲突处理。

### 2. 原生能力
- **系统托盘**：显示/隐藏窗口、打开 Web UI、退出；状态图标（空闲/运行中/降级）。
- **全局快捷键**唤醒窗口（默认 Ctrl+Alt+U，可配）。
- **桌面通知**：任务完成 / 错误（订阅 WS 事件 task.finished / task.failed 触发，复用状态机终态判定）。

### 3. 双端一致（核心验收）
- **共用 ui/index.html + state-machine.js + themes.js**，Desktop 不另写状态逻辑。
- **主题切换双端同步**：Desktop 与 Web 都经 `PUT /api/v1/themes/active` + `GET /api/v1/themes`，同一内核同一注册表 → 天然同步；给出验证步骤。
- 功能一致性 ≥95%：列出 Web 有而 Desktop 缺的能力（若有）并说明原因。

### 4. 离线 Ollama（验收：离线模式可完成 chat）
- 文档 + 配置：`UH_LLM_BASE_URL=http://localhost:11434/v1`、`UH_LLM_PROVIDER=openai`（兼容）、`UH_LLM_API_KEY=ollama`（占位非真密钥）、`UH_LLM_MODEL=<本地模型>`。
- **核实 kernel/llm.py 是否真能走 Ollama 兼容端点**；若有阻碍（如强制 Bearer 校验、流式格式差异）请修复并测试。
- 无 Ollama 时用 mock provider 验证离线 chat 链路（无外网依赖）。

### 5. 测试与门禁
- `tests/unit/test_desktop.py`：断言 desktop 配置正确引用 ui/ 三件套（无重复状态机）、tauri.conf.json CSP/窗口/快捷键配置存在、托盘菜单项齐全、通知订阅终态事件。
- **双端一致性静态校验**：脚本断言 Desktop 未引入独立 state-machine 副本、主题走同一 REST 端点。
- `tests/unit/test_llm_offline.py`（或扩展 test_llm）：Ollama 兼容 base_url 构造正确、mock 离线 chat 完成、无外网调用。
- 可选：接入 verify-all 新增 **L 项**（Desktop 配置与双端一致性），保持既有 11 项不变。

## 验收（#33）
- 与 Web 功能一致性 ≥95%（列对照表）
- 离线模式可完成 chat（mock 或 Ollama 实测）
- 主题切换双端同步（同内核同注册表，给验证步骤）

## 门禁（跑并报真实数字）
- `python -m pytest -q`（≥342 全绿，skip 说明）
- `node scripts/verify-all.mjs --full`（全绿）
- `node scripts/secret-scan.mjs`（rc=0）

## 报告
新增改动文件 + **Web/Desktop 功能对照表（一致性百分比与缺项原因）** + 双端共用三件套的证明 + 主题同步机制 + 离线 chat 验证结果 + **Tauri 是否实际编译（未编译须明说原因与构建步骤）** + 三门禁真实数字。**不要提交**。
