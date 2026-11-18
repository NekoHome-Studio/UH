# M1 三审准备清单 · DeepSeek-V4-Flash

| 项 | 内容 |
|---|---|
| 用途 | 为 M1 第三轮验收（brief 0010）做**就绪准备**：锁基线、复现门禁、跑通探针包、列出仍需挖的点 |
| 准备时间 | 2026-11-18 18:12 ~ 18:40（Asia/Shanghai） |
| 准备者 | DSH 会话 `session-a9dd898c-ce93-426b-a3a3-096edba8ca48` |
| **模型** | **deepseek-v4-flash**（provider `deepseek-official`，reasoningEffort `high`；记账名 `deepseek-flash`）· preset `standard` |
| 性质 | **准备件，不是验收结论**。结论待三审正式出具（报告命名 `2026-11-18-M1-三审验收报告-<模型名>.md`） |
| 副作用 | 无。本轮所有探针均隔离到 `.work/probe3/tmp/`，未触碰仓库 `.uh/`（仅 pytest 本身按现有约定写入） |

---

## 1. 基线锁定

| 项 | 值 |
|---|---|
| 三审基线 | `cb4d1e6`（bind tag **m1-review2-fixed-v0.1.0**） |
| 当前 HEAD | `8a7d1c5` = 基线 + `briefs/0010-m1-third-review-acceptance.md`（仅新增 53 行 brief） |
| 工作树 | 干净（`git status --short` 0 行） |
| 已有 tag | contract-v0.1.0 / contract-internal-v1 / contract-events-v1 / m1-v0.1.0 / m1-review-fixed-v0.1.0 / m1-review2-fixed-v0.1.0 |

结论：三审可直接在 `8a7d1c5` 上做，相对基线的唯一差异是 brief 本身，无代码变更。

---

## 2. 门禁复现（准备者亲跑）

| 门禁 | 命令 | 结果 |
|---|---|---|
| 单元测试 | `python -m pytest -q` | **142 passed**（7 次中 6 次）；**1 次为 141 passed / 1 failed** ⚠️ |
| 契约门禁 | `node scripts/verify-all.mjs` | 6 项全绿 |
| 完整门禁 | `node scripts/verify-all.mjs --full` | **8/8 PASS**（含 swagger-cli OpenAPI 3.1、@asyncapi/parser 2.6） |
| 新增回归 | `pytest tests/unit/test_review_fixes.py -q` | 18 passed |

### 2.1 门禁不稳定项（三审必须先知道）

- 失败用例：`tests/unit/test_cli.py::test_reset_cleans_logs`
- 现象：全量跑 7 次，**1 次红**（首次、休眠 19h 后）；单跑必绿；此后连续 6 次全绿。
- 断言：`assert not cli.LOG_FILE.exists()`，即 `dev reset` 删除 `.uh/kernel.log` 后该文件仍存在。
- 机制假设：`dev_up` 以 `open(LOG_FILE, "ab")` 把同一路径句柄交给子进程（`subprocess.Popen(stdout=log)`）。若上一用例的 uvicorn 子进程在 Windows 上尚未完全退出，其继承句柄会在 unlink 之后重新生成文件；冷启动/慢盘时窗口更大。
- 建议（低成本）：该用例 `monkeypatch` 掉 `cli.LOG_FILE` / `cli.STATE` / `cli.PID_FILE` 到 `tmp_path`，与真实运行态解耦。
- 三审口径建议：门禁结论写「142/142（已知 1 例 flaky，见清单 §2.1）」而不是无条件 142。

### 2.2 顺带发现的测试侧问题（不阻断 M1，但影响可信度）

`tests/unit/conftest.py` 只设了 `UH_DEV_MODE=1`，**未隔离存储路径**，于是任何 `TestClient(create_app())` 都会写真实 `.uh/`：

```
.uh/long_term_memory.json     → 348 条（多次跑测累积）
.uh/long_term_vectors.json    → 420 KB
.uh/checkpoints.sqlite3       → 1.1 MB
```

建议加一个 autouse fixture，把 `UH_LONG_TERM_STORE` / `UH_LONG_TERM_VECTOR_STORE` / `UH_RETRIEVAL_STORE` / `UH_RETRIEVAL_CHROMA` / `UH_CHECKPOINT_SQLITE_PATH` 指到 `tmp_path`。这同时也解释了 §2.1 的偶发：测试与真实运行态共用一个日志/状态目录。

---

## 3. 预检探针包（已跑通，31/31 PASS）

**位置**：`.work/probe3/preflight.py`（gitignored，不入库）
**运行**：`python .work/probe3/preflight.py`（约 25 s）

设计约束（吸取二审误清 `.uh` 的教训）：脚本在 import kernel **之前**把所有存储 env 指向 `.work/probe3/tmp/`，并每次运行前清空该目录。

| 组 | 覆盖 | 结果 |
|---|---|---|
| A 鉴权矩阵 | `/api/v1/*` 无凭据 401；**遗留别名 `/plugins`、`/memory/export`、`/memory/short-term` 均 401**；ops 200；正确 token 201；`?token=` 回退 200 | 9/9 PASS |
| B 单设生效 | 只设 `UH_AUTH_REQUIRED=1`（LOCAL_MODE 缺省）→ 401 | PASS |
| C WS 实时流 | 带 token 建任务收 `ids=[1,2,3]`（非仅回放）；`after_event_id=3` 重连得 `[4..44]` 无空洞无重复；终态送达 | 4/4 PASS |
| D 工具确认 inline | `tool.call → confirmation_required` 顺序；approve → `tool.result → task.finished`；reject → `task.cancelled(confirmation_rejected)`；超时 → `resolved_by=timeout` + `cancelled(timeout)` | 4/4 PASS |
| E 取消语义 | 返回 `data.status=cancelling`；**取消闭合未完成 step**（`step.failed` 补齐）；终态 cancel/resume → 409 | 5/5 PASS |
| F 超窗回退载体 | `/steps` 含 `task_id`；`seq` 对齐 `step.started`（=2） | 2/2 PASS |
| G 幂等与 Cookie | 同客户端同键回放同 task；**鉴权模式下同键跨客户端不复用**；响应无 token Cookie | 3/3 PASS |
| H 隐私 | L3 内容 `override=L1/L2` 仍拒入；L1 正常入向量库 | 3/3 PASS |

**这张表的意义**：三审不必从零摸路径——A/B/C/D/E/F/G/H 的主干已经确认可跑通；三审的价值应放在 **§5 的开放问题** 与 **§4 的未覆盖面**。

---

## 4. 我上轮（二审）发现的当前状态

| 二审编号 | 内容 | 现状（本轮实测/证据） |
|---|---|---|
| R1 | 鉴权前缀白名单致别名绕过 | ✅ 已修（A4 三条别名全 401） |
| R2 | 401 缺 `meta.trace_id` | ✅ 已修（A2：`{'trace_id': 'uh_trc_…'}`） |
| R3 | `/steps`、`/cancel` 形状 + 409 登记 | ✅ 已修（F1 `task_id` 在；E1 `status=cancelling` 在；openapi 已有 409） |
| R4 | errors.md 计数 13 → 14 | ✅ 已修（标题已回写「14 个」） |
| R5 | dev 门禁 fail-open | ✅ 已修（默认关；未设 `UH_DEV_MODE` 时探针调 `/dev/tool-task` 直接拿不到 data） |
| R7 | 取消不闭合 step | ✅ 已修（E2：`step.failed` 补齐，unclosed=∅） |
| R8 | `steps()` 读 5 min 裁剪缓冲 | ⚠️ 未变（仍读内存 `event_buffer`）→ 见 §5-4 |
| R9 | 幂等并发竞态 | ⚠️ 已加 in-flight 锁（代码可见），**并发实测未做** → 见 §5-5 |
| R11 | ADR-003「插件可覆盖模式」 | ✅ 已补 `context.error_mode`（commit 6ef4303） |
| R12 | 事件发射器 15/18 | ⚠️ 仍缺 `task.yielded` + `safety.*`×3（`kernel/` 内只有映射表，无发射点）→ 见 §5-6 |

---

## 5. 三审仍需挖的开放问题（建议按此分配精力）

1. **`stream_url` 形状与契约不一致（需裁决）**：现返回相对路径 `/api/v1/stream?task_id=…`（commit be202ab「不信任 Host 头」），而 `task-flow.md` §1 示例是 `wss://<host>/api/v1/stream?task_id=…`。安全动机成立，但契约未同步。建议：契约补一句「相对路径，客户端按自身 scheme/host 拼接；部署可设 `UH_PUBLIC_BASE_URL` 输出绝对地址」。
2. **resume 的 404/409 判定顺序**：`resume()` 先查 checkpoint 再查终态，因此**无 checkpoint 的 chat 任务**取消后 resume 得 404 而非 409（workflow 终态得 409，已实测）。若要求「任何终态任务一律 409」，需把终态 guard 前移。
3. **本地模式幂等跨客户端复用是有意设计**（`caller` 仅在 `auth_required` 时进 key）。三审必须按「生产鉴权模式」验证，否则会误判；本地/单用户语义建议写进 task-flow 或 errors.md 的说明里。
4. **超窗回退的真实完整性**：`/steps` 仍取自 5 min 裁剪的 `event_buffer`。长任务（>5 min）超窗后 steps 只能重建最近窗口，「全量」名不副实。三审建议用一个 >5 min 的构造任务实测，而不是改 buffer 时间戳。
5. **幂等 in-flight 锁的并发实效**：锁在 `app.state.idempotency_locks`，需确认：同键并发两请求是否真只执行一次（副作用计数）；锁字典是否会随 cache 一起回收（否则又一处无界增长）。
6. **事件字典完整性**：15/18。要么补 `safety.*`/`task.yielded` 的发射点，要么在 events.md §3 明确标「v0.1.0 预留、无发射器」。否则 M1-9「18 事件端到端」仍是 fixture 口径。
7. **门禁默认语义相反（部署风险）**：`UH_LOCAL_MODE` 缺省时——REST 鉴权视为「本地 → 免鉴权」，dev 端点视为「非本地 → 关闭」。生产若漏配 `UH_AUTH_REQUIRED`，服务是**无鉴权**的；建议 deploy-hardening 加一条启动自检（非本地监听 + 未配 token → 拒绝启动或大声 WARN）。
8. **服务器侧门禁未复现**：`142/142（本地+服务器）` 的「服务器」部分本轮未验；建议三审在 `ssh ipc` 上复跑一次 pytest/verify-all（脚本 `scripts/server-deploy.ps1`）。
9. **CI 覆盖面**：`.github/workflows/contracts.yml` 已跑 pytest + verify-all，但未跑 `verify-all --full`（swagger/asyncapi 官方解析器），本地 --full 8/8 与 CI 6/6 存在口径差。
10. **Worktree 卫生**：`docs/reviews/` 已入库（含各模型报告），三审报告请按 brief 0010 §报告契约命名，避免与既有 11 份文档混名。

---

## 6. 三审执行清单（照抄即可）

```bash
# 0) 锁定基线
git log --oneline -1 && git status --short          # 期望 8a7d1c5 + 空

# 1) 门禁（本地）
python -m pytest -q                                 # 期望 142（§2.1 有 1 例 flaky）
node scripts/verify-all.mjs --full                  # 期望 8/8

# 2) 预检探针（31 项，隔离存储）
python .work/probe3/preflight.py                    # 期望 31/31

# 3) 需要人眼判断的补充项
#    §5-1 stream_url 契约裁决、§5-4 >5min 超窗、§5-5 并发幂等、§5-8 服务器复跑

# 4) 服务器（可选但建议）
ssh ipc "cd D:\\uh && python -m pytest -q"
```

**证据归档**：每条结论必须带 `file:line` 或命令输出；判定分「已修 / 部分 / 未修」三档，并标注是「生产链路可观测」还是「fixture/单测口径」。

---

## 7. 报告模板（brief 0010 已定，此处只列骨架）

```markdown
# M1 三审验收报告 · <模型名>
> 基线 cb4d1e6 · 审查时间 · 模型/provider/effort · 独立性声明（未读其他三审报告）

## 0 门禁复现（亲测，含 flaky 说明）
## 1 二审逐项验收判定（表：编号 → 判定 → 证据 file:line/命令）
## 2 生产链路 8 项逐条结论（brief 0010 §验收范围 1–9）
## 3 新发现（按严重性；file:line + 复现命令）
## 4 探针副作用记录（须声明存储隔离路径）
## 5 验收结论：通过 / 有条件通过 / 不通过 + 签署模型名
```

---

## 8. 结论

**就绪**。基线干净、门禁可复现（1 例已知 flaky 已定性）、探针包 31/31 可直接复用、二审 R1–R5/R7 已实测确认关闭。三审的增量价值集中在 §5 的 10 个开放问题，尤其：**stream_url 契约裁决、超窗回退真实完整性、服务器侧复跑、鉴权缺省语义**。
