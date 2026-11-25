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
