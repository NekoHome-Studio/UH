# M1 四审验收报告（跨机）· DeepSeek-V4-Flash

| 项 | 内容 |
|---|---|
| 验收对象 | M1 能力期 · 基线 `c42e71b`（M1 跨机验收报告）· 实际审查点 **`d23d027`**（HEAD = 基线 + 环境状态总览） |
| 验收形态 | **跨机**：本机 `192.168.0.14`（编排端，Python 3.14.3 / Node 24.13.1）→ 服务器 `192.168.0.28:8080`（Win10 22H2，Python 3.12.10 venv，生产鉴权模式 B） |
| 审查时间 | 2026-11-18 19:48 ~ 20:40（Asia/Shanghai） |
| 签署模型 | **deepseek-v4-flash**（provider `deepseek-official`，reasoningEffort `high`；DSH 记账名 `deepseek-flash`）· preset `standard` |
| 独立性 | 未阅读其他审查者的跨机材料；结论独立从「代码 + 契约 + 双端运行时」得出 |
| 结论 | **不通过**（1 项 P0 阻断：服务器鉴权主令牌已进入公开仓库与运行日志；其余跨机链路全部通过） |

---

## 0. 部署一致性（先验条件）

| 检查 | 方法 | 结果 |
|---|---|---|
| 内核文件漂移 | 16 个 `kernel/*.py` 逐一 SHA256 对比（本机 vs `D:\uh\kernel`） | **16/16 完全一致**（仅大小写归一后全等），无部署漂移 |
| 契约文件在位 | `D:\uh\docs\contracts\events.schema.json` | 存在（`event_mapping` 运行时依赖） |
| 服务在跑 | `GET /health` | `200` |
| 运行环境差异 | Python 3.14.3（本机）vs 3.12.10（服务器 venv） | 版本不同但测试全过，构成**跨版本正向证据** |

---

## 1. 门禁（双端）

| 门禁 | 位置 | 结果 |
|---|---|---|
| `python -m pytest -q` | 本机 | **148 passed**（47.6 s） |
| `node scripts/verify-all.mjs --full` | 本机 | **8/8 PASS** |
| `server-test.ps1`（pytest） | 服务器 D:\uh | **148 passed**（23.7 s，独立 venv/Python 3.12） |
| `node scripts/verify-all.mjs` | 服务器 D:\uh | **FAIL ×2**（B2 运行时映射、C 交叉一致性预检）→ 见 P2-1 |

结论：**测试门禁跨机成立**（本地 148 / 服务器 148）；**契约门禁跨机不成立**（服务器 2 项失败），"本地+服务器全绿"的说法在契约门禁部分不准确。

---

## 2. 跨机生产链路（本机 → 服务器，生产鉴权模式 B）

探针：`.work/probe4/cross.py`（30 项）+ 单连接确认链复测。全部走真实网络 `http/ws://192.168.0.28:8080`。

| 组 | 结论 | 关键证据 |
|---|---|---|
| A 免鉴权面 | ✅ 5/5 | `/health` `/ready` `/metrics` `/docs` `/openapi.json` 均 200（免鉴权为**有意的信息面开放**，见 P2-2） |
| B 无凭据拒绝 | ✅ 5/5 | `/api/v1/sessions`、`/api/v1/plugins`、**`/plugins`**、`/memory/export`、`/memory/short-term` 全 401 + `UH-USR-1001` |
| C 基本链路 | ✅ 4/4 | 201 建会话；202 发消息；`stream_url` = `ws://192.168.0.28:8080/api/v1/stream?task_id=…`（`UH_PUBLIC_BASE_URL` 生效，三审 N2 在部署侧已消解）；`meta.trace_id` 在 |
| D WS 实时流 | ✅ 4/4 | 单连接 53 事件，`event_id` **1..53 连续无空洞**；终态 `task.finished`；终态重连**只推终态 + close 1000** |
| E 工具确认 inline | ✅ 4/4 | 单连接完整链：`task.created → tool.call → confirmation_required →（HTTP 200 批准）→ confirmation_resolved → tool.result → task.finished`；拒绝 → `task.cancelled` |
| F 取消 / steps / 409 | ✅ 4/4 | `POST /cancel` → `status=cancelling` → 终态 `cancelled`；`/steps` 含 `task_id`；终态 `resume` → 409 |
| G 幂等 | ✅ 1/1 | 同键同 body 跨机回放同一 `task_id` |
| H 显式鉴权别名 | ✅ 2/2 | 带 token `/plugins`、`/memory/export` → 200 |
| 路径白名单（补充） | ✅ | `index_document(path=C:\Windows\win.ini)` 与 `path=D:\uh\pyproject.toml` 均被拒：`UH-CAP-4004 文档路径越界` |

**合计 30/30 PASS**（其中 E3 首轮因探针在批准后才重连、命中"终态只推终态"设计而误报，复测改为**批准期间保持 WS 连接**后通过——这是探针口径问题，不是产品缺陷）。

---

## 3. P0 阻断项（1 项，必须先处置）

### P0-1 服务器鉴权主令牌进入**公开仓库**并已推送，且同时落运行日志

| 证据 | 内容 |
|---|---|
| 仓库内的令牌 | `docs/runbooks/环境状态总览.md:55` 以 `UH_AUTH_TOKEN=<43 字符明文>` 形式提交（commit `d23d027`） |
| 违反的硬性规则 | `AGENTS.md §4`：「密钥：一律经环境变量 / 系统钥匙串引用，**严禁硬编码进代码、测试、日志、仓库**」 |
| 已推送 | `git log --oneline @{u}..HEAD` 为空 → 该 commit 已在 `origin/main` |
| **仓库公开** | GitHub API：`private=false`，`visibility=public`，`pushed_at=2026-11-18T11:47:47Z` |
| 历史可检索 | `git log --all -S<token>` 命中 `d23d027`（即使后续删除文件，历史仍可读出） |
| 日志二次泄漏 | 服务器 `D:\uh\kernel.out.log` 中 **97 行**含 `token=`；`kernel.err.log` 的 `WebSocket … [accepted]` 行含**完整令牌**（每次 WS 连接写一次） |

**影响判定（精确表述）**：该令牌是当前唯一鉴权凭据（单共享 token 模型），任何能读到公开仓库或 `D:\uh\kernel.*.log` 的人，都能在局域网内取得**完整业务权限**（建任务、触发 dev 工具入口、读取/清空长期记忆）。服务器目前未做公网端口映射，故"公网直接可利用"暂不成立；**一旦做内网穿透/端口映射即等同无鉴权**。

**立即可执行（建议 30 分钟内）**

```powershell
# 1) 轮换：生成新令牌（本机）
python -c "import secrets;print(secrets.token_urlsafe(32))"
# 2) 服务器：改 start-uh.ps1 的 UH_AUTH_TOKEN（只放脚本，不再进仓库），重启
# 3) 本机：更新 .work/server-auth-token.txt；清空 D:\uh\kernel.out.log / kernel.err.log
# 4) 仓库：从文档移除令牌（改引用 .work/server-auth-token.txt 或 env），
#    commit 后按需 filter-repo 清历史；无论是否清历史，令牌已轮换即失效
# 5) 加门禁：CI/pre-commit 增加 secret 扫描（gitleaks 或正则 UH_AUTH_TOKEN=[A-Za-z0-9_-]{20,}）
```

> 说明：报告与回复中均不复制令牌原文；只记录位置与长度。

---

## 4. P1（部署形态风险，修复前不应视为"生产就绪"）

### P1-1 真实服务器以验收期 `UH_DEV_MODE=1` 运行
`/api/v1/dev/tool-task` 对任何持 token 者可触发工具执行（探针 E0 实测 202）；`/api/v1/dev/confirmations` 暴露全任务确认元数据（令牌前 6 字符 + 全量审计）。环境文档已注明"正式生产应设 0"，但**当前部署就是这个形态**，属未收敛的暴露面。建议在 M1 出口前把服务器切到 `UH_DEV_MODE=0` 并复跑一次确认链路（确认流程改用正式入口后需要相应调整）。

### P1-2 `UH_RETRIEVAL_ROOT` 未配置且 `D:\uh\data` 不存在
`index_document(path=…)` 在部署态**100% 失败**（路径白名单根目录缺失）。安全上没问题（越界全拒），但功能上"按路径索引"在服务器不可用；若将来把 ROOT 设成 `D:\uh`，等于把源码目录纳入可索引范围。建议部署脚本显式 `mkdir D:\uh\data` 并设 `UH_RETRIEVAL_ROOT=D:\uh\data`。

### P1-3 明文 HTTP + 查询串令牌
`ws://` 与 `?token=` 组合意味着：局域网嗅探、代理日志、浏览器历史都会留存主令牌；浏览器无法给 WS 设自定义头是客观限制。建议：生产前置 TLS 反代；WS 改用**短时票据**（`ticket = HMAC(task_id, exp)`，TTL 60s，只能订阅单个 task），主令牌不再出现在 URL。

---

## 5. P2（契约/工具链一致性）

| 编号 | 问题 | 证据 / 建议 |
|---|---|---|
| P2-1 | **契约门禁跨机不可复现** | 服务器 `verify-all` 报 B2/C 失败；直接运行 `node scripts/review-precheck.mjs` 报 `Cannot find module 'js-yaml'`（依赖装在 `.work/verify-deps`，靠 `YAML_PATH` 传入，在服务器 node v24.22.3 + cmd/PowerShell 混合调用下解析失败）；B2 依赖 `python -c` 的解释器解析（服务器 PATH python ≠ venv）。建议：verify-all 显式 `PYTHON=` venv 路径 + `YAML_PATH` 用绝对路径并在失败时打印真实 stderr；CI 与服务器用同一入口 |
| P2-2 | `/metrics`、`/docs`、`/openapi.json` 免鉴权 | 部署在 `0.0.0.0:8080`，局域网可读；`/metrics` 暴露记忆条目数。若为有意设计，请写进 `deploy-hardening.md` 的豁免清单 |
| P2-3 | `UH-CAP-4004` 被用于"不可重试"的路径越界错误 | 探针返回 `retryable: true`，前端会重试一个必然失败的操作。建议路径越界用 `UH-USR-1003`（参数校验）或 CAP-4004 下 `retryable=false` 的细分 detail |
| P2-4 | 服务器无 TLS、无反向代理 | 与 P1-3 同源；`deploy-hardening.md` 目前只写"生产化前用开发身份"，未写 TLS 要求 |

---

## 6. P3（登记、不阻断）

1. 单共享 token 无 per-user principal（三审 N7 已登记，跨机下同样成立：同一 token 的任意 client 可读他人任务）。
2. UI 令牌存于 `localStorage`（XSS 面），配合 P1-3 的主令牌模型风险叠加。
3. 服务器 `.uh` 运行态随跑测增长（`long_term_memory.json` 126 KB / `checkpoints.sqlite3` 294 KB），沿用三审 N5 的测试隔离建议后可缓解。
4. `kernel.out.log` 无轮转（当前 99 KB，长期运行需 logrotate 或按大小切分）。

---

## 7. 验收结论

**不通过。**

- **通过部分**：部署一致性（16/16 文件哈希一致）、测试门禁跨机（148/148 双端）、跨机生产链路 30/30（鉴权矩阵、WS 实时与回放、确认 inline/RPC、取消、steps、409、幂等、路径白名单）；此前三审的 P0/P1 项在跨机环境下未出现回归。
- **阻断部分**：**P0-1** 令牌进入**公开**仓库并已推送，同时以查询串形式写入服务器运行日志。这一条与 M1 功能无关，但直接构成鉴权体系失效，按 `AGENTS.md §4` 属硬性规则违反，不能以"能力期已交付"放行。
- **复验口径**：轮换令牌 + 清理仓库与日志 + 收紧日志脱敏（或改短时票据）后，只需复跑 §1 的服务器 pytest 与 §2 的跨机探针即可给出最终结论；其余 P1/P2 可与 M2 并行收敛。

**签署**：`deepseek-v4-flash` · 2026-11-18

---

## 8. 探针副作用记录

- 未修改服务器任何文件；未 `scp` 任何内容；未重启服务。
- 在服务器**运行态**创建了约 10 个任务（含 4 个 dev 工具任务、2 次路径越界尝试——均被拒、未写入索引），并因此新增约 30 行含 `token=` 的访问日志（属 P0-1 现象的复现痕迹，非新增风险）。
- 本机未写入仓库 `.uh/`；探针脚本位于 `.work/probe4/cross.py`（gitignored）。
- 读取了 `.work/server-auth-token.txt` 用于鉴权，**报告中不含令牌原文**。

---

## 附录 · 复现命令

```bash
# 本机
python -m pytest -q                                   # 148
node scripts/verify-all.mjs --full                    # 8/8

# 服务器
ssh ipc "cd D:\uh && powershell -File D:\uh\server-test.ps1"     # 148
ssh ipc "cd D:\uh && node scripts/verify-all.mjs"                # B2/C FAIL

# 跨机探针
python .work/probe4/cross.py                          # 30/30（E3 需保持 WS 连接同时批准）

# 部署一致性
ssh ipc "powershell -Command \"Get-FileHash D:\uh\kernel\*.py -Algorithm SHA256\"" | diff 与本地 sha256sum
```
