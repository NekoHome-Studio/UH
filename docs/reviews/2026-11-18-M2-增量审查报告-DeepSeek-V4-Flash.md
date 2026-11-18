# M2 增量审查报告（未提交工作树）· DeepSeek-V4-Flash

| 项 | 内容 |
|---|---|
| 审查对象 | 基线 `3031015` 之上的**未提交改动**：7 个修改文件 + 6 个新增文件（M1 五审条件项 + M2-1 插件驱动 + M2-2 沙箱第一层） |
| 审查时间 | 2026-11-18 23:10 ~ 23:55（Asia/Shanghai） |
| 签署模型 | **deepseek-v4-flash**（provider `deepseek-official`，reasoningEffort `high`；记账名 `deepseek-flash`）· preset `standard` |
| 审查方式 | 逐文件读 diff + 新模块全文 + 门禁复跑 + 隔离探针 15 项（`.work/probe6/new.py`，存储指向 `.work/probe6/tmp`） |
| 结论 | **有条件通过（增量）** —— 实现质量高于前几轮，但有 3 处「看起来完成、实际不可用/未接线」必须先明确，否则会把 M2-1/M2-2 误判为已交付 |

---

## 0. 改动构成

| 类别 | 文件 |
|---|---|
| 契约（未提交） | `task-flow.md`（+2 条：client_id 归属、终态回连）、`openapi.yaml`（bearerAuth 说明 + session.client_id）、`asyncapi.yaml`（连接说明） |
| 内核 | `main.py`（+189：签名 client_id、会话归属表、插件 REST、dev 守卫复用）、新增 `plugins.py`(724)、`sandbox.py`(305) |
| 门禁 | `verify-all.mjs`（子进程 stderr 保留）、`review-precheck.mjs`（改用 `secret-scan.mjs`）、新增 `secret-scan.mjs` |
| 依赖 | `pyproject.toml`：`pyyaml` 提为运行时依赖（插件清单需要），dev extras 相应收敛 |
| 测试 | 新增 `test_plugins.py`(8)、`test_sandbox.py`(4)、`test_review5_fixes.py`(3) |
| 文档 | 新增 3 份修订对照（M1 五审 N1–N5 / M2-1 / M2-2），均标「待评审」——符合只增流程 |

---

## 1. 门禁复现（亲跑）

| 门禁 | 结果 |
|---|---|
| `python -m pytest -q` | **162 passed**（前值 148 → +14） |
| `node scripts/verify-all.mjs` | 6/6 PASS |
| `node scripts/verify-all.mjs --full` | **8/8 PASS**（含 swagger-cli、@asyncapi/parser） |
| `secret-scan.mjs` 自测 | `hits: 0`（无明文 token 命中） |

---

## 2. 新增能力实测（15 项探针，13/15 通过；2 项失败见 §3）

| 组 | 断言 | 结果 |
|---|---|---|
| A 签名 `client_id` | `/sessions` 返回 `uhcid1.<b64>.<hmac>`；带签名头 → 202；**伪造裸 `uh_ses_xxx` 头的裸客户端 → 403 `USR-1002`**；篡改签名 → 服务端重新签发而非采纳 | ✅（A3 首轮为探针 cookie 干扰，定点复测确认 403） |
| B 插件安装边界（生产） | 未签名 `.uhpkg` → 403 `UH-KRN-2010`；`no_verify=true` 在生产 → 同样拒绝 | ✅ |
| C 本地开发路径 | `UH_LOCAL_MODE=1 + UH_PLUGIN_ALLOW_UNVERIFIED=1` + `no_verify` → 201；列表可见；`PATCH /plugins/{name}/config` → 200 | ✅ |
| D 沙箱策略 | 越界写被拒（`KRN-2010`）；未声明出网被拒；Windows 运行计划 `degraded=True` 且给出理由 | ✅ |
| E 接线检查 | 内核安装路径的 4 类加载器全是 `NoopPluginLoader` | ❌ 见 §3-3 |

包体安全另由单测覆盖（路径穿越、符号链接、重复路径、签名不匹配先于解包拒绝、activate 失败回滚、deactivate 超时强杀 + quarantine）。

---

## 3. 发现

### P1-1 `update_config` 改了不落盘（实测复现）

- 现象：`PATCH /plugins/hello/config {"timeout_ms": 2000}` → 200；重新构造 `PluginDriver` 读注册表 → `config={'timeout_ms': 1000, 'memory_limit_mb': 16}`（旧值）。
- 根因：`plugins.py:707` 只更新 `record.manifest.config`；而 `_save_registry()` 序列化的是 `manifest.raw`（`to_dict()` → `manifest.raw`），raw 里的 `config` 从未同步。
- 影响：插件配置"当下生效、重启丢失"，且注册表与内存态不一致。
- 修法：`update_config` 同时写回 `record.manifest.raw["config"]`，或 `to_dict()` 用 `self.manifest.config` 覆盖 raw。

### P1-2 生产环境没有任何可信公钥通道 → 已签名插件也装不上

- `PluginSecurityConfig.from_env()` 只读 `UH_PLUGIN_SIGNATURE_EXCEPTIONS` / `UH_PLUGIN_ALLOW_UNVERIFIED` / `UH_PLUGIN_SIGNATURE_REQUIRED`，**不读任何 trusted_keys 来源**；`trusted_keys` 默认空 dict。
- 结果：`_verify_signature` 必然在「插件签名公钥不受信任」处失败（`plugins.py:460-461`）。也就是说 **M2-1 的"官方签名插件"在部署里无法安装**，单测是靠直接注入 `PluginSecurityConfig(trusted_keys={"test": public})` 绕过的。
- 修法：加 `UH_PLUGIN_TRUSTED_KEYS_FILE`（JSON: key_id → 公钥 b64/hex）并在 `from_env` 载入；无 key 时在非本地模式启动告警。

### P1-3 沙箱未接入内核（M2-2 目前是库，不是能力）

- 实测：`app.state.plugin_driver.loaders` 四类全是 `NoopPluginLoader`；`SandboxRunner`/`ProcessPluginLoader` 在主链路无构造点。
- 影响：manifest 里的 `permissions` / `timeout_ms` / `memory_limit_mb` 在真实安装流程中**不产生任何约束**；插件 activate 不启动独立进程，也没有 Job Object 内存上限。若按"文件已存在"记 M2-2 交付，会高估能力。
- 建议：`main.py` 里按 `manifest.type` 注入 `ProcessPluginLoader(SandboxRunner(...))`（或显式开关），并在 M2-2 对照里把"内核接线"列为未完成项。

### P2-1 Windows 沙箱 = 仅进程/内存/时间，无 FS/网络强制

`build_plan` 在 Windows 恒返回 `degraded=True, reason="M2 Windows FS/network enforcement is SDK/代理级"`——文档（M2-2 对照）已诚实标注，但目标部署机就是 Win10，M2-2 的实际隔离强度等于「Job Object + 约定」。建议在 `sandbox.md` 的 SBX-01/02 行与验收口径里同步这一事实，避免"沙箱已实装"的误读。

### P2-2 `client_id` 密钥默认是进程随机值

`_read_client_identity_secret()` 在未配 `UH_CLIENT_ID_SECRET(_FILE)` 时 `secrets.token_bytes(32)`：重启后所有已签发凭据立即失效（内存任务同生共死，故当前可接受），但**多进程/反向代理多 worker 场景会全量 401/403**。建议非本地模式且未配置时启动打 WARN，并在部署清单里列为必配项。

### P3-1 SSE 对未知任务返回 200 + 帧内错误

`GET /tasks/<不存在>/stream?token=` → **200 `text/event-stream`**，错误以事件帧下发（`{"event":"error"}`）。与 REST 其它端点「任务不存在 → 404 `USR-1003`」不一致。建议改为连接前 404，或把该行为写进 task-flow §2。

### P3-2 改动未提交

162 绿 + full 8/8 全部发生在工作树，尚未形成 commit；`docs/contracts/*` 三处修改与 3 份「待评审」修订对照需要**同批**入库评审，否则契约正文会先于修订对照进入历史，破坏"只增 + 修订对照"的可追溯性。

---

## 4. 结论

**有条件通过（增量）。**

这轮实现的工程质量明显高于前几轮：签名 `client_id` 真做了 HMAC 且伪造被拒、插件包体安全面（路径穿越/符号链接/重复路径/先验签后解包/activate 回滚/超时强杀）覆盖得扎实、`verify-all` 吞 stderr 的老毛病改掉了、`secret-scan` 从 5 个目录扩到全部受控文件。探针 13/15 通过，两处失败中一处是探针 cookie 干扰（已复测为正确行为），另一处（C4 配置不持久化）是真 bug。

**三项 P1 必须先处置**，否则会把 M2-1/M2-2 记成"已交付"：
1. `update_config` 落盘（一行级修复）。
2. 可信公钥通道（否则签名插件装不上，M2-1 只剩 no_verify 开发路径）。
3. 沙箱接线或明确标注未接线（M2-2 的隔离能力当前不在运行路径上）。

**建议提交顺序**：先修 P1-1/P1-2 → 接线或标注 P1-3 → 把 3 份修订对照与契约改动同批 commit（含 162 测试）→ 再进 M2-1 的正式验收。

**签署**：`deepseek-v4-flash` · 2026-11-18

---

## 5. 副作用记录

- 未修改任何源码/契约；仅新增 `.work/probe6/new.py`（gitignored）。
- 探针全部隔离到 `.work/probe6/tmp`（长期记忆、向量库、checkpoint、插件根与注册表），未触碰仓库 `.uh/`。
- 报告中不含任何令牌原文。
