# M1 三审验收报告 · DeepSeek-V4-Flash

| 项 | 内容 |
|---|---|
| 验收对象 | M1 能力期（M1-1 ~ M1-9），基线 commit **cb4d1e6** / tag **m1-review2-fixed-v0.1.0** |
| 实际审查点 | HEAD `8a7d1c5` = 基线 + `briefs/0010-m1-third-review-acceptance.md`（仅 53 行 brief，无代码变更） |
| 审查时间 | 2026-11-18 18:12 ~ 19:05（Asia/Shanghai） |
| 签署模型 | **deepseek-v4-flash**（provider `deepseek-official`，reasoningEffort `high`；DSH 记账名 `deepseek-flash`）· preset `standard` |
| 独立性 | 未阅读 GPT-6 / Hunyuan-Hy 的三审材料；结论独立从「代码 + 契约 + 运行时」得出；仅将一审/二审自身报告与修复回应作为待验基线 |
| 探针隔离 | 全部探针在 import kernel 前把 `UH_*_STORE` / `UH_CHECKPOINT_*` 指向 `.work/probe3/tmp*`，未写入仓库 `.uh/`（详见 §5） |
| 结论 | **有条件通过**（4 项条件，见 §4） |

---

## 0. 门禁复现（亲测）

| 门禁 | 命令 | 结果 |
|---|---|---|
| 单元测试 | `python -m pytest -q` | **142 passed**（共跑 8 次：7 绿 / 1 红，红例见 N5） |
| 契约门禁（fast） | `node scripts/verify-all.mjs` | 6/6 PASS |
| 契约门禁（full） | `node scripts/verify-all.mjs --full` | **8/8 PASS**（含 swagger-cli OpenAPI 3.1、@asyncapi/parser 2.6） |
| 新增回归 | `pytest tests/unit/test_review_fixes.py -q` | 18 passed |
| 生产链路探针（本报告） | `.work/probe3/{preflight,section2,section3}.py` | **31/31 + 21/21 + 11/11**（判据见表） + 检索 E2E 1 项 |

> 说明：142 这个数字**可复现**，但不是无条件稳定——`test_reset_cleans_logs` 存在偶发失败（8 次中 1 次），已在 N5 定性并给出低成本修法；不因此否决门禁，但验收结论不写"无条件 142"。

---

## 1. 二审逐项验收判定

| 编号 | 内容 | 判定 | 证据（本轮实测） |
|---|---|---|---|
| R1 | 鉴权前缀白名单致别名绕过 | **已修** | 无凭据扫描：`/plugins`、`/memory/export`、`/memory/short-term` 全 401；鉴权条件已改为"全路径 + 豁免白名单"（`main.py:241`） |
| R2 | 401 缺 `meta.trace_id` | **已修** | 401 体 `{"data":null,"meta":{"trace_id":"uh_trc_…"},"error":{code:UH-USR-1001}}`（`main.py:246-249`） |
| R3 | `/steps`、`/cancel` 形状 + 409 登记 | **已修** | `/steps` 含 `task_id`；`/cancel` 返回 `status=cancelling`；openapi 已含 409 |
| R4 | errors.md 计数 13→14 | **已修** | `errors.md:20`「有效错误码全表（14 个）」 |
| R5 | dev 门禁 fail-open | **已修** | 缺省（未设 `UH_DEV_MODE`/`UH_LOCAL_MODE`）时 `/dev/tool-task`、`/dev/confirmations`、`/dev/capability/*`、`/dev/route` 全 404 |
| R7 | 取消不闭合 step | **已修** | 取消事件序列 `task.created, step.started, delta…, step.failed, task.cancelled`，未闭合 step 集合为空 |
| R8 | `steps()` 读 5 min 裁剪缓冲 | **未变** | 仍是内存 `event_buffer`（`tasks.py:182`）；超窗后"全量重建"对 >5 min 任务不成立 → 见 N4 |
| R9 | 幂等并发竞态 | **已修** | 同键并发 2 请求只产生 1 个 task（in-flight 锁，`main.py:255-266`）；并已绑 body 哈希与调用者 |
| R11 | ADR-003「插件可覆盖模式」 | **已修** | `module_error_code(mode=None)` 支持调用方覆盖（`protocol.py:79-84`） |
| R12 | 事件发射器 15/18 | **未变** | `safety.check_started/finished/denied`、`task.yielded` 仍只有映射表无发射点 → N6 |
| 一审 P1-2 | USR-1002 detail 不符契约 | **已修** | 现含 `confirmation_required` + `risk_level`（`mcp.py:531-541`） |

---

## 2. 生产链路 9 项逐条结论（brief 0010 §验收范围）

| # | 项目 | 结论 | 证据 |
|---|---|---|---|
| 1 | 门禁复现 | ✅ | 142 / 6-6 / 8-8（§0） |
| 2 | 鉴权（含别名、单设生效、401 trace） | ✅ | 无凭据 GET/POST 全量扫描 **0 泄漏**；ops 200；只设 `UH_AUTH_REQUIRED=1` 即生效；token 201；`?token=` 回退 200 |
| 3 | WS 实时流 + 重连 + 换 token 归属 | ✅ / ⚠️局限 | 带 token `ids=[1,2,3]` 实时；`after_event_id=3` 重连续 `[4..44]` 无空洞无重复；终态 close 1000。**归属**：共享 `UH_AUTH_TOKEN` 下他人 client 仍可读（200）——brief 已知局限，登记为 N7 |
| 4 | 工具确认 inline | ✅ | `tool.call → confirmation_required → resolved → tool.result → finished`；reject → `cancelled(confirmation_rejected)`；超时 → `resolved_by=timeout` + `cancelled(timeout)` |
| 5 | 工具确认 RPC fail-closed | ✅ | RPC 路径无确认通道 → `UH-USR-1002` 且 mock 工具执行次数 0；调用方传 `risk_level="read-only"` 试图降级 → 仍 fail-closed |
| 6 | 取消 | ✅ | 返回 `status=cancelling`；闭合 step；终态 cancel/resume → 409；**取消后旧令牌 400 不可再批准**，无 `tool.result` 执行痕迹 |
| 7 | 超窗回退 | ✅ / ⚠️ | 窗口失效 → `{"op":"replay_unavailable","reason":"window_expired"}` + close 4408；`/steps` 200 且含 `task_id`。局限见 N4（窗口为构造失效，非真等 5 min） |
| 8 | 隔离 / 隐私 | ✅ / ⚠️ | 命名空间重复声明被拒；跨模块访问 `LocalVectorStore` 被拒；L2 加密且 `docs=0`；L2 语义召回为空、授权元数据过滤可解密；L3 内容 `override=L1/L2` 仍拒入；无痕 `memory_recall=False` 不写。**Chroma/LlamaIndex 后端未覆盖** → N1 |
| 9 | UI | ⚠️ 静态核对 | 确认弹窗调用 `/confirm`、游标重连、4408→`/steps`、degraded 徽标、4401/4403 文案均存在（`ui/index.html`）。**未做浏览器实测**（本机无 WebDriver 会话），结论按静态核对口径 |

补充：检索链路（LocalVectorStore + StorageGuard 真实接线）端到端可用——`retrieval.search` 返回 `degraded=False, docs=1, source=builtin:uh-demo.md`。

---

## 3. 新发现（本轮）

### N1（P2·brief 点名检查项）StorageGuard 未覆盖 Chroma / LlamaIndex 后端

- `ChromaVectorStore.add/query` 在 `self._chroma is not None` 时**不调用** `_guard_access`（`retrieval.py:156-186`）；`LlamaIndexChromaStore` 继承同一路径。
- `RetrievalService.__init__` 在 `backend=chroma/llamaindex` 分支**不传 guard**（`retrieval.py:224-225`），且 `ChromaVectorStore.__init__` 在 chroma 可用时跳过 `super().__init__`，连 `self._guard/_module/_namespace` 都不存在——将来任何 `_guard_access()` 调用会 AttributeError。
- 影响：默认 `backend=local` 时不触发；一旦按 ADR-001 切 Chroma/LlamaIndex（目标部署），存储隔离**静默失效**。
- 建议：三条分支统一 `ChromaVectorStore(guard=guard)`、在构造里先 `super().__init__` 或显式赋值，并在 add/query 开头调 `_guard_access()`。

### N2（P2·契约）`stream_url` 相对路径与 task-flow §1 不一致

- 实现：`/api/v1/stream?task_id=…`（相对，防 Host 头注入，`main.py:483-485`）。
- 契约：`task-flow.md` §1 示例为 `wss://<host>/api/v1/stream?task_id=…`。
- 建议：契约补「相对路径，客户端按自身 scheme/host 拼接；服务端可设 `UH_PUBLIC_BASE_URL` 输出绝对地址」，或实现按可信 base 输出。

### N3（P2·契约）resume 的 404/409 判定顺序

- `resume()` 先查 checkpoint 再判终态（`tasks.py:337-345`），故**无 checkpoint 的 chat 任务**取消后 resume 得 404；workflow 终态得 409（已实测）。
- 建议：终态 guard 前移，使"任何终态任务 resume → 409"一致；或在 task-flow §4 明确"无 checkpoint 优先 404"。

### N4（P2·契约）`/steps` 不是全量载体

- `steps()` 读 `event_buffer`，而 running 任务缓冲按 300s 裁剪（`tasks.py:182-190`、`tasks.py:256`）。
- 影响：>5 min 任务的超窗回退只能重建最近窗口；task-flow §5.4「全量」措辞与实现不符。
- 建议：steps 落独立 append-only 存储，或把契约改为「回退最近窗口 + 终态」。

### N5（P2·工程质量）测试污染运行态 + 1 例 flaky

- `tests/unit/conftest.py` 只设 `UH_DEV_MODE=1`，未隔离存储 → 每次 `TestClient(create_app())` 都写真实 `.uh/`（本次审查后：`long_term_memory.json` 348 条、`checkpoints.sqlite3` 1.1 MB）。
- flaky：`test_reset_cleans_logs` 8 次跑 1 次红；机制为 `dev_up` 把 `.uh/kernel.log` 的句柄交给 uvicorn 子进程，上一用例子进程未完全退出时会在 unlink 后重建文件。
- 建议：autouse fixture 把 5 个存储 env 指到 `tmp_path`；该用例 monkeypatch `cli.LOG_FILE/STATE/PID_FILE`。

### N6（P3）事件字典 15/18

`safety.check_started / check_finished / denied`、`task.yielded` 无运行时发射点（`kernel/` 内仅 `_source_action` 映射）。建议补发射点或在 `events.md §3` 明确标「v0.1.0 预留、无发射器」，否则「18 事件端到端」仍是 fixture 口径。

### N7（P3，登记）共享 token 下归属守卫退化为 token-only

`owner_matches` 在 `owner_token` 存在时只比 token（`tasks.py:398-407`），单共享 `UH_AUTH_TOKEN` 部署下"他人 client 读任务"成立（实测 200）。属 brief §已知设计局限（需 per-user principal），M3 前登记。

### N8（P3）CI 与本地门禁口径差

CI 跑 `verify-all`（6 项），未跑 `--full`（swagger/asyncapi 官方解析器）；本地 8/8 与 CI 6/6 不等价。建议 CI 增加 `--full` 步骤（需网络，可设失败不阻断的 nightly）。

### N9（P3）长期记忆存储未接 guard

`long_term_memory.py:152` 构造 `LocalVectorStore(...)` 未传 `guard/module/namespace`；虽为自有命名空间，但「所有模块存储路径都过隔离」口径下仍是缺口。

### N10（P3，正面项需确认）`/docs`、`/openapi.json` 在鉴权模式下一并 401

比预期更严（信息面收紧），但会阻断 SDK 生成/文档站流程。请确认是有意为之并写进 deploy-hardening。

---

## 4. 验收结论：**有条件通过**

**通过依据**：门禁 142 + full 8/8 亲测可复现；二审 R1–R5、R7、R9、R11 与一审 P1-2 全部实测确认关闭；生产链路 9 项中 6 项完全通过、3 项"通过但带登记局限/静态口径"；未发现新的可绕过鉴权、可越权执行高危工具、可跨模块读存储的**高危**路径。

**四项条件（建议 M2 开工前完成）**

1. **N1**：Chroma/LlamaIndex 后端接入 `StorageGuard`（brief 点名项，目标部署形态）。
2. **N2**：`stream_url` 与 `task-flow §1` 二选一对齐（改实现或改契约，走只增/修订对照）。
3. **N4**：`/steps` 全量语义落定（持久化 or 契约措辞）。
4. **N5**：测试存储隔离 + flaky 用例解耦（否则门禁的可信度会持续被偶发红侵蚀）。

**登记不阻断**：N3、N6、N7、N8、N9、N10。

**签署**：`deepseek-v4-flash` · 2026-11-18

---

## 5. 探针副作用记录

- 三组探针（`preflight.py` / `section2.py` / `section3.py`）均在 import kernel 前设置：
  `UH_LONG_TERM_STORE`、`UH_LONG_TERM_VECTOR_STORE`、`UH_RETRIEVAL_STORE`、`UH_RETRIEVAL_CHROMA`、`UH_CHECKPOINT_SQLITE_PATH`、`UH_CHECKPOINT_JSON_PATH` → `.work/probe3/tmp*/`，运行前清空该目录。
- **未对仓库 `.uh/` 发起任何写/删操作**；`.uh/` 的变化全部来自 `pytest` 自身（N5 所述既有问题）。
- 未修改任何受版本控制文件；本报告为唯一新增文件。

---

## 附录 · 复现命令

```bash
git log --oneline -1            # 8a7d1c5
python -m pytest -q             # 142（N5：偶发 1 红）
node scripts/verify-all.mjs --full   # 8/8

python .work/probe3/preflight.py     # 31/31  鉴权/WS/确认/取消/steps/幂等/隐私
python .work/probe3/section2.py      # 21/21  RPC fail-closed/令牌失效/超窗/StorageGuard/L2/无痕/dev 门禁/并发/checkpoint
python .work/probe3/section3.py      # 11/11  鉴权全量扫描/401 形状/UI 静态
```
