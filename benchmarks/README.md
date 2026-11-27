# UH! 基准测试报告（M3-9 / #36）

> **结论：100 并发下 SLO 未达标。** 本报告如实记录实测数据与瓶颈分析，不做美化。
> 采集日期：2026-11-25 · 工具：`uh bench run`（kernel/bench.py）· schema `uh-bench/v1`

## 1. SLO 定义
| 指标 | 目标 | 来源 |
|---|---|---|
| 错误率 | **< 1%** | Issue #36 验收标准 |
| P95 延迟 | **≤ 2000ms** | 默认 SLO（可 `--slo-p95-ms` 覆盖） |

测量口径：**请求提交延迟**（建会话 + 发消息到 202 返回），不含后台任务完成时间。

## 2. 实测结果（家庭服务器 192.168.0.28:8080）

| 场景 | 并发 | 请求 | 错误率 | P50 | P95 | P99 | 错误率判定 | P95 判定 |
|---|---|---|---|---|---|---|---|---|
| chat | 100 | 200 | 1.50% | 4338.58ms | 16065.58ms | 18216.12ms | ❌ FAIL | ❌ FAIL |
| orchestrate | 100 | 200 | 18.00% | 14829.25ms | 62635.27ms | 64495.11ms | ❌ FAIL | ❌ FAIL |
| search | 100 | 200 | 0.00% | 12196.49ms | 55776.24ms | 59787.59ms | ✅ PASS | ❌ FAIL |

**全部场景 P95 超标 8~31 倍**；chat/orchestrate 错误率亦超标。

### 硬件基线（数字可比性依据）
**被测服务端**：Win10 22H2 工控机 DESKTOP-AOI7835，**4 核 / 7.9G RAM**，Python 3.12.10，
单进程 uvicorn（`--workers` 未设），LLM provider = **mock**（无真实 API 延迟），检索 local 后端。

**压测客户端**：Windows-11-10.0.26200-SP0，16 核 / 15.23G，Python 3.14.3

> ⚠️ **客户端与服务端硬件不同**。报告中 `client_baseline` 与 `server_baseline` 分列，
> 引用数字时必须以服务端基线为准（Issue 风险项：无硬件基线的性能数字不可比）。

## 3. Kylin 对照实测（8 核 / 30G，Linux）

| 场景 | 并发 | 错误率 | P50 | P95 |
|---|---|---|---|---|
| chat | 100 | 0.00% | 4626ms | 17435ms |
| search | 100 | 0.00% | 5384ms | 33881ms |
| orchestrate | 100 | 0.00% | 7389ms | 51268ms |

**关键发现：Kylin 硬件强 2 倍（8核30G vs 4核7.9G），P95 反而更差。**
→ **瓶颈不是硬件，是单进程事件循环 + 后台任务抢占**。

## 4. 瓶颈分析

### 4.1 单进程 uvicorn（主因）
两台机器均单 worker。100 并发请求 + 每请求触发的后台任务（LLM 流式、检索、编排）
共享同一事件循环，**提交路径被后台协程饿死**。加 CPU 无用（GIL + 单循环）。

### 4.2 mock provider 逐字符 sleep
`MockLLMClient.stream_chat` 每字符 `await asyncio.sleep(0.005)`，约 45 字符 = 225ms/任务。
100 并发 → 100 个长驻协程持续占用循环调度，放大排队。
**注意：这是开发用 mock，真实 LLM 的 IO 等待会让出循环，实际表现可能不同**——
故本报告的绝对数值仅对 mock 后端有效，不可外推到生产 LLM。

### 4.3 场景差异
`search`/`orchestrate` 比 `chat` 慢 3~4 倍：检索走文件扫描、编排走多步 workflow，
单请求占用循环时间更长。

## 5. 达标路径（建议，未实施）
| 措施 | 预期效果 | 代价 |
|---|---|---|
| **uvicorn 多 worker**（`--workers 4`）或 K8s replicas≥3 | 提交路径并行，P95 数量级下降 | 需解决 session/task 状态跨进程共享（当前 in-memory dict） |
| 后台任务移出请求进程（独立 worker + 队列） | 提交与执行解耦 | 架构改动，M3+ |
| 检索加索引 / 编排 checkpoint 异步化 | 缩短单请求占用 | 中等 |
| 真实 LLM 替代 mock 重测 | 得到生产可信数字 | 需 API 额度 |

> **当前状态诚实结论**：v0.1.0 在**低并发（≤10）**下功能与延迟正常（实测 5 并发 P95 ≈186ms，0% 错误）；
> **100 并发 SLO 未达标**，需上述架构改进后重测。#36 的「压测能力」已交付，「达标」未达成。

## 6. 复现命令
```bash
# 低并发冒烟（应 PASS）
uh bench run --scenario chat --concurrency 5 --requests 10 \
  --base-url http://192.168.0.28:8080 --token "$UH_AUTH_TOKEN"

# 100 并发正式（当前 FAIL，见上表）
uh bench run --scenario chat --concurrency 100 --requests 200 \
  --base-url http://192.168.0.28:8080 --token "$UH_AUTH_TOKEN" --timeout 60

# 自定义 SLO
uh bench run --scenario search --concurrency 50 --slo-p95-ms 5000 --slo-error-rate 0.02

# CI 消费（JSON）
uh bench run --scenario chat --concurrency 100 --json
```
退出码：`0` SLO 达标 / `1` SLO 未达标 / `2` 参数或连接错误。

## 7. 成本分层模板
`deploy/cost-tiers/{light,standard,enterprise}.env.example`：
| 档位 | LLM | 检索 | 编排 | 追踪采样 | 并发预期 |
|---|---|---|---|---|---|
| light | 本地 Ollama 3B | local | memory | 关 | ≤10 |
| standard | gpt-4o-mini | Chroma | PostgreSQL | 0.1 | ≤50 |
| enterprise | gpt-4o | Chroma top10 | PostgreSQL HA | 1.0 | 100+（需扩容） |

密钥全为 `${ENV}` 引用，无明文。

## 8. 原始数据
本目录 `*.json`（schema `uh-bench/v1`）含每请求延迟样本、SLO 判定、双端基线，可复核。


---

## 9. 架构改进实测（2026-11-25 追加）

按 §5 达标路径实施了四项**不改契约**的优化，逐项实测：

| # | 优化 | 位置 | 效果 |
|---|---|---|---|
| 1 | **同步 MCP handler 移入线程池** | `kernel/mcp.py::_dispatch` | search P95 **55.8s → 11.6s**（4.8×）。原实现 `self.handler(request)` 同步直调，检索的 embedding/余弦纯 CPU 全在事件循环里跑 |
| 2 | **embedding LRU 缓存** | `DeterministicEmbedding.embed` | 消除重复查询的 SHA256+归一化重算（`UH_EMBEDDING_CACHE` 默认 512） |
| 3 | **倒排索引预筛** | `LocalVectorStore.query` | 只对含查询词的文档算余弦，替代全量 O(N×128维)；无命中退回全量保召回语义 |
| 4 | **delta 不进内部总线** | `TaskManager._emit` | hooks 订阅 `"*"`，原实现每 token 一条 delta 都 publish → 45×100=4500 次钩子队列洪泛。chat P95 **21.6s → 7.7s**（2.8×）。delta 仍推 SSE/WS，外部契约不变 |
| 5 | **event_buffer 裁剪节流** | 同上 | 原每条事件全量重建 buffer（O(n)），高频下退化 O(n²)；改按计数节流 + 从头裁剪 |
| 6 | **可配线程池** | `create_app` startup | `UH_THREAD_POOL_SIZE`（默认 max(32, cpu×4)）。**注意：GIL 下加大线程池对 CPU 密集任务无益，实测反而加剧争抢** |

### 错误率成果（稳定达成）
| 场景 | 优化前 | 优化后 |
|---|---|---|
| chat | 1.5% | **0%** ✅ |
| search | 0% | **0%** ✅ |
| orchestrate | 18% | **0%**（隔离测）✅ |

**错误率 SLO（<1%）已达标。**

### P95 仍未达标，且测量噪声大
同一配置多次实测 chat P95 在 **7.7s ~ 16.3s** 间波动（2 倍差异）。原因：
- 被测机是 **4 核 / 7.9G Win10 工控机**，后台进程与磁盘 IO 干扰显著
- 连续压测导致状态累积，需每次重启才可比
- **该硬件不适合产出稳定基准数字**

### 关键诊断证据（分层隔离实测，100 并发）
| 路径 | P95 |
|---|---|
| `GET /health`（纯 HTTP） | **341ms** |
| `POST /sessions`（建会话，不发消息） | **2063ms** |
| 完整 chat（含后台任务） | 7700~16300ms |

→ **HTTP 层与提交路径本身没问题，瓶颈在发消息后的后台任务占用事件循环**。

### 未实施的根治方案（需架构改动）
**多进程 worker** 是唯一能突破 GIL 的路径，但受阻于：
1. `app.state` 有 **37 处依赖**，`TaskManager.tasks` 是内存 dict 且含 `asyncio.Queue`（不可跨进程）
2. Windows 下 `uvicorn --workers` 走 multiprocessing spawn，`-m uvicorn` 入口不支持（实测起不来）
3. 需先把 session/task 状态迁到 Redis 共享 + 任务队列解耦，属 M3+ 工程量

### 诚实结论
- ✅ **压测能力**交付（CLI + SLO + 双端基线 + 可复现报告）
- ✅ **错误率 SLO 达标**（三场景 0%）
- ✅ **P95 改善 3~7 倍**（55.8s → 7.7s 最优值）
- ❌ **P95 ≤2000ms 未达标**，根治需多进程 + 状态外置（M3+）
- ⚠️ 当前硬件不足以产出稳定基准；建议在专用 Linux 压测机重测

> 所有优化均通过 **392 passed + verify-all 14/14 + secret 0**，契约与外部行为不变。


---

## 10. 多进程 + 状态外置（2026-11-25 追加，Kylin 实测）

实施 §5 根治方案的第一阶段：**会话归属外置 Redis + 集群共享签名密钥 + 多 worker**。

| 场景 | 错误率 | P50 | P95 | 对比单进程 P95 |
|---|---|---|---|---|
| chat | **0%** | 2641ms | **5615ms** | 7.7~16.3s → **5.6s** |
| search | **0%** | 2505ms | **5762ms** | 11.6~55.8s → **5.8s** |
| orchestrate | **0%** | 2672ms | **5698ms** | 25.6~62.6s → **5.7s** |

**成果**：
- 错误率稳定 0%（原 0~45% 波动）
- **P95 改善约 10 倍**，且**波动消失**（多进程让测量终于可比）
- 8 worker 无进一步改善 → CPU 并行度已不是瓶颈

**剩余瓶颈已精确定位**：单请求 submit 仅 **6ms**，但任务完成 **5977ms**；
而组件合计仅 513ms（LLM 312 + 召回 41 + 写入 160）。**差 5.5s 在任务执行路径内，未定位**。

详见 [多进程部署手册](../docs/deployment/multiprocess.md)。


---

## 11. 证据链补齐（独立审查 P1-2 修复）

Hunyuan 审查指出两处失真，均已修正：

### ① server_baseline 曾无硬件字段
原 `fetch_server_baseline` 只取 `/health` 与 `/metrics`，产出仅
`{health_status, kernel_version, metrics_families}`——**服务端 CPU/内存/平台从未采集**，
使「双端硬件基线、数字可比」承诺不成立。

**修复**：新增 `GET /api/v1/bench/baseline`（非契约端点，`include_in_schema=False`），
返回服务端 `hardware_baseline()`；`fetch_server_baseline` 采集并合并。
实测服务端字段齐全：`platform / machine / python / cpu_count / memory_total_gb /
llm_provider / llm_model / retrieval_backend / doc_corpus_md_files`。

### ② 最终数据组未随包
此前 README 引用的 6.7~7.2s 那组无原始 JSON，「改善 8~9 倍」不可复核。

**修复**：`benchmarks/kylin-4worker/` 随包提交三份原始 JSON（含每请求样本 + 双端基线）：

| 场景 | 错误率 | P50 | P95 | P99 | 服务端基线 |
|---|---|---|---|---|---|
| chat | **0%** | 4407ms | **7420ms** | 7999ms | Kylin 8 核 / 29.38GB |
| search | **0%** | 4445ms | **7321ms** | 7940ms | 同上 |
| orchestrate | **0%** | 3396ms | **6854ms** | 8090ms | 同上 |

> 采集时间 2026-11-25T12:20Z · 4 worker + Redis · mock delay 5ms · UH_TASK_CONCURRENCY=4
> 与 §10 表格（6.6~7.2s）为**同配置不同轮次**，波动约 ±0.6s，属正常范围。
> 复核命令见各 JSON 的 `client_baseline` / `server_baseline` 字段。

### 仍未达标
P95 ≤2000ms 目标未达成（6.85~7.42s）。根因与达标路径见 §5 与
[延迟根因诊断](../docs/reviews/2026-11-25-延迟根因诊断.md)。


---

## 12. v0.2.0 延迟归因与已修项

目标 P95 ≤2000ms。本轮做了归因，修掉两个真缺陷，但**目标仍未达成**，如实记录如下。

### 已修 ①：存储写入 O(n²) → 追加式（26 倍）

单任务完成耗时 2572ms 中，长期记忆写入占大头。拆解后真正的瓶颈既不是加密也不是
事件广播，而是**每次写入都全量重写整个 JSON 文件**：

| 操作 | 修复前 | 成因 |
|---|---|---|
| `vector_store.add` | 190ms/次 | 1332 文档全量序列化 + 落盘 |
| `long_term_memory._save` | 11ms/次 | 280 条目全量序列化 |

成本随存量线性增长，总成本 O(n²)；并发越高争用越重（GIL 下无法并行）。

改为 JSONL 追加（O(1)），`_load` 兼容旧数组格式，同 id 后写覆盖前写。

**本机实测（单 worker，mock delay=0）**：

| 指标 | 修复前 | 修复后 | 倍数 |
|---|---|---|---|
| 单次 write | 203ms | 7.7ms | 26× |
| 单任务完成 | 2572ms | 178ms | 14× |
| 并发 10 p95 | 4429ms | **1112ms** | 4×（进入 SLO） |
| 并发 50 p95 | 15226ms | 4412ms | 3.5× |
| 全量测试耗时 | 195s | 145s | — |

### 已修 ②：多 worker 从未真正生效

Kylin 上 `UH_WORKERS=4` 时 `ps -ef | grep '[k]ernel.serve'` **只有 1 个进程**。
所有标注「4 workers + Redis」的历史压测实际都是**单 worker** 跑出来的。
这也解释了此前两个反常现象：p95 长期停在 6.7~7.4s 降不下来；提高
`UH_TASK_CONCURRENCY` 反而更慢（单进程内 GIL 争用）。

原因是 `uvicorn.run(workers=N)` 的 Multiprocess 分支未真正拉起子进程。
改为显式 fork（父进程 bind 一次 socket，子进程继承 fd）。修复后 Kylin 实测
1 父 + 4 子，PPID 正确。Windows 无 `os.fork` 且 spawn 无法共享 socket，
明确退化为单进程并打印提示，不再按 workers 假装多进程。

### 仍未达标：真 4 worker 下 p95 仍 7.7~8.7s

2026-11-27 复测（Kylin 8 核 / 4 worker / Redis / 100 并发）：

| 场景 | 错误率 | P50 | P95 |
|---|---|---|---|
| chat | 0% | 3415ms | 7776ms |
| search | 0% | 6099ms | 8686ms |
| orchestrate | 0% | 4166ms | 7810ms |

**worker 数不是瓶颈**（真 4 worker 与单 worker 数字接近）。当前证据指向
**单任务耗时本身**：本机 mock 路径单任务 178ms，Kylin 同流程 p50 就 3.4~6.1s，
差一个量级。下一步需在 Kylin 上按「路由 / 检索 / 编排 / 记忆 / LLM」分段计时，
定位这 3~6s 的构成，而不是继续加 worker 或调并发闸。

> 在此之前，SLO 仍未达成；本文档不宣称达标。
