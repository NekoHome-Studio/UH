# UH! 二次审查报告（修复后回归 · 模型署名版）

> **审查模型：Tencent Hunyuan Hy（腾讯混元 Hy）**
> 运行产品：WorkBuddy（Agent 模式）
> 审查时间：2026-11-17（22:11–22:25 GMT+8）
> 审查基线：`main` 分支 commit **`6fd06ee`**（入库 M1 审查材料 + ADR-003 进度）
> 对比基线：`41442bc`（上一轮审查对象）
> 报告性质：**非契约、非规范文件**；不修改 `docs/contracts/` 冻结事实源
> 关联前报：`docs/reviews/2026-11-17-审查与实测报告.md`、`docs/reviews/2026-11-17-审查报告-Tencent-Hunyuan-Hy.md`（均针对 `41442bc`）

---

## 0. 为什么有这份报告

上次审查后，仓库连续出现五个修复/治理提交：

| commit | 说明 |
|---|---|
| `ffff141` | WS 空 token 绕过、确认 TTL、dev 门禁、幂等、路径白名单、CLI 编码等（119/119 + 8 回归） |
| `2b1a6c1` | bus→task 事件桥、StorageGuard 接线、治理回写、CI 增 pytest + verify-all（120/120） |
| `9bd9ef6` | REST bearerAuth：本地豁免 + ops 豁免 + query token 回退（126/126） |
| `27e392a` | ADR-003 双模式 strict/legacy、记忆 scope global/session（129/129） |
| `6fd06ee` | 入库三份独立审查、模型对比与修复回应 |

**本报告全部实测均在 `6fd06ee` 上执行。** 上一轮报告只对 `41442bc` 有效。

结论先行：**测试数字变漂亮了（129/129、verify-all 8/8），原高危多数修好，但修复本身引入或留下了可绕过的缺口。**

---

## 1. 当前基线实测

### 1.1 门禁结果

```
"/c/Python314/python.exe" -m pytest -q
-> 129 passed, 98 warnings in 47.12s

PATH="/c/Python314:$PATH" node scripts/verify-all.mjs --full
-> VERIFY_ALL_PASSED (8 checks, full)
   A / B / B2 / C / D / F / G 全部 PASS，E 为 INFO
```

CI 也已补 pytest 与 verify-all，触发路径纳入 `kernel/**`、`tests/**`（`.github/workflows/contracts.yml:27-44`）。**门禁层面已修复。**

### 1.2 原问题复核矩阵

| 原问题 | 当前判定 | 证据 |
|---|---|---|
| WS 空 token 绕过 | **已修** | `main.py:577` 要求 `expected_token is not None`；动态复测首帧为 `UH-USR-1001 未认证` |
| WS 重订阅丢实时事件 | **已修** | `main.py:683-685` 重建 `queue_task`；动态复测收到 `delta 5`（大于回放上限 4） |
| 路径穿越 | **已修（主入口）** | `retrieval.py:190-194` resolve + `is_relative_to`；动态复测返回“文档路径越界”，marker 不在索引文档中 |
| 确认 TTL 死等 | **核心已修，有残留** | `confirmation.py:134-143` + `tasks.py:150` 自动超时；1.3s 后终态为 `failed`、future 释放；但 `confirm_timeout` 标志跨轮不重置 |
| REST 全局无鉴权 | **部分修复** | 中间件已加（`main.py:235-244`），但 `UH_LOCAL_MODE` 默认 `"1"` 导致只设 `UH_AUTH_REQUIRED=1` 仍全开；非 `/api/v1` 别名绕过 |
| dev 令牌泄露 | **部分修复** | 完整 token 已脱敏，但仍回显前 6 位；`dev_route` 漏 dev 门禁；`UH_DEV_MODE` 默认开 |
| CLI 中文 Windows 编码 | **已修** | `cli.py:121-124` 改为字节 + `errors="replace"`；129/129 含该用例 |
| CI 不跑 Python 门禁 | **已修** | `contracts.yml` 增加 pytest + verify-all；本地实测全绿 |

---

## 2. 新发现（按严重性）

### N1（阻塞）REST 鉴权默认豁免：`UH_AUTH_REQUIRED=1` 单独设置无效

`kernel/main.py:235`：

```python
auth_required = _truthy(os.environ.get("UH_AUTH_REQUIRED") or os.environ.get("UH_WS_AUTH_REQUIRED")) \
    and not _truthy(os.environ.get("UH_LOCAL_MODE", "1"))
```

`UH_LOCAL_MODE` 默认 `"1"`，而 WS 侧（`main.py:572`）读同一变量**没有默认值**。同一配置下 REST 全开、WS 开启鉴权。生产只设 `UH_AUTH_REQUIRED=1` 即中招。

**动态实测**：

```
UH_AUTH_REQUIRED=1, UH_LOCAL_MODE unset -> POST /api/v1/sessions: 201
```

**建议**：去掉 REST 的默认值，与 WS 对齐；或要求 `UH_AUTH_REQUIRED=1` 且未显式设 `UH_LOCAL_MODE=0` 时拒绝启动。

### N2（阻塞）非 `/api/v1` 别名端点绕过鉴权

`main.py:236` 只保护 `path.startswith("/api/v1/")`，但 `DELETE /memory/long-term`、`GET /memory/export`、`GET /memory/short-term`、`GET /plugins` 都有无前缀别名。

**动态实测**（`UH_LOCAL_MODE=0` + `UH_AUTH_REQUIRED=1` + token 已配）：

```
GET /api/v1/memory/export -> 401
GET /memory/export        -> 200
```

匿名调用者可经别名导出记忆或直接清库。上一轮 B5 只修了一半。

### N3（阻塞）MCP RPC 通道绕过高危确认门

`kernel/mcp.py:504`：

```python
if confirm is not None and risk in {"write-host", "irreversible"}:
```

确认门要求调用方显式传 `confirm`。但 `register_capabilities` 生成的 handler（`mcp.py:561-566`）调用 `call_tool` 时**没有 `confirm` 参数**，RPC 调用方也无法传入。

**动态实测**（manifest 把 `index_document` 标为 `irreversible`）：

```
requires_confirmation in bus event: [True]
RPC degraded: False
inline content indexed via RPC: True
```

总线事件明确声明“需要确认”，但 RPC 调用不经过确认就执行了工具。

### N4（高危）Host 头注入 `stream_url`

`main.py:459-461` 直接用请求 `Host` 头拼 WS URL：

```python
host = request.headers.get("host") or request.url.netloc
stream_url = ws_scheme + "://" + host + "/api/v1/stream?task_id=" + task_id
```

**动态实测**：

```
stream_url: ws://evil.example/api/v1/stream?task_id=uh_task_e157...
```

客户端会连到攻击者主机，并可能在 `op:auth` 帧中发送 token。应使用服务端可信配置。

### N5（高危）幂等回放泄漏首次调用者 Cookie

`main.py:245-260` 的 cache_key 绑定 method/path/Idempotency-Key/body hash，但缓存的 `headers` 原样保存，包含 `Set-Cookie: uh_client_id=...`（`main.py:456`）；cache_key 不含 client/session/token。

**动态实测**：

```
first set-cookie:  uh_client_id=victim; HttpOnly; Path=/; SameSite=lax
second set-cookie: uh_client_id=victim; HttpOnly; Path=/; SameSite=lax
```

第二个客户端拿到 victim 的 client id，结合 owner 回退可订阅对方任务。另：并发同键请求仍是“先查→执行→后写”，无 in-flight 锁，两个请求都会真实执行。

### N6（高危）`dev_route` 漏 dev 门禁，`UH_DEV_MODE` 默认开

`main.py:413-425` 的 `/api/v1/dev/route` 没有 `_dev_enabled()` 检查；`UH_DEV_MODE` 默认 `"1"`。

**动态实测**（`UH_DEV_MODE=0`）：

```
POST /api/v1/dev/route        -> 200
GET  /api/v1/dev/confirmations -> 404（此处门禁生效）
```

`/dev/confirmations` 虽已脱敏完整 token，但仍回显前 6 位。

### N7（中高）StorageGuard 接线是空操作（修正上一轮表述）

上一轮我写“StorageGuard 无 check 调用点”，**表述不准确**。当前 `protocol.py:100-103` 有 `authorize()`，`:141` 也确实调用。但：

```python
def authorize(self, module):
    for ns in self._owned.get(module, set()):
        self.check(module, ns)      # check 判断 ns in self._owned[module] -> 恒真
```

它只遍历自己已声明的命名空间再检查同一个集合，`call()` 从不传入 handler 实际要访问的目标命名空间。因此仍是**无实际隔离效果**，只是从“没有调用”变成“调用了恒真检查”。

### N8（中高）WS `op:auth` 换 token 后不重校归属

`main.py:647-660`：已订阅任务后，客户端再发 `op:auth` 带另一个合法 token，代码更新 `token` 与 `authenticated`，但只在 `subscriber is None` 时才 `attach`。结果：用 token B 的连接继续接收任务 A 的事件。

### N9（中）确认超时标志跨轮不重置

`tasks.py:154,158,168`：`t["confirm_timeout"] = True` 后从不清除。同一任务第一轮超时、第二轮人工拒绝，仍记为 `resolved_by="timeout"` / `reason="timeout"`，污染审计。

### N10（中）ADR-003 双模式实现不一致

- ADR 称插件可经 manifest/上下文覆盖模式（`ADR-003-dual-mode-config.md:14`），但 `module_error_code()`（`protocol.py:79-84`）只读环境变量，覆盖未实现。
- `event_mapping.py:169` 的 step.failed 兜底仍硬编码 CAP-4003，应为新增的 CAP-4004。
- `errors.md:35` 定义 4004 `retryable=true`，但 `mcp.py:323,372` 用 `module_error_code()` 配 `retryable=False`。

### N11（中）deadline 分层回归

`tasks.py:326-332` 把 `timeout_ms = deadline_ms`，节点级与任务级都用 120s；`orchestration.py:343` 的 `UH_ORCHESTRATION_TIMEOUT_MS` 实际被忽略。四节点仍可能在前三个耗满后被任务级 `wait_for` 杀掉，`step.started` 无配对终态。

### N12（中）长期记忆跨会话默认仍开启

`tasks.py:521-523` 默认 `UH_MEMORY_SCOPE=global` → `session_id=None` → 不过滤（`long_term_memory.py:261,280-281`）。A 会话记忆仍注入 B 会话；请求方也可用 `context.memory_scope="global"` 覆盖管理员设置的 `session`。

### N13（中）旧实现问题仍未动

| 问题 | 本轮证据 |
|---|---|
| `get_context(limit=0)` 返回全部 | 存 3 条，`limit=0` 返回 3 条（`memory.py:38` 的 `limit or window`） |
| checkpoint_id 每次保存重新生成 | 6 次保存、5 个不同 ID（`orchestration.py:268,279,290,300` 等） |
| 配置 PostgreSQL 却静默用 SQLite | 设 `UH_CHECKPOINT_POSTGRES_URL` 后 `store class: SQLiteCheckpointStore`（`orchestration.py:181-184`） |
| 嵌套 trace 上下文清除外层 | `outer -> inner -> None`（`tracing.py:48-49` 用 `clear()` 而非 `reset(token)`） |
| MCP stdio stderr 管道不消费 | `mcp.py:149`，静态 |
| 单条坏 L2 密文使整体召回/导出崩溃 | `long_term_memory.py:272,308`，静态 |
| 日志脱敏键名不全 | `mcp.py:405`，静态 |

### N14（中）UI 确认流程与重连仍缺失（静态）

- `ui/index.html:98-116` 无 `task.confirmation_required` 分支，高危工具任务必然挂起等超时；契约要求的确认弹窗/批准拒绝按钮未实现。
- `:105-107,119` 收到 `replay_unavailable` / 4408 只 `fetch` 不渲染，`/steps` 回退结果被丢弃。
- `:96-123` `lastEventId` 更新但断线不重连、不按游标恢复。
- `:121` 把 4401 误报成 `UH-KRN-5001 事件流已断开`，认证失败伪装成系统故障。
- `renderState`（`:68-70`）不读 `uiState.degraded`，契约 C10 徽标未落地。
- 两处 `fetch` 不检查 `resp.ok`，`JSON.parse` 无 try/catch，`payload` 空值未防。

---

## 3. 修复质量总评

| 维度 | 评价 |
|---|---|
| 测试数量 | 从 110/111 提升到 129/129，verify-all full 8/8，门禁已真实生效 |
| 安全修复 | 空 token、TTL、路径白名单、CLI 编码是实修；REST 鉴权、dev 门禁、令牌脱敏只修到一半 |
| 新引入风险 | Host 头注入、幂等 Cookie 泄露、REST 默认豁免、别名绕过、RPC 确认绕过 |
| 治理 | 契约状态已回写 frozen，但 ADR-003 与实现、PROGRESS/AGENTS/README 仍互相矛盾 |
| 测试质量 | 新增用例覆盖了修复函数本身，但未覆盖绕过路径（别名、并发同键、dev_route 漏门、RPC 确认、重订阅实时事件） |

---

## 4. 建议优先级

1. **立即**：REST 鉴权默认值与 WS 对齐；保护所有别名端点；MCP RPC 确认门下沉为强制；移除 Host 头拼接。
2. **高**：幂等缓存剔除 Set-Cookie 并绑定调用者；`dev_route` 补门禁、dev 默认关闭；WS auth 换 token 重校归属。
3. **中**：StorageGuard 改为真实目标命名空间校验；清 `confirm_timeout`；ADR-003 覆盖与 retryable 对齐；deadline 分层；记忆 scope 默认 session；UI 确认/重连/回退。
4. **低**：`limit=0`、checkpoint ID 稳定性、PG 配置告警、嵌套 trace token reset、MCP stderr、L2 坏记录隔离、日志脱敏键名。

---

## 5. 审查声明

1. 本报告全部命令与复现均在 commit **`6fd06ee`** 上执行；审查期间 HEAD 未变，工作区干净。
2. 报告由 **Tencent Hunyuan Hy（腾讯混元 Hy）** 在 WorkBuddy Agent 模式下生成，模型型号按要求记录。
3. 未修改任何受版本控制源码；只新增本报告文件。
4. 上一轮报告中“StorageGuard 无调用点”的表述在本报告中已修正为“有调用但恒真空操作”。
5. 复现脚本位于系统临时目录，审查结束后删除；关键输出已内嵌本报告。

**报告结束。基线：6fd06ee。审查模型：Tencent Hunyuan Hy。**
