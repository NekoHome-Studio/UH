# 多进程部署（M3-9 状态外置）

> 目标：突破单进程 GIL 瓶颈，让 100 并发下 P95 可控。
> 实测环境：Kylin V10 SP1（8 核 / 30G），Redis 7.2.5 用户态编译，Python 3.12.16。

## 1. 为什么需要多进程
单进程 uvicorn 下，100 并发请求与各自的后台任务共享同一事件循环。
CPU 密集段（检索 embedding/余弦、记忆写入）会饿死请求提交路径。
**加线程无用**（GIL），实测加大线程池反而加剧争抢。

## 2. 状态外置范围（最小可行）

| 状态 | 处理 | 原因 |
|---|---|---|
| **会话存在性 + 归属** | **外置 Redis**（`kernel/session_registry.py`） | worker A 建的会话必须能被 worker B 校验，否则 USR-1003/1002 |
| **client_id 签名密钥** | **集群共享**（Redis SETNX 派生） | 每进程随机密钥会让 A 签发的凭据在 B 验签失败 → 403 |
| 任务对象（含 asyncio.Queue） | **仍进程内** | Queue 不可跨进程；任务粘在创建它的 worker |
| 短期记忆 | 已是 Redis | 天然共享 |
| 长期记忆 / 检索索引 | 文件后端 | 多进程读同一文件；写入需注意并发（当前 append-only 场景安全） |

### 关键实现
- `SessionRegistry`：async 接口，Redis 后端；**fakeredis 显式排除**（进程内不共享），无 Redis 回退本地 dict（单进程语义不变）。
- `_derive_shared_identity_secret()`：`UH_WORKERS>1` 时从 Redis `uh:cluster:client_identity_secret` 取/建（SETNX 保证只一个 worker 生成）。
- **未提供稳定密钥且无 Redis 时 fail-fast**（抛 RuntimeError），避免静默 403。

## 3. 启动方式

**必须用 `kernel.serve`（Python API），不要用 `-m uvicorn --workers N`** ——
实测 uvicorn 0.54.0 的 CLI 形式在本环境不真起子进程。

```bash
# 1) Redis（无 root 可用户态编译）
curl -sL -o /tmp/redis.tar.gz https://download.redis.io/releases/redis-7.2.5.tar.gz
tar xzf /tmp/redis.tar.gz -C /tmp && cd /tmp/redis-7.2.5
make -j8 MALLOC=libc BUILD_TLS=no && cp src/redis-server src/redis-cli ~/bin/
~/bin/redis-server --port 6379 --bind 127.0.0.1 --dir ~/uh/data/redis --save '' --appendonly no &

# 2) 内核多 worker
export UH_REDIS_URL=redis://127.0.0.1:6379
export UH_LOCAL_MODE=0 UH_AUTH_REQUIRED=1
export UH_AUTH_TOKEN_FILE=$HOME/uh/.auth-token
export UH_WORKERS=4 UH_HOST=127.0.0.1 UH_PORT=18080
export UH_THREAD_POOL_SIZE=16
python -m kernel.serve
```

验证 worker 真起来（**看 socket 而非 grep 父进程**）：
```bash
ss -ltnp | grep 18080   # 应看到 1 父 + N 子进程共享同一 socket
```

## 4. 实测结果（Kylin 8核30G，4 worker + Redis）

| 场景 | 错误率 | P50 | P95 | P99 |
|---|---|---|---|---|
| chat | **0%** ✅ | 2641ms | **5615ms** | 5890ms |
| search | **0%** ✅ | 2505ms | **5762ms** | 6100ms |
| orchestrate | **0%** ✅ | 2672ms | **5698ms** | 5981ms |

### 对比单进程
| 指标 | 单进程（家庭服务器） | 4 worker + Redis（Kylin） |
|---|---|---|
| 错误率 | 0~18%（波动） | **0%（稳定）** |
| P95 | 7.7~55.8s（波动 2 倍以上） | **5.6~5.8s（波动消失）** |
| 测量可比性 | 差（需每次重启） | **好** |

**8 worker 无进一步改善**（P95 仍 5.5s）→ 瓶颈已不在 CPU 并行度。

## 5. 剩余瓶颈（已定位未完成）

分阶段计时（单请求，无并发）：
```
session 创建 = 45ms
消息提交    = 6ms      ← 提交路径本身极快
任务完成    = 5977ms   ← 延迟全在这里
```

组件级计时：
```
mock LLM 流式 = 312ms（53 字符 × 5ms + 开销）
长期记忆召回  = 41ms
长期记忆写入  = 80ms × 2 = 160ms
合计 ≈ 513ms
```

**任务实际 6s vs 组件合计 0.5s，差 5.5s 未定位。**
已排除：hooks 队列（异步不阻塞）、任务队列（无界）、路由判定（正确走 chat）。
下一步应在 `TaskManager._run` 内部直接插桩（外部包裹实例方法未生效，疑协程捕获时机）。

## 6. 已知限制
- **任务粘性**：WS/SSE 订阅须落到创建任务的 worker。当前靠客户端重连 + 任务查询可能命中别的 worker → 生产需 sticky session（ingress affinity）或独立任务路由层。
- **幂等缓存仍进程内**（`app.state.idempotency_cache`）：跨 worker 重放检测失效，需外置 Redis。
- **长期记忆/检索文件后端**：多进程写入无锁，高并发写场景需改 Redis 或加文件锁。
- Windows 下 multiprocessing 走 spawn，`-m uvicorn --workers` 不支持；`kernel.serve` 有 `__main__` 保护可用。

## 7. SLO 结论
- ✅ **错误率 <1% 达标**（三场景 0%，且稳定）
- ❌ **P95 ≤2000ms 未达标**（5.6s，但从 55.8s 改善 **10 倍**）
- 剩余 5.5s 延迟源未定位，是下一步唯一攻坚点


---

## 8. K8s 多副本的客户端亲和（R2 审查 P0-3）

### 问题
`deploy/k8s/deployments.yaml` 的 `uh-kernel` 是 **replicas: 2**，而任务对象
（含 `asyncio.Queue`）与 `idempotency_cache` **仍是进程内状态**。
Service 若无亲和，请求随机落 worker：

| 操作 | 落到未持有任务的 worker |
|---|---|
| `GET /tasks/{id}` | **404** |
| `GET /tasks/{id}/steps` | **404** |
| SSE / WS 订阅 | 拿不到事件 |
| 同 `Idempotency-Key` 重放 | **各建一个任务**（幂等失效） |

双 app 实例实测确认：A 创建任务后 B 查询返回 404。约 **50% 概率**。

### 短期止血（已实施）
`uh-kernel` Service 加：
```yaml
sessionAffinity: ClientIP
sessionAffinityConfig:
  clientIP: { timeoutSeconds: 10800 }
```
并新增 J 项断言：**replicas ≥ 2 时必须有 ClientIP 亲和**，去掉即门禁 FAIL。

### 这是权宜，不是根治
- **经 ingress/网关时源 IP 会被改写**，ClientIP 亲和随之失效。
  该形态下需改用 ingress 层 cookie 亲和（如 nginx `affinity: cookie`）。
- 亲和只保证「同一客户端」落同一 worker；**客户端换 IP、Pod 重启、滚动更新**仍会丢任务。
- 长期记忆文件后端多进程写为 **last-write-wins**（非 append-only），并发写会丢数据。

### 根治路径（v0.2.0）
1. 任务记录外置 Redis（状态、步骤、事件流）
2. 事件经 pub/sub 回流，任意 worker 可订阅
3. 幂等缓存外置 Redis
4. 独立执行进程池，提交与执行解耦

**在此之前，不应把该清单宣称为「生产可用」**——它需要亲和配置且仍有上述边界。
