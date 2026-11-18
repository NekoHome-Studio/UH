# UH! M1 四审跨机验收报告（独立第三方 · 跨机实测）

> **审查模型：Tencent Hunyuan Hy（腾讯混元 Hy）**
> 运行产品：WorkBuddy（Agent 模式）
> 审查日期：2026-11-18（19:56–20:35 GMT+8）
> 验收机：192.168.0.14（本机，Windows）· 目标机：192.168.0.28:8080（DESKTOP-AOI7835，`ssh ipc`，`D:\uh`）
> 审查基线：`main` HEAD **`d23d027`**（`origin/main` 同步，0/0），工作区干净（仅有他人新增的未跟踪报告）
> 口径来源：`briefs/0010-m1-third-review-acceptance.md`（9 项生产链路）+ `docs/reviews/2026-11-18-跨机验收指南.md` + `docs/runbooks/环境状态总览.md`
> **独立性声明**：未阅读 GPT-6-Codex / DeepSeek-V4-Flash 的四审报告；结论全部由「当前代码 + 冻结契约口径 + 跨机真实 HTTP/WS 实测」独立得出。
> 报告性质：**非契约、非规范文件**；不修改 `docs/contracts/`，不修改任何源码/契约，只新增本报告。
> 关联前报：`2026-11-17-审查与实测报告.md`、`2026-11-17-二次审查-修复后回归-Tencent-Hunyuan-Hy.md`、`2026-11-18-M1-三审验收报告-Tencent-Hunyuan-Hy.md`

---

## 0. 结论摘要

**验收结论：不通过（P0 阻断，修复后须重验）**。

门禁与功能链路本身是达标的：本机 **148/148**、verify-all full **8/8**、双端核心文件 SHA256 一致、9 项跨机生产链路中 8 项通过。但跨机视角暴露了本地 `TestClient` 无法发现的两类确定性问题：

1. **生产鉴权凭据已泄露且仍有效**：明文 token 随 `d23d027` 提交并已推送 `origin/main`；服务器访问日志、错误日志、WMI 常驻进程命令行也全部明文留存。实测用文档里的 token 可直接 201 创建会话。
2. **跨客户端可越权确认高危工具**：SSE 任务流缺失归属守卫，泄露完整确认令牌；`owner_matches` 在持 token 时忽略 client_id，导致所有任务级守卫在单共享 token 下失效。已在真实跨机环境跑通 6 步完整链（读他人任务 → 偷令牌 → 批准/拒绝 → 任务被改态）。

其余为 P1/P2：dev 端点在生产模式下可远程关能力、WS 事件风暴（单任务 8188 帧）、生产模式幂等 caller 漂移、服务器多实例残留、明文 HTTP/WS、控制帧在事件洪水下滞后等。

**一句话**：功能层可以进 M2，但当前这版**不能按"生产鉴权模式 B"对外放行**。

---

## 1. 基线与双端环境（亲测）

### 1.1 提交与工作区

```
git status -sb                  -> ## main...origin/main
git rev-parse --short HEAD      -> d23d027
git log --oneline 6fd06ee..HEAD -> 11 个提交（跨机修复、UI 鉴权、三审收尾、环境总览等）
git branch -r --contains d23d027 -> origin/main（该提交已推远端）
```

工作区有两份他人未跟踪的四审报告，以及一份被外部修改的二审报告（非本次操作）；本次未改动它们。

### 1.2 本机门禁

| 命令 | 实测结果 |
|---|---|
| `C:\Python314\python.exe -m pytest -q -p no:cacheprovider`（`PYTEST_DEBUG_TEMPROOT` 指向独立临时根） | **148 passed, 118 warnings in 36.30s** |
| `PATH=/c/Python314:$PATH node scripts/verify-all.mjs --full` | **VERIFY_ALL_PASSED (8 checks, full)**（A/B/B2/C/D/F/G PASS，E 为 INFO） |

> 环境说明：默认 `python` 仍是 WorkBuddy 3.13（无 pytest），门禁脚本硬编码 `python`；本机实测仍需显式指定 `C:\Python314`（与一审结论一致，属长期环境事实）。首次直接跑 pytest 时，沙箱拦截了 pytest 清理历史临时目录的动作，改用独立 `PYTEST_DEBUG_TEMPROOT` 后正常通过，非测试失败。

### 1.3 服务器实测状态

| 项 | 实测 |
|---|---|
| 连通 | `health` 200 `{"status":"ok","version":"0.1.0"}`、`ready` 200、`openapi.json` 200；无凭据 `POST /api/v1/sessions` 401 |
| 端口 | `TCP 0.0.0.0:8080 LISTENING PID 1900` |
| 运行模式 | `UH_LOCAL_MODE=0`、`UH_DEV_MODE=1`、`UH_AUTH_REQUIRED=1`、`UH_AUTH_TOKEN=<明文>`、`UH_PUBLIC_BASE_URL=http://192.168.0.28:8080`（来自 `D:\uh\start-uh.ps1` 的 `set` 链） |
| LLM 后端 | 无 `UH_LLM_API_KEY` → `MockLLMClient`（`kernel/llm.py:160-178`，逐字符 yield + `asyncio.sleep(0.005)`） |
| 部署一致性 | 本机 vs 服务器 `kernel/main.py`、`kernel/tasks.py`、`kernel/mcp.py`、`ui/index.html` 四个 SHA256 **完全一致**（`56d27b22…` / `44fda70e…` / `a87c49ae…` / `5ee17c71…`） |
| 进程 | 3 个内核相关进程：`PID 9460` `D:\Python312\python.exe main.py`（18:01 残留）、`PID 9948` venv uvicorn、`PID 1900` `D:\Python312\python.exe -m uvicorn`（19:41，**实际监听者**） |

---

## 2. 实测方法

全部探针写在本机系统临时目录（`%TEMP%\uh4_probe_*.py`），从 **192.168.0.14 经真实 TCP/HTTP/WS 发往 192.168.0.28:8080**，不使用 `TestClient` 作为验收依据（仅 RPC fail-closed 因跨机无对应端点，用本机代码级补测并明确标注）。

| 组 | 覆盖 |
|---|---|
| **A** | 鉴权矩阵（无/错/空/畸形凭据、8 业务端点含别名、8 豁免端点）、401 envelope+trace、10 种路径规范化/编码绕过、bearer 与 query 优先级、Host 注入、响应头/Cookie/CORS、metrics/openapi 暴露面、dev 端点 |
| **B** | 中文消息 + WS 实时流 event_id 连续性、after_event_id 重连、无/错 token WS、跨客户端归属链（任务详情/steps/SSE/WS/confirm）、未知任务 SSE |
| **C** | 确认 approved/rejected/重复、dev 令牌脱敏、取消闭合与 409、终态 resume 409、steps 全量、幂等同/跨客户端/并发、dev 能力开关破坏+恢复、会话存在性、中文/emoji/大 payload/时钟差 |
| **D** | 幂等 caller 漂移 4 组合专项、dev 脱敏正确 URL、WS 完整到终态（不截断）、跨机会话注入与全量记忆导出、超窗/未来游标 |
| **E** | 长任务终态核实、事件样本、最短输入事件规模、残留任务清理、非法游标 |
| **F / F2 / F3** | WS `op:auth` / `op:subscribe` 精确复测、RPC fail-closed（直接调用 + registry 路径 + bus 事件配对） |
| **G / H** | 控制帧时序对照：低事件量（`pending_confirmation`）vs 事件洪水下的 `ping` / `auth` / `subscribe` |

探针副作用：创建了若干独立会话与任务（含 4 个挂起确认任务，均已 `rejected` 清理）、导出了记忆（只读）、`dev/capability/mcp.tool.search` 曾被置 `false` 后**已恢复 `true`**；未执行 `DELETE /memory/long-term`，未改服务器文件/配置，未重启服务。临时探针脚本收尾时删除。

---

## 3. 9 项跨机生产链路逐项判定

| # | 项目 | 判定 | 关键实测证据 |
|---|---|---|---|
| 1 | **鉴权（模式 B）** | ✅ 通过 | 无凭据 8 个业务端点（含 `/memory/export`、`/memory/short-term`、`/plugins`、`/api/v1/dev/*` 别名）全 401 且带 `meta.trace_id`/`X-Trace-Id`；`/health` `/ready` `/metrics` `/` `/static/state-machine.js` `/docs` `/openapi.json` `/redoc` 全 200；错/空/畸形凭据 401；10 种路径变体（大小写、`//`、`.`、`;`、尾斜杠、URL 编码、`..`）**全部 401 或 404，无一 2xx**；401 不回显凭据 |
| 2 | **会话存在性** | ✅ 通过 | 未知会话 `POST /messages` → 404 `UH-USR-1003`；未知任务 → 404 `UH-USR-1003` |
| 3 | **WS 实时流 / 重连** | ✅ 通过（有滞后项，见 P2-8） | 中文消息收 `task.created`→`step.started`→`delta`→终态，`event_id` 严格连续（1..200、1..5119、1..8188）；`after_event_id=3` 重连从 4 开始；终态重连回终态；错 token 首帧 `UH-USR-1001` + 4401；`Host: evil.example` 下 `stream_url` 仍为 `ws://192.168.0.28:8080`（Host 注入已修） |
| 4 | **确认 inline** | ✅ 通过 | `approved`→`finished`、`rejected`→`cancelled`、重复 confirm→400 `UH-USR-1003`；`dev/confirmations` 正确 URL 返回 `confirmation_token: "[redacted]"`（全脱敏，无前缀），审计 10 条；超时终态三审已验（TTL 固定 300s，本次跨机未等 5 分钟） |
| 5 | **确认 RPC fail-closed** | ✅ 通过（本机补测） | 跨机无直接 RPC 端点；本机 `MCPHost.call_tool(risk_level="irreversible")` → `UH-USR-1002 高危工具须经确认通道`；经 `CapabilityRegistry.call` → `degraded=true, error_code=UH-USR-1002`；bus 事件配对 `mcp.tool.call(requires_confirmation=true)` + `mcp.tool.result(status=denied)`，工具未执行 |
| 6 | **取消 / 409** | ✅ 通过 | `cancel` → `cancelling` → `cancelled`，`step.failed` 闭合；二次 cancel 409；终态 `resume` 409（三审残留已修）；`/steps` 含 `task_id` 与 `duration_ms` |
| 7 | **超窗回退 /steps** | ⚠️ 部分 | `/steps` 全量阶段 ✅；**4408 未动态复现**：跨机无法在不压垮服务器的前提下构造 >5min 运行中任务（终态任务 `replay_available` 恒 True，见 `tasks.py:475-486`）；`after_event_id=999999` 静默不回放也不报错（P2-9）；负/非数字游标 → 4404 |
| 8 | **隔离 / 隐私** | ❌ 不通过 | 并发同键去重 ✅（4 并发仅 1 个 task）、回放剔除 `Set-Cookie` ✅；但**跨客户端隔离失效**：attacker 可读他人任务/步骤、SSE 偷完整确认令牌、订阅他人 WS、用偷来的令牌改他人任务终态、往他人 session 发消息、导出 105KB 全量记忆（详见 P0-2）；生产模式幂等 caller 漂移（P1-5） |
| 9 | **UI** | ✅ 通过（静态） | 12 项静态检查全通过：token 输入框 / localStorage / URL 参数 / `withToken` fetch / WS query token / 确认弹窗 / 4408 / 4401 / degraded 徽标 / `aria-live` / 重连退避；`innerHTML` 仅用于静态骨架与 `escapeHtml` 转义后的确认弹窗（`ui/index.html:85,117`），delta 用 `textContent +=`（`:139`），无 XSS 注入点；**未做浏览器真机点击** |

---

## 4. 新发现（按严重性，全部带实测证据）

### P0-1（阻断）生产鉴权凭据明文入仓、已推远端，且服务器四处留痕

**证据链**：

1. `docs/runbooks/环境状态总览.md:55` 明文写 `UH_AUTH_TOKEN=MQ5s…`（43 字符）；`git grep` 确认它是**唯一含完整 token 的受版本控制文件**；`git log -S` 确认由 `d23d027` 引入；`git branch -r --contains d23d027` → **`origin/main` 已包含**，即凭据已推送到 `github.com/NekoHome-Studio/UH`。
2. 实测有效性（本机发往 .28:8080）：
   ```
   no token            -> 401 UH-USR-1001
   doc token bearer    -> 201 {"data":{"session_id":"uh_ses_44b25e…","status":"active"}}
   wrong token         -> 401
   doc token query     -> 201
   ```
   文档中的字符串与 `.work/server-auth-token.txt` **完全一致**，是当前生产环境的有效凭据。
3. `.work/start-uh-prod.ps1:4` 与服务器 `D:\uh\start-uh.ps1` 的 `set` 链明文注入同一 token（`.work/` 被 gitignore，但服务器文件明文）。
4. 服务器日志：`findstr /C:"token=" D:\uh\kernel.out.log | find /C` → **113 条**完整 `?token=…` URL；`kernel.err.log` → **32 条**（含 WS 握手 URL）。示例：`INFO: 192.168.0.14:58219 - "POST /api/v1/sessions?token=MQ5s… HTTP/1.1" 201 Created`。
5. WMI 常驻进程命令行明文：
   ```
   CommandLine=cmd.exe /c "cd /d D:\uh && set "UH_LOCAL_MODE=0" && … && set "UH_AUTH_TOKEN=MQ5s…" && …"
   ```
   任何能查询 `Win32_Process` 的同机用户都可读取。

**影响**：直接违反 `AGENTS.md:4`「密钥一律经环境变量/系统钥匙串引用，严禁硬编码进代码、测试、日志、仓库」；拿到仓库、日志、进程列表或局域网流量的任一方都可完全控制内核（建会话、关能力、读全量记忆、批准高危工具）。这不是配置建议问题，是已发生且可验证的凭据泄露事件。

**建议**：① 立即轮换 token（新旧同时失效）；② 清理 git 历史中的该字符串并 force-push 前评估协作影响；③ 启动脚本改从环境变量/凭据管理器读取，禁止写进进程命令行；④ uvicorn 关闭 access log 或对 `token`/`Authorization` 脱敏；⑤ 生产改用 Authorization 头 + TLS，WS 至少避免把 token 写进可被日志捕获的 URL；⑥ 环境总览改为占位符 `UH_AUTH_TOKEN=<从钥匙串读取>`。

> 本报告全文对该 token 一律脱敏，不复现完整值。

### P0-2（阻断）跨机跨客户端越权链：SSE 泄露完整确认令牌 + 归属守卫集体失效

**6 步真实跨机复现**（victim `X-UH-Client-ID: victim_uh4`，attacker `attacker_uh4`，两者持有同一共享 token）：

```
1) victim  POST /api/v1/dev/tool-task (irreversible)  -> 202, task pending_confirmation
2) attacker GET  /api/v1/tasks/{tid}                  -> 200  （期望 403）
   attacker GET  /api/v1/tasks/{tid}/steps            -> 200  （期望 403）
3) attacker GET  /api/v1/tasks/{tid}/stream (SSE)     -> 200，事件序列
      task.created / tool.call / task.confirmation_required
      payload.confirmation_token = "T4Qn_vrl8C…"（完整令牌，非脱敏）
4) attacker WS   /api/v1/stream?task_id={tid}&client_id=attacker_uh4 -> 成功收到 3 帧，无 4403
5) attacker POST /api/v1/tasks/{tid}/confirm {stolen_token, rejected} -> 200
      {"decision":"rejected","operator":"attacker_uh4"}
6) victim  GET   /api/v1/tasks/{tid}                  -> "cancelled"
```

**根因（三层）**：

- `kernel/tasks.py:427-435` `owner_matches()`：
  ```python
  if owner_token:
      return bool(token and secrets.compare_digest(token, owner_token))
  return not owner_client_id or client_id == owner_client_id
  ```
  只要任务创建时带了 token（生产模式必然），**client_id 分支根本不会执行**。单共享 token 模型下所有任务归属比对恒为真。
- `kernel/main.py:503-512` `_task_guard()` 因此形同虚设，虽然 `/tasks/{id}`、`/resume`、`/confirm`、`/steps`、`/cancel` 五个端点都挂了它。
- **`kernel/main.py:608-616` 的 SSE `/api/v1/tasks/{task_id}/stream` 是唯一没有 `_task_guard` 的任务端点**（grep 调用点：517/527/541/555/565 有，608 无）。而 `task.confirmation_required` 事件 payload 里带完整确认令牌，于是 `kernel/main.py:587-599` 对 `dev/confirmations` 做的 `[redacted]` 全脱敏被 SSE 完全旁路。

**旁证**：`POST /api/v1/sessions/{victim_sid}/messages` 用 attacker 身份 → 202（会话无归属校验）；`GET /memory/export` → 200、105407 字节全量记忆。

**与 brief 的关系**：brief 0010 把「REST 完全多用户隔离需 per-user principal」列为**已知设计局限，勿当 blocker**。但本项不是纯设计局限：

- SSE 缺 guard 是**实现缺口**（同一文件的其他 5 个端点都有，将来引入 per-user principal 时它仍会漏）；
- 确认令牌从「仅经任务自有事件流送达归属者」的设计，变成「任何持共享 token 者可读取」，使高危工具确认形同虚设；
- 已动态证明可改变他人任务终态，构成实际可利用链。

**建议**：① SSE 端点补 `_task_guard`；② 确认令牌改由带外通道下发或在 SSE 中对非归属者脱敏；③ `owner_matches` 改为「token 与 client_id 双因子」，并在共享 token 模式下用 cookie/签名 client_id 做强绑定；④ 生产放行前引入 per-user principal 或明确声明「单用户设备、所有持 token 者等价」，并在契约中写明。

### P1-3（高危）生产鉴权模式下 dev 端点可远程关闭能力

`UH_DEV_MODE=1` 是验收期配置（环境总览自注「正式生产应设 0」）。实测持共享 token 即可：

```
POST /api/v1/dev/route                                   -> 200
GET  /api/v1/dev/confirmations                          -> 200（全量审计）
POST /api/v1/dev/capability/mcp.tool.search/toggle?available=false -> 200 {"available":false}
POST …?available=true（恢复）                            -> 200 {"available":true}
```

`kernel/main.py:601-606` 的 toggle 无角色区分、无二次确认、无审计告警，可造成能力级拒绝服务。**已恢复，无残留**。建议：正式生产 `UH_DEV_MODE=0`；dev 端点独立 ops token；toggle 至少写审计。

### P1-4（中高）WS 事件风暴与终态缓冲不回收

一个普通中文短消息在跨机 WS 上实测：

```
D3: 45 秒收到 5119 帧（task.created 1 + step.started 1 + delta 5117），尚未终态
E2: 该任务最终 event_id 到 8188，task.finished
E3: 最短输入 "hi" -> 126 帧 / 0.3s 终态
```

根因：`kernel/llm.py:172-178` MockLLM 逐字符 yield（每字符 `sleep(0.005)`），`kernel/tasks.py:303-305,345-346` 每个字符发一条 `delta` 事件；跨机逐帧传输约 113 帧/秒，服务端 0.x 秒完成的任务客户端要 60+ 秒收完。`kernel/tasks.py:255-258` 的 event_buffer 仅在 `status == "running"` 时按 5 分钟裁剪，**终态后永不裁剪**，且 `tasks` dict 无回收路径 → 长跑内存单调增长。

建议：delta 合批（如 50ms 窗口或按 token 片段）、終态任务事件持久化后释放内存、WS 背压与帧率上限、客户端渲染节流。

### P1-5（中高）生产模式幂等 caller 漂移：同键同 body 重复执行

`kernel/main.py:251-255` 的 `caller` 依次取 `cookie → X-UH-Client-ID → query token`，而 `send_message`（`:472-500`）首请求会 `set_cookie("uh_client_id", client_id)` 改写 cookie。实测 4 组（每组全新 client、同 key、同 body）：

```
fresh + header clientA x2          -> same_task=False（两个任务）
fresh + 无 header x2               -> same_task=True ✅
fresh + header 后再 cookie-only    -> same_task=False
预置旧 cookie + header clientB x2  -> same_task=False
```

即：**只要客户端显式带 `X-UH-Client-ID` 且与已有 cookie 不一致，首次与重试必然落到不同 cache_key，幂等失效**，可能重复建任务/重复写记忆。三审只验了「跨客户端不复用 + 并发去重」，未覆盖该组合。建议 caller 只取稳定的认证主体，或首次请求就把 cookie 与 header 归一，并对同一 key 在 in-flight 阶段统一占位。

### P2-6（中）服务器多实例残留与实际运行解释器偏离文档

环境总览称 venv `D:\uh\.venv`（Python 3.12.10）单实例；实测：

```
PID 9460  D:\Python312\python.exe main.py          CreationDate 18:01（残留，非 uvicorn）
PID 9948  D:\uh\.venv\Scripts\python.exe -m uvicorn …   19:41
PID 1900  D:\Python312\python.exe -m uvicorn …          19:41 ← 8080 实际监听者
```

`start-uh.ps1` 的清理条件 `CommandLine -like '*uvicorn*kernel.main*'` 不会杀掉 `python.exe main.py` 残留；多实例同时启动存在端口竞争风险，且实际监听进程并非文档所述 venv。建议重启前按端口占用精确清理、统一解释器、启动后校验 PID 与 venv 路径。

### P2-7（中）明文 HTTP/WS + 凭据进入 URL / localStorage

跨机链路全程 `http://` / `ws://`（无 TLS）；UI 静态检查确认 token 存入 `localStorage`、拼进 WS query；`Set-Cookie: uh_client_id=…; HttpOnly; Path=/; SameSite=lax` **无 Secure**；响应无 `Strict-Transport-Security` / `X-Frame-Options` / `Content-Security-Policy` / `X-Content-Type-Options`（`Access-Control-Allow-Origin` 未设置，同源默认）。局域网内可嗅探完整 token；结合 P0-1 的日志留痕，暴露面叠加。属已知设计局限，但在跨机部署下应作为放行前条件（至少 TLS + 日志脱敏）。

### P2-8（中）控制帧与事件洪水竞争，响应滞后

对照实测（同一代码、同一跨机链路）：

```
低事件量（pending_confirmation）：ping -> 立即 pong；auth wrong -> error UH-USR-1001 / 4401 ✅
事件洪水（chat）：ping 的 pong 出现在第 14 帧
事件洪水 + auth wrong：收 49 帧到 task.finished、连接以 1000 正常关闭，全程未收到 4401
事件洪水 + op:subscribe：低事件量下切换到另一任务成功（t2 3 帧、t1 0 帧）
```

`kernel/main.py:700-770` 的 `asyncio.wait(FIRST_COMPLETED)` 循环在高频 `queue_task` 下，控制帧处理被明显延后。**鉴权逻辑本身正确**（H1 证实），不构成稳定绕过；但在任务快速终态时，恶意/错误的 `op:auth` 可能来不及被拒绝，且 `ping`/`subscribe` 响应不可预期。建议控制帧优先队列、delta 合批（P1-4）、或将事件发送与控制帧接收拆分为独立任务。

### P2-9（中低）未来游标静默、超窗 4408 缺乏跨机可复现路径

`after_event_id=999999` → 不回放、不报错、继续推实时事件（首帧 `delta id=26`）；负/非数字 → 4404 `UH-USR-1003`。`subscribe()` 在 `after_event_id >= latest_id` 时返回空回放（`tasks.py:463-465`），`replay_available()` 对终态任务恒 True（`:475-486`），因此 4408 只在「运行中且事件早于 5 分钟窗口」时触发，**跨机未能在不制造 >5 分钟长任务的前提下动态复现**（三审为本地构造）。建议未来游标返回显式提示，并在 CI 中用可控时钟补 4408 集成用例。

### P3-10（低）凭据优先级与解析宽松

- `bearer 错误 + query 正确 → 401`（`_bearer_token(Authorization) or query` 的短路优先级）：若代理/CDN 自动注入 Authorization 头，合法 query token 会被误拒。
- `Authorization: <裸 token>`、`bearer <token>`（小写）、`Bearer  <token>`（双空格）**均 201**：`_bearer_token` 解析过宽。仍需正确 token，不构成越权，但与 OpenAPI `bearerAuth` 口径不一致。

### P3-11（低）内部端点进入公开契约 / 运维文档口径

`/openapi.json` 含 `dev/tool-task`（而 `dev/route` 用 `include_in_schema=False` 隐藏），与 `kernel/main.py:440` 注释「不在公开 OpenAPI 契约内」不一致；`/metrics` 仅 `# TYPE uh_memory_short_term_keys gauge`，无敏感信息；环境总览称 148/148 与双端一致属实，但「venv 单实例」「正式生产应设 UH_DEV_MODE=0」与实测运行态有偏差（P2-6 / P1-3）。

---

## 5. 与一/二/三审发现的对照

| 前轮问题 | 四审跨机判定 |
|---|---|
| WS 空 token 绕过 | ✅ 已修（错 token 4401，未配期望值不放行） |
| REST 全局无鉴权 / 别名绕过 | ✅ 已修（8 业务端点含别名全 401；10 种路径变体无绕过） |
| Host 头注入 `stream_url` | ✅ 已修（可信 `UH_PUBLIC_BASE_URL`） |
| dev 确认令牌泄露 | ⚠️ **部分**：dev 端点全脱敏，但 SSE 旁路（P0-2） |
| 确认 TTL 死等 | ✅ 已修（三审本机验过，跨机 approved/rejected 终态正确） |
| WS 重订阅丢实时事件 | ✅ 已修（跨机 `op:subscribe` 切换成功） |
| 路径穿越 | ✅ 已修（二审起主入口白名单） |
| 幂等 Cookie 泄漏 | ✅ 已修（回放剔除 `Set-Cookie`）；但新增 caller 漂移（P1-5） |
| 终态 resume 404 | ✅ 已修（跨机 409） |
| CI 不跑 Python 门禁 | ✅ 已修（本机 148/148 + full 8/8） |
| CLI 中文 Windows 编码 | ✅ 已修（148/148 含该用例） |
| per-user 隔离缺失（设计局限） | ❌ **四审升级为实际可利用链**（P0-2） |
| 凭据硬编码入仓 | ❌ **四审新发现，且已推远端**（P0-1） |

---

## 6. 探针副作用与清理记录

- 服务器新增：约 30 个独立会话与任务（含 4 个 `pending_confirmation`），均已通过 `rejected` 清理或自然终态；1 个 8188 帧长任务已 `finished`（`cancel` 返回 409 属正常）。
- `dev/capability/mcp.tool.search` 曾 `available=false`，**已恢复 `available=true` 并验证**。
- 只读导出过 `/memory/export`（105KB）；**未执行 `DELETE /memory/long-term`**，未清库、未重启服务、未修改服务器任何文件。
- 跨机探针使 `kernel.out.log` / `kernel.err.log` 新增若干含 `?token=` 的访问记录（P0-1 已统计并作为证据，非本次引入的新风险类型）。
- 本机临时探针 `uh4_probe_*.py` 收尾删除；报告之外未新增文件。

---

## 7. 验收结论与放行前清单

**结论：不通过（P0 阻断，修复后须重验）· 签署：Tencent Hunyuan Hy（腾讯混元 Hy）**

通过的部分应予肯定：门禁 148/148 + 8/8、双端部署哈希一致、鉴权矩阵与路径规范化无绕过、确认 inline/RPC、取消/409、steps、Host 注入修复、UI 12 项静态检查、中文/emoji/大 payload/时钟差（约 5 秒，含请求耗时）均正常。

**放行前必修（P0）**

1. 轮换生产 token；清理 git 历史与远端；启动脚本/文档/日志/进程命令行全部去明文（P0-1）。
2. SSE 补归属守卫、确认令牌不再经非归属事件流明文下发、`owner_matches` 双因子化（P0-2）。
3. 明确「共享 token = 单用户等价」还是引入 per-user principal，并写入契约与部署文档（P0-2 的设计前提）。

**建议同批修复（P1/P2）**：生产关 `UH_DEV_MODE`；delta 合批 + 终态缓冲回收；幂等 caller 归一化；服务器进程清理与解释器统一；TLS + 日志脱敏 + 安全头；控制帧优先；未来游标提示与 4408 集成用例。

**重验口径**：上述 P0 修复后，至少重跑本报告 A/B/D/H 四组跨机探针 + `pytest -q` + `verify-all.mjs --full`，并新增「SSE 非归属 403」「确认令牌不在非归属流中出现」「日志无完整 token」三条回归。

---

## 附录 A：关键原始输出（脱敏）

```
# 门禁
148 passed, 118 warnings in 36.30s
VERIFY_ALL_PASSED (8 checks, full)

# 凭据有效性
no token -> 401 ; doc token bearer -> 201 ; wrong token -> 401 ; doc token query -> 201
doc token == .work/server-auth-token.txt : True
git branch -r --contains d23d027 : origin/main
kernel.out.log "token=" 行数 : 113 ; kernel.err.log : 32

# 越权链
attacker get_task: 200 ; attacker steps: 200
attacker SSE leaked full confirmation_token: True (prefix T4Qn_vrl8C…)
attacker WS frames=3 (task.created / tool.call / task.confirmation_required)
attacker confirm with stolen token: 200 {"decision":"rejected","operator":"attacker_uh4"}
victim task status after attacker reject: cancelled

# 事件风暴
D3 frames=5119 max_event_id=5119 continuous=True terminal=None elapsed=45.0s
E2 samples=[{'id': 8188, 'event': 'task.finished'}]
E3 short input "hi": frames=126 terminal=task.finished elapsed=0.3s

# 幂等
fresh+header x2 -> same_task=False ; fresh no header x2 -> True
concurrent same key -> distinct_tasks=1 ; cross client cookie -> None

# RPC fail-closed（本机）
direct high-risk call denied: UH-USR-1002
registry RPC degraded=True code=UH-USR-1002
bus events=[('mcp.tool.call', None, True), ('mcp.tool.result', 'denied', None)]

# 控制帧对照
quiet ping -> pong ; quiet auth wrong -> error UH-USR-1001
flood ping -> pong_at=13 ; flood auth wrong -> 49 帧到终态未收到 4401，close 1000
```

## 附录 B：未覆盖范围（诚实标注）

- **4408 超窗回退**：跨机未动态复现（需构造 >5 分钟运行中任务，或可控时钟）；`/steps` 与代码路径已核。
- **确认 TTL 5 分钟超时**：跨机未等待；三审本机已验（`cancelled` + `audit timeout`）。
- **StorageGuard / L3 override / L2 不语义索引 / 无痕模式**：跨机无对应写入端点，未跨机复现；三审本机已验。
- **UI 浏览器真机点击**：仅静态检查 + Node 状态机（三审已跑真实迁移序列），未做端到端浏览器交互。
- **per-user 多主体**：当前单共享 token，无法构造不同主体的凭据组合。
- **未读其他模型的四审报告**（独立性要求），因此本报告不对其结论做对比或背书。

---

> 审查主体：**Tencent Hunyuan Hy（腾讯混元 Hy）** · WorkBuddy Agent 模式
> 验收机 192.168.0.14 → 目标机 192.168.0.28:8080 · 基线 `d23d027` · 2026-11-18
> 本报告所有 token / 确认令牌均做脱敏处理；原始探针输出保存在本文件附录 A 的脱敏摘要中。
