# UH! M1 五审：四审修复后独立回归复核报告

> **审查模型：Tencent Hunyuan Hy（腾讯混元 Hy）**
> 运行产品：WorkBuddy（Agent 模式）
> 审查日期：2026-11-18（21:32–22:05 GMT+8）
> 验收机：192.168.0.14 · 目标机：192.168.0.28:8080（`ssh ipc`，`D:\uh`）
> 审查基线：`main` HEAD **`3031015`**（`origin/main` 同步），对比基线 `d23d027`（四审对象）
> 口径：四审 `docs/reviews/2026-11-18-M1-四审跨机验收报告-Tencent-Hunyuan-Hy.md` 的 P0/P1 清单 + `docs/reviews/2026-11-18-四审修复报告.md`（修复台账，非独立审查报告）+ brief 0010 的 9 项生产链路
> **独立性声明**：未阅读其他审查模型的五审报告；结论全部由「当前代码 + 修复 diff + 跨机真实 HTTP/WS 实测」独立得出。
> 报告性质：非契约、非规范文件；不修改源码/契约，只新增本报告。

---

## 0. 结论摘要

**验收结论：有条件通过（不推荐直接生产放行）**。

四审最硬的 **P0-1 凭据泄露已实质闭环**：旧 token 实测失效、新 token 经文件引用注入、进程命令行与访问日志去明文、文档改占位符、新 token 未入仓。四审 **P0-2 的主链也确实被挡住**（非匹配 client_id 的 attacker 在 REST/SSE/WS 全 403/4403）。

但修复引入/遗留了四条必须在放行前收敛的问题：

1. **双因子可伪造，越权链未实质消除**：`client_id` 是调用方自 asserted 的字符串，WS 从 query 取、REST 从 cookie 取。实测伪造 victim 的 client_id 后，攻击者仍能经 WS 拿到**完整确认令牌**、经伪造 cookie 通过 REST 守卫、并成功 `/confirm` 改变他人任务终态。修复报告「越权阻断 7/7」的结论偏乐观。
2. **新增的 secret 扫描门禁可被一个空格绕过**：正则把 `\s*` 写成 `s*`，带空格的 `UH_AUTH_TOKEN = <明文>` 完全不匹配；本报告把假 token 放进 `docs/reviews/` 后，verify-all 仍全绿、check 9 报 `hits=` 空。
3. **client_id 事实上的必填未回写契约**（openapi/asyncapi/task-flow 全文无 `client_id`），且 REST/WS/创建/幂等四处取值优先级不一致，导致合法用户在 header 与 cookie 不一致时被自己的任务 403、无 cookie 的 SDK 式 WS 被 4403。
4. **终态释放 delta 破坏事件级回放**：任务终态后按 `after_event_id` 重连只回 1 帧 `task.finished`，中间百余条 delta 静默丢失且不报 4408。

若部署模型是「单用户设备 + 可信局域网」，上述可作为有条件通过；若是多用户或不可信网络，按四审同等严格口径（无新可利用绕过）应判**不通过**。

---

## 1. 基线与门禁（亲测）

```
git status -sb                     -> ## main...origin/main
git rev-parse --short HEAD         -> 3031015
git log --oneline d23d027..HEAD    -> 709eebd（入库四审报告）/ c80e261（P0-P1 修复）
                                      a61d065（修复报告）/ 3031015（交接文档）
```

| 检查 | 实测 |
|---|---|
| `C:\Python314\python.exe -m pytest -q`（独立 `PYTEST_DEBUG_TEMPROOT`） | **148 passed, 118 warnings in 38.69s** |
| `node scripts/verify-all.mjs --full` | **VERIFY_ALL_PASSED (8 checks, full)**，A/B/B2/C/D/F/G PASS，E 为 INFO |
| `scripts/review-precheck.mjs`（由 verify-all C 项调用） | **9 checks**，`.work/precheck-results.json` 含「9 密钥扫描」`ok:true, hits=` |
| 跨机连通 | `health` 200 / `ready` 200 / `openapi.json` 200；无凭据 `POST /sessions` 401 |
| 部署一致性 | 本机 vs 服务器 `kernel/main.py` SHA256 `eb727b9f…`、`kernel/tasks.py` `01342694…` **完全一致**（修复已同步） |

---

## 2. 四审修复逐项复核矩阵

| 四审项 | 修复动作 | 本轮判定 | 实测证据 |
|---|---|---|---|
| **P0-1 明文 token 入仓/日志/进程** | 轮换 + `_read_auth_token()` 文件引用 + 文档占位符 + `--no-access-log` + check 9 | ✅ **已闭环**（门禁本身除外，见 N2） | 旧 token bearer/query 全 **401**；新 token bearer/query 全 **201**；进程命令行只剩 `UH_AUTH_TOKEN_FILE=D:\uh\.auth-token`；`kernel.out.log` 仅 4 行且不含本轮 `SECRET_SCAN_MARKER`；`git grep` 新 token 无命中；`docs/runbooks/环境状态总览.md:55` 已改文件引用 |
| **P0-2 SSE 无 guard / owner 单因子** | SSE 补 `_task_guard`、`owner_matches` 双因子 | ⚠️ **主链阻断，实质未消除** | 不同 client_id 的 attacker：REST 403 `UH-USR-1002`、SSE 403、WS 首帧 `UH-USR-1002`(4403)；但伪造 client_id 后完整链仍成立（N1） |
| P1-5 幂等 caller 漂移 | caller 改 `header → cookie → token` | ✅ **已修** | 带 `X-UH-Client-ID: clientA` 同键同 body 两次 `same_task=True`；无 header 两次亦 `True`（四审为 False） |
| P1 CAP-4004 参数错误可重试 | `-32602` → `UH-USR-1003 retryable=false` | ✅ **已修** | 本机补测路径越界：`code=UH-USR-1003 retryable=False msg=文档路径越界…` |
| P1 span 溯源闭合 | `bus.publish` 自动 span_id；`tool.result` 复用 call span | ✅ 静态确认 | `kernel/protocol.py:63-66`、`kernel/mcp.py:553-580` diff；未动态复测 |
| P1-4 终态 delta 内存 | 终态剔除 event_buffer 中 delta | ⚠️ **已实现但引入回放回归** | 见 N5 |
| P1 dev 生产关 | 修复报告列为待办 | ❌ **未修** | `GET /api/v1/dev/confirmations` 仍 200；`UH_DEV_MODE=1` |
| P2-6 服务器多实例 | 未处理 | ❌ **未修** | 当前监听 PID 7752；仍有 `D:\Python312\python.exe main.py` 残留 + venv/D:\Python312 双 uvicorn |
| P2-7 TLS / 安全头 / query token | 修复报告列为 M2 生产化 | ❌ **未修** | 仍为 `http://` / `ws://`；Cookie 无 `Secure`；UI 仍把 token 存 localStorage 并拼进 URL |
| 四审 P0-1「清 git 历史」 | 修复报告列为「可选待办」 | ⚠️ 残留 | `git log --all -S<旧token>` 仍返回 `d23d027`（引入）与 `c80e261`（删除）；旧 token 已失效，风险大幅降低但历史未清 |

---

## 3. 本轮新发现（按严重性）

### N1（P0 降级 / P1 阻断）双因子可伪造，跨客户端越权链未实质关闭

**跨机 6 步完整复现**（victim `victim_v3`，attacker 持同一共享 token）：

```
1) victim  POST /api/v1/dev/tool-task (irreversible)      -> 202, pending_confirmation
2) attacker GET /api/v1/tasks/{tid}  (client_id=attacker) -> 403 UH-USR-1002   ✅ 主链已挡
3) attacker WS  /api/v1/stream?task_id={tid}&client_id=victim_v3
      -> task.created / tool.call / task.confirmation_required
      -> payload.confirmation_token = 完整令牌（未脱敏）      ❌
4) attacker GET /api/v1/tasks/{tid}/stream (Cookie: uh_client_id=victim_v3) -> 200  ❌
5) attacker POST /api/v1/tasks/{tid}/confirm {偷来的令牌, rejected}（同样伪造 cookie）
      -> 200 {"decision":"rejected","operator":"spoof_v3"}   ❌
6) victim 的任务被改为 cancelled / rejected 终态            ❌
```

**根因**：`kernel/tasks.py:432-441` 的双因子第二项只是比较调用方**自己提供**的字符串：

```python
if owner_token and not (token and secrets.compare_digest(token, owner_token)): return False
if owner_client_id and client_id != owner_client_id: return False
```

而 `client_id` 的来源在两处都由调用方完全控制：

- WS（`kernel/main.py` 约 660）：`params.get("client_id") or headers X-UH-Client-ID or cookies`，**query 参数优先**；
- `_task_guard`（`kernel/main.py:503-512`）：`cookies 优先于 header`。

没有签名、没有服务端下发绑定、没有与 token 派生的关系。修复报告第 20 行自己也承认「client_id 自 asserted（可 spoof）」，但第 18/41 行仍给出「越权阻断 7/7 / 当前安全态可复跑独立验收」，口径偏乐观。

**可获取性评估（实测）**：`/memory/export`（123KB）与 `dev/confirmations` **不含** victim client_id/session_id（V1-3 实测 False），因此不是零门槛；但 ① 全链路明文 HTTP/WS，同局域网可嗅探 `X-UH-Client-ID`/Cookie；② client_id 常取默认值（session_id）或脚本中的可预测常量；③ 任何持共享 token 者本就是同一「用户」——第二因子只增加了猜测成本，未建立真正的主体边界。

**建议**：把 client_id 改为服务端签名下发的 HttpOnly cookie（HMAC 绑定 token+session），或引入 per-user principal；同时在契约/部署文档明确「单共享 token = 单用户设备等价」的威胁模型假设，不要表述为「越权已阻断」。

### N2（P1 阻断）secret 扫描门禁可被一个空格绕过，防再泄露机制失效

`scripts/review-precheck.mjs:121`：

```js
/UH_AUTH_TOKENs*=s*["']?[A-Za-z0-9_-]{20,}/.test(read(f))
```

作者意图应是 `\s*`，实际写成了字面字母 `s` 的重复。Node 实测：

```
UH_AUTH_TOKEN=<43字符>            -> true   （能查）
UH_AUTH_TOKEN = <43字符>          -> false  ❌
UH_AUTH_TOKEN = "<43字符>"        -> false  ❌
UH_AUTH_TOKEN: <43字符>           -> false  ❌
```

**端到端验证**：在 `docs/reviews/` 放入临时文件 `UH_AUTH_TOKEN = ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefg`，跑 `node scripts/verify-all.mjs` → `VERIFY_ALL_PASSED (6 checks, fast)`、C 项 PASS、`precheck-results.json` 的 check 9 仍 `ok:true, detail:"hits="`。**门禁完全没发现**。（探针文件用后已删除，工作区已复原。）

另：扫描范围仅 `docs/*.md`、`kernel/*.py`、`scripts/*.mjs|*.ps1`、`briefs/*.md`、`README.md`、`AGENTS.md`，**不含** `ui/`、`tests/`、`.github/`、`uh-issues/`、`PROGRESS.md` 等；write 到这些目录同样漏检。

**建议**：正则改 `/UH_AUTH_TOKEN\s*[:=]\s*["']?[A-Za-z0-9_-]{20,}/`；用 `git ls-files` 遍历全部受版本控制文本；把 check 9 单独接入 CI 与 pre-commit，并对扫描逻辑本身加正/负例单测（当前无任何测试覆盖该正则）。

### N3（P1）client_id 成为事实必填，但契约未回写

`docs/contracts/openapi.yaml`、`asyncapi.yaml`、`task-flow.md` 全文 grep `client_id|client-id` **零命中**；契约仍只声明 bearerAuth / query token。而修复后：

```
SDK 式 WS（仅 query token，无 client_id/cookie） -> error UH-USR-1002 (4403)
浏览器式 WS（带同源 Cookie uh_client_id）        -> task.created / step.started / delta ✅
```

即 WS 订阅从「token 即可」变成「必须 client_id 贯穿」，`docs/runbooks/交接文档.md:63` 也写明「跨机验收探针需新 token + client_id 贯穿」，但**冻结契约没有相应变更**。按 `AGENTS.md:4` 契约冻结规则，这属于实现先行、契约未回写；且会让按契约实现的第三方 SDK 直接 4403。

**建议**：要么在 asyncapi/openapi 明确 `client_id` 必填与来源优先级，要么让服务端在缺少 client_id 时从已认证主体派生，保持对契约客户端兼容。

### N4（P1）client_id 四处取值优先级不一致，合法用户被误伤

| 位置 | 优先级 |
|---|---|
| 创建任务 `send_message` / `dev_tool_task` | `X-UH-Client-ID` → cookie → session_id |
| `_task_guard`（REST 任务端点） | **cookie → X-UH-Client-ID** |
| WS `attach` | **query client_id → header → cookie** |
| 幂等 caller（本轮已改） | `X-UH-Client-ID` → cookie → query token |

实测：用 `X-UH-Client-ID: header_owner_v4` 创建任务后，同一客户端在请求中同时带该 header 与一个旧的 `Cookie: uh_client_id=stale_cookie_v4`：

```
both header+cookie(stale) -> 403 UH-USR-1002   ❌ 合法用户被自己任务拒绝
header only               -> 200 ✅
cookie(new) only          -> 200 ✅
```

浏览器残留旧 cookie、或 SDK 同时设置 header 与 cookie 时都会触发。建议统一为单一可信来源（签名 cookie 或认证主体），并在所有端点复用同一个解析函数。

### N5（P2）终态释放 delta 破坏事件级回放，且不报 4408

`kernel/tasks.py:258-259` 在终态时剔除 event_buffer 全部 delta；`subscribe()` 对终态任务只返回 `terminal_events[-1:]`；`replay_available()` 对终态恒 True。实测（任务达到 `task.finished`，event_id=117）：

```
after=0    -> 仅 [('task.finished', 117)]
after=20   -> 仅 [('task.finished', 117)]
after=120  -> 仅 [('task.finished', 117)]
（运行中任务 after=0/3/20 均能正常回放 delta 序列）
```

中间的百余条 `delta` 静默丢失、无 `replay_unavailable`/4408 提示；虽然 `task.finished.payload.final_result.content` 含完整答案可恢复文本，但「按 after_event_id 回放不丢事件」的语义已不成立。建议：终态后显式返回 4408 或 `replay_summary`，或把 delta 摘要保留在 step_log/持久化层，并把该行为写入契约。

### N6（P2）修复台账与交接文档口径偏乐观

- `docs/reviews/2026-11-18-四审修复报告.md:18,41`：「越权阻断 7/7」「当前安全态可复跑独立验收」；
- `docs/runbooks/交接文档.md:79`：「SSE守卫+owner_matches双因子（越权阻断7/7）」。

实测 N1 显示 spoof 链仍可改变他人任务终态。建议改为「主链阻断；client_id 自 asserted 可伪造，未实现多用户隔离；依赖单用户设备假设」。

### N7（P2）运维与传输残留（修复报告已列待办，本轮确认仍在）

- 服务器仍多实例：`D:\Python312\python.exe main.py`（18:01 残留）、venv uvicorn、`D:\Python312` uvicorn；当前监听 PID 7752。`start-uh.ps1` 的清理条件 `-like '*uvicorn*kernel.main*'` 不覆盖 `main.py` 残留。
- 明文 HTTP/WS + query token 进 URL（`--no-access-log` 只解决服务器日志，不解决局域网嗅探）；Cookie 无 `Secure`；无 HSTS/CSP/X-Frame-Options。
- `UH_DEV_MODE=1` 仍开，`/api/v1/dev/confirmations` 返回全量审计（8 条，含 task_id/tool_name/operator/decision），dev 端点仍在公开 openapi 中；任何持 token 者可 `toggle` 关闭能力（四审 P1-3/P3-11 未关）。
- 事件风暴未解：MockLLM 逐字符 delta，本轮一个 `hi` 任务 6 秒仍 `running`、终态 117 帧；终态释放只解内存不解带宽。
- git 历史仍含旧 token（已失效）。

---

## 4. 9 项生产链路抽检（本轮覆盖）

| # | 抽检 | 判定 |
|---|---|---|
| 1 鉴权 | 旧/新 token 401/201；无凭据 401；ops 豁免；dev 端点需 token | ✅ |
| 2 会话/任务存在性 | 沿用四审（未重复） | ✅（四审） |
| 3 WS 实时流 | 浏览器式 cookie WS 正常；SDK 式无 client_id WS 被 4403（N3）；错 token 4401（四审） | ⚠️ |
| 4 确认 inline | victim 自身可确认；attacker 不同 client_id 403；**伪造 client_id 仍可确认（N1）** | ❌ |
| 5 确认 RPC fail-closed | 四审已验；本轮未重复 | ✅（四审） |
| 6 取消 / 409 | 本轮多次 cancel 清理正常 | ✅ |
| 7 超窗 /steps | `/steps` 正常；终态回放回归（N5）；4408 仍未动态复现 | ⚠️ |
| 8 隔离 / 隐私 | 主链阻断但 spoof 链成立（N1）；幂等 caller 修复（✅）；`/memory/export` 不含 client_id | ❌ |
| 9 UI | 浏览器路径可用；token 仍 localStorage + URL（N7） | ⚠️ |

---

## 5. 探针与副作用

- 探针组 V1~V5（本机 `%TEMP%\uh5_probe_*.py`），全部经真实 HTTP/WS 发往 .28:8080；本机补测 CAP-4004 与 secret 正则。
- 创建约 15 个独立会话/任务；`dev/confirmations` 盘点 **8 条全部 resolved、pending=0**，挂起任务已全部 cancel 清理（V3-5 / V4-4 / V5-2 实测）。
- 临时文件 `docs/reviews/__secret_scan_probe_tmp.md` 仅用于门禁绕过验证，**已删除**；`git status --short` 除他人新增的一份未跟踪报告外无残留。
- 未执行 `DELETE /memory/long-term`、未清库、未重启服务、未改服务器文件/配置、未改任何源码或契约。

---

## 6. 验收结论与放行前清单

**结论：有条件通过（不推荐直接生产放行）· 签署：Tencent Hunyuan Hy（腾讯混元 Hy）**

已达标：P0-1 凭据泄露事件闭环（旧串失效、无明文留痕、文件引用、日志关闭）、P0-2 非伪造攻击主链全阻、幂等 caller、CAP-4004、span 溯源、门禁 148/148 + full 8/8、双端部署一致。

**放行前必须收敛**

1. **N1**：client_id 改为服务端签名绑定或引入 per-user principal；在契约/部署文档写明威胁模型；修正「越权阻断 7/7」表述。
2. **N2**：修复 secret 扫描正则、扩大扫描范围、加正负例单测、接入 CI/pre-commit。
3. **N3/N4**：契约回写 client_id 要求；统一四处 client_id 解析优先级（建议单一函数 + 签名 cookie）。
4. **N5**：终态回放语义（4408 或摘要）并回写契约。

**建议同批**：生产关 `UH_DEV_MODE`、dev 端点移出公开契约、TLS + 安全头 + WS 短时票据、服务器多实例清理、delta 合批、git 历史旧 token 清理（可选）。

**重验口径**：修完 N1~N4 后，重跑本报告 V1（spoof 链，期望全 403/4403）、V3（浏览器/SDK WS）、V4（终态回放）、门禁端到端（带空格假 token 应被 check 9 命中），再跑 `pytest -q` + `verify-all.mjs --full`。

---

## 附录 A：关键输出（脱敏）

```
# 凭据轮换
OLD token bearer -> 401 ; OLD token query -> 401
NEW token bearer -> 201 ; NEW token query -> 201
git grep NEW token -> 无命中 ; git log -S OLD token -> d23d027, c80e261（历史未清）
kernel.out.log 行数=4，SECRET_SCAN_MARKER 命中=0（--no-access-log 生效）
进程命令行：UH_AUTH_TOKEN_FILE=D:\uh\.auth-token（无明文）

# 越权链（spoof）
attacker plain GET -> 403
WS spoof(client_id=victim) -> task.created / tool.call / task.confirmation_required / 完整令牌=True
REST spoof(cookie=victim) SSE -> 200 ; GET -> 200
POST /confirm(偷来令牌+伪造cookie) -> 200 decision=rejected

# 门禁绕过
正则：'UH_AUTH_TOKEN = <43字符>' -> false
临时文件放入 docs/reviews 后 verify-all -> VERIFY_ALL_PASSED (6 checks, fast)，check9 hits=

# 归属一致性
both header+cookie(stale) -> 403 ; header only -> 200 ; cookie(new) only -> 200
SDK-style WS(no cookie/cid) -> error UH-USR-1002 ; browser-like WS(cookie) -> 正常

# 终态回放
final event_id=117 ; after=0/20/120 -> 均仅 [('task.finished',117)]

# 修复有效项
幂等 header 优先：same_task=True / True
CAP-4004：code=UH-USR-1003 retryable=False
```

## 附录 B：未覆盖范围

- 4408 超窗（需 >5min 运行中任务）、确认 TTL 5 分钟超时、StorageGuard/L3/L2/无痕跨机路径、浏览器真机点击：均沿用三/四审结论，本轮未重复。
- span 溯源闭合仅静态核对 diff，未动态复测 bus 事件 span_id 配对。
- 未阅读其他模型的五审报告（独立性要求）。
- 旧 token 虽已失效，git 历史仍含该字符串；本报告不复现完整值。

---

> 审查主体：**Tencent Hunyuan Hy（腾讯混元 Hy）** · WorkBuddy Agent 模式
> 验收机 192.168.0.14 → 192.168.0.28:8080 · 基线 `3031015` · 2026-11-18
> 本报告所有真实 token 均脱敏；假 token 仅用于门禁绕过验证。
