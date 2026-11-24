# UH! M3 生产期（进行中）独立审查报告

> **审查模型：Tencent Hunyuan Hy（腾讯混元 Hy）**
> 运行产品：WorkBuddy（Agent 模式）
> 审查日期：2026-11-24（09:58–10:35 GMT+8）
> 基线：HEAD **`3568349`**（M3-7 系统级安全加固），工作区干净；M3 已落地 M3-1~M3-7（#28~#34），**未完工 M3-8（#35 L2 加密/日志脱敏/供应链）、M3-9（#36 压测/SLO）**
> 方法：只读审查 + 本机实测 + 跨机 192.168.0.28:8080 核验；不改源码/契约/服务器配置，只新增本报告。
> 关联：briefs/0016~0022、deploy/**、kernel/{metrics,otel,scaler,collaboration,crewai_worker,sandbox,linux_sandbox}.py、docs/contracts/deploy-hardening.md、docs/reviews/2026-11-22-SBX-Linux-矩阵证据.md

---

## 0. 结论摘要

**M3 已落地的 M3-1~M3-7 工程质量不错，但存在 4 个生产阻断级问题，且跨机部署与测试门禁都还没到「生产可用」状态。**

门禁亲测：本机 **1 failed / 363 passed / 15 skipped**（声明 364 passed，失败仍是 Git fixture）；verify-all full **13/13 PASS**（I/J/K/L/M 全绿）；服务器 `server-test.ps1` **283 passed**——**服务器仍停在 M2 基线 `b9f46109…`，M3 七个模块（main/metrics/otel/collaboration/scaler/…）全未部署**，跨机 `/metrics` 只返回 2 个 M1 时代指标。这与 M2 验收时同一模式：代码已提交、服务器未跟上，任何跨机 M3 结论目前都不可信。

四个生产阻断项（详见 §3）：

1. **K8s 上 scaler 控制面通路是断的**：default-deny Egress 只放 DNS 与业务 Pod，无到 apiserver 的放行；文档要求运维自建同节点 TLS 入口但仓库无清单 → 0→1 协作永远 degraded。
2. **M3-3/M3-4 在 K8s 侧未落地**： observability 仅 Compose overlay；Prometheus 抓 `kernel:8080`（Compose alias，K8s 无此 Service）；default-deny 无 4317/4318 → K8s 生产可观测性为空。
3. **cgroup/pids.max 默认未启用**：`UH_SANDBOX_CGROUP_ROOT` 在 compose 与 K8s 清单均未注入 → fork 炸弹防护在生产部署落空，只有 ulimit 内存限制。
4. **协作释放是 fire-and-forget 且失败不重试**：scale(0) 失败只发 degraded，进程退出即副本永久停在 1；锁 TTL 45s 仅比释放超时 40s 多 5s，边界竞态可双重删除状态。

另有 Desktop Tauri 2 IPC 形状疑似错误（UI 用 `__TAURI__` v1 API）、seccomp overlay 可绕过 M 项、SBX-07~09 无真实 Linux 证据等中高危问题。M3-8/M3-9 三大件（key_ref 加密、日志脱敏、SBOM）和 bench CLI 均**无实现痕迹**，仍是空白。

---

## 1. 门禁与部署状态（亲测）

| 检查 | 声明 | 实测 | 判定 |
|---|---|---|---|
| 本机 pytest | 364 passed / 15 skip | **1 failed / 363 passed / 15 skipped in 174.49s** | ❌ |
| 失败项 | — | `test_signed_install_sources_activate_and_execute[git]`，`git http-backend` 返回 1（重试 3 次后仍失败，本机 fixture 问题） | ❌ |
| `verify-all.mjs --full` | 13/13 | **13/13 PASS**（A/B/B2/C/D/F/G + I/J/K/L/M，E INFO） | ✅ |
| 服务器 `server-test.ps1` | 364 | **283 passed**（M2 时期数字） | ❌ |

### 双端哈希（M3 模块）

```
本机 main.py      f15d8fe8…    服务器 main.py      b9f46109…（M2 基线，不一致）
本机 metrics.py   a2ac2c9d…    服务器 metrics.py   不存在
本机 otel.py      12003103…    服务器 otel.py      不存在
本机 collaboration.py bdb6f529… / scaler.py 896f9beb… —— 服务器均不存在
```

跨机实测：`GET /health` 200、`/ready` 200、`/metrics` 200 但只返回 `uh_memory_short_term_keys` / `uh_memory_long_term_entries` 两个 M1 指标（本机已 13 族）——**服务器跑的是旧内核**。

---

## 2. 已落地 M3-1~M3-7 审阅

### M3-1 Compose / M3-2 K8s —— 结构完整，控制面与密钥有洞

Compose 7 服务、仅 8080 对外、非 root 10001、`.env.example` 密钥引用，I 项文本校验通过。但：

- **长期密钥走环境变量**：`docker-compose.yaml:23,31,35,36` 的 `UH_LLM_API_KEY`/`PGPASSWORD`/`UH_CLIENT_ID_SECRET`/`UH_MEMORY_MASTER_KEY` 都是 env；`docker inspect` 或 `/proc/1/environ` 可读。K8s 侧 `deploy/k8s/deployments.yaml:51-71` 用 `secretKeyRef` 更严，两边不一致。
- **I 项只查字面 `${VAR}`**：`scripts/verify-compose-exposure.mjs:32-36` 的 `ENV_REF` 允许 `:?` 分支携带任意文本后整段 replace，内嵌凭据检测可绕过。
- **K8s Secret 模板直接 apply 会写死占位符**：`scripts/verify-k8s-manifests.mjs:100,142-145` 把 `${VAR}` 当合规；`kubectl apply -f deploy/k8s/`（无 envsubst）会把字面 `${DB_PASSWORD}` 写进 Secret。门禁奖励模板、文档未强调必须渲染。
- **三个 StatefulSet 无 requests/limits**：`deploy/k8s/datastores.yaml:17-24,49-57,81-94`；J 项 `verify-k8s-manifests.mjs:200-205` 与 M 项 `verify-security-hardening.mjs:9` 的 WORKLOADS 都只查四个业务 Deployment，主动豁免存储 Pod。节点内存紧张时 OOM killer 可能先杀内核。
- **K8s Localhost seccomp profile 无节点分发**：`deploy/security/seccomp.json` 需装到每节点，仓库无 DaemonSet/安装脚本；`localhostProfile` 缺失时 Pod 直接启动失败。

### M3-3 Prometheus / M3-4 OTel —— 行为正确但 K8s 侧为空

- `/metrics` 13 族、路由模板化防高基数、K 项 AST 卡口（`scripts/verify-metrics-cardinality.mjs`）——本机实现完整；但 K 项在 `verify-all.mjs:139` 写死 `true`（靠子进程 assert 抛异常判失败），且 AST 只认 `ast.Constant` 的 `# TYPE uh_`，f-string 拼接可绕过。
- 告警规则语义只靠 `tests/unit/test_metrics.py:271-274` 用 Python 重算期望值验证，从不把 PromQL 交给 Prometheus 执行。
- **`deploy/k8s/` 无任何 observability 清单**；`deploy/observability/prometheus.yaml:17` 抓 `kernel:8080`（Compose alias）；default-deny 无 4317/4318 出站。
- **镜像 tag 复用两种依赖集合**：`Dockerfile:6` `UH_INSTALL_EXTRAS=compose`，`deploy/observability/jaeger.yaml:4-6` overlay 改成 `compose,otel`，但都打 `ultra-harness/kernel:0.1.0`；不重建时 `kernel/otel.py:92-95` 只 warn 就 no-op，追踪静默消失。
- **Collector 尾采样无部署路径**：`otel-collector.yaml` 没被任何 overlay/K8s 引用，`UH_OTEL_TAIL_SAMPLING=collector` 只可配不可用；`decision_wait: 8m` + `num_traces: 10000` 中等 QPS 会耗尽缓存。
- **span ID 32 位截断**：`kernel/otel.py:36-37`，单 trace ~6.5 万 span 后碰撞概率过半，跨服务父子关系可错配（tracing.md 承认）。

### M3-5 CrewAI 按需启停 —— 逻辑有竞态

- **控制面断链**（阻断，见 §0-1）：`deploy/k8s/networkpolicies.yaml:1-18,41-64` 无 apiserver 放行；`docs/deployment/kubernetes.md:35` 要求运维自建同节点 TLS 入口，仓库无清单；`kernel/scaler.py:38-40` 默认走 ClusterIP → PATCH 被 CNI 丢弃 → 30s degraded。
- **释放泄漏副本**：`kernel/collaboration.py:132` finally 用 `_background(self._release(...))` 不等待；`:208-232` scale(0) 失败只发 degraded 不重试；进程退出即副本永久 1。锁 TTL 45s vs 释放超时 40s，边界可双 owner 并发 `_release` 双重删状态。
- **预算叠加**：`kernel/scaler.py:78` scale 可用满 30s，`collaboration.py:202` 之后又 `wait_ready()`（`:32-42` 无内部超时）→ 扩容成功但没时间就绪，0→1→0 空转。租约 180s > 协作截止 155s，崩溃后 reconciler 最长 180s 才回收。
- **StorageGuard 不覆盖 worker**：`kernel/protocol.py:199` 只在内核进程校验，worker 是独立 HTTP 进程（`collaboration.py:48`）；K8s 若无等价 NetworkPolicy，能力边界是声明式的。
- 乐观锁正确性仅 fakeredis 验证，无真实 Redis WATCH 竞态证据。

### M3-6 Desktop 壳 —— Tauri 2 IPC 形状疑似错误

- `ui/app.js:44` 用 `window.__TAURI__?.core.invoke`，而 Tauri 2（`desktop/src-tauri/Cargo.toml:13` tauri="2"，`tauri.conf.json:10` withGlobalTauri=true）的全局 API 是 `__TAURI_INTERNALS__`（Tauri 2 文档），`__TAURI__` 是 v1 兼容层且未必暴露；若 undefined，第 44 行静默 return，托盘/通知永不更新。`tests/unit/test_desktop.py:106` 手工 mock 了 `__TAURI__:{core:{invoke}}`，测试通过却掩盖此问题。**未编译真机验证**（`desktop/README.md:63`）。
- `tauri.conf.json:7` frontendDist 是远程 `http://127.0.0.1:8080/?desktop=1`，生产 bundle 强依赖本地内核服务；`capabilities/desktop.json:4` 标 `"local": true` 与测试注释「release frontendDist is Tauri-local origin」冲突。
- 无 `Cargo.lock`，依赖未锁；README 明确安装包不含 Python 解释器（需用户 `pip install -e .`），非开箱即用。
- 主题双端同步实为 `ui/themes.js:127-129` 3 秒轮询，非事件推送；`test_desktop.py:66-84` 同进程两 TestClient 未覆盖真实双窗口延迟。

### M3-7 系统级安全加固 —— 实现在，默认部署未启用且缺运行时证据

- **cgroup 默认不启用**（阻断）：`kernel/sandbox.py:139` 只在 `UH_SANDBOX_CGROUP_ROOT` 存在时建 cgroup，否则只 warning；docker-compose.yaml 与 deploy/security/*.yaml 均未注入该变量 → `pids.max`/`cpu.max` 生产落空，容器级 `pids_limit:256` 管不到容器内插件子进程。
- **seccomp overlay 绕过 M 项**：`deploy/security/compose-bwrap.yaml:5-8` 用 `!override` 改 seccomp 并把 `UH_SANDBOX_OCI_STRICT` 改为 `"0"`；`scripts/verify-security-hardening.mjs:9,64` 只读 `docker-compose.yaml`，看不到 overlay → 用该 overlay 启动即放宽嵌套沙箱而门禁仍 PASS。
- **三态调试无 fail-closed**：`kernel/linux_sandbox.py:22,31-35,110-112` 的 disabled/audit 分别不下发 filter 或只 LOG 不拦截；`kernel/sandbox.py:144-145` 非 enforce 仅 warning，无启动失败/告警升级。
- **白名单宽项**：`deploy/security/seccomp.json:19,24,30` 的 ioctl/prctl/seccomp 无条件 ALLOW，无参数约束；`verify-security-hardening.mjs:30-34` 只校验危险名单 deny，不校验白名单宽度。
- **SBX-07~09 无真实 Linux 证据**：`tests/unit/test_security_hardening.py:18-21,260-324` 全挂 `@LINUX_ACCEPTANCE`（需 linux + `UH_RUN_LINUX_HARDENING=1`），本机 Windows skip；`2026-11-22-SBX-Linux-矩阵证据.md:16-26` 只覆盖 SBX-01~05 + isolation/net/credential，**07/08/09 从未真跑**。M 项全绿不含这三项运行时证据。
- **read-only root 可写面过大**：`docker-compose.yaml:46` 把 `uh-state:/app/.uh` 整卷可写（含插件根 `/app/.uh/plugins`，`:38`），插件进程 OS 级可写全部内核状态/凭据引用。

---

## 3. 本轮新发现（按严重性）

| # | 严重性 | 问题 | 证据 |
|---|---|---|---|
| N1 | **P0** | K8s scaler 控制面不通，协作 0→1 永远 degraded | `networkpolicies.yaml:1-18` default-deny 无 apiserver；`kubernetes.md:35` 要求运维自建入口但无清单；`scaler.py:38-40` |
| N2 | **P0** | M3-3/M3-4 在 K8s 侧无落地，可观测性为空 | `deploy/k8s/` 无 observability；`observability/prometheus.yaml:17` 抓 Compose alias `kernel:8080`；NetPol 无 4317/4318 |
| N3 | **P0** | cgroup/pids.max 默认未注入，fork 炸弹防护落空 | `sandbox.py:139`；compose K8s 清单无 `UH_SANDBOX_CGROUP_ROOT` |
| N4 | **P0** | 协作释放 fire-and-forget + 失败不重试，副本泄漏 | `collaboration.py:132,208-232`；锁 TTL 45s vs 释放 40s |
| N5 | P1 | Desktop Tauri 2 用 v1 IPC API，通知/状态静默失效 | `ui/app.js:44` `__TAURI__`；`Cargo.toml:13` tauri=2；`test_desktop.py:106` mock 掩盖 |
| N6 | P1 | seccomp overlay 把 OCI_STRICT=0，M 项不读 overlay | `compose-bwrap.yaml:5-8`；`verify-security-hardening.mjs:9,64` |
| N7 | P1 | SBX-07~09 从未真实 Linux 运行，13/13 不含其证据 | `test_security_hardening.py:18-21` skip；`SBX-Linux-矩阵证据.md` 无 07~09 |
| N8 | P1 | 长期密钥走 env 可被 `/proc/1/environ` 读取 | `docker-compose.yaml:23,31,35,36` |
| N9 | P1 | 镜像 tag 复用两种依赖集合，OTel 静默关闭 | `Dockerfile:6` / `jaeger.yaml:4-6` / `otel.py:92-95` |
| N10 | P2 | K 项 `check(...,true,...)` 恒真 + AST 只认 ast.Constant | `verify-all.mjs:139`；`verify-metrics-cardinality.mjs:48-49` |
| N11 | P2 | StatefulSet 无 resources，J/M 项主动豁免 | `datastores.yaml:17-24,49-57,81-94`；`verify-k8s-manifests.mjs:200-205` |
| N12 | P2 | span ID 32 位截断碰撞 | `otel.py:36-37`；`tracing.md:20` |
| N13 | P2 | 主题双端同步实为 3s 轮询 | `ui/themes.js:127-129` |
| N14 | P2 | 告警规则无 PromQL 真机求值 | `test_metrics.py:271-274` |
| N15 | P2 | 协作链路无指标，M3-9 SLO 无法度量 | `kernel/metrics.py` 无 collaboration/crewai/scale 指标 |

---

## 4. M3-8 / M3-9 未完工缺口

**M3-8（#35）**：任务卡五项全勾空。实测：
- `grep key_ref` 在 `kernel/long_term_memory.py` 与契约中**零命中**——L2 虽已有 AESGCM 加密（`long_term_memory.py:196-200`），但无 `key_ref: memory-master-key` 语义。
- `kernel/logging.py` 无 REDACT/熵检测/正则脱敏实现——日志正文脱敏未开工。
- `Dockerfile`/`.github/workflows`/`deploy/**` 无 SBOM（syft/cyclonedx）痕迹——镜像含 SBOM 未兑现。
- SEC-01~05 无证据。

**M3-9（#36）**：`kernel/cli.py` grep `bench` 零命中——`uh bench run --scenario --concurrency` 未实现；`benchmarks/` 目录不存在；无 100 并发/P95/SLO 数据；无轻量/标准/企业成本模板。

**衔接风险**：M3-8 的日志脱敏需覆盖 `kernel/crewai_worker.py:42,47` 的协作 payload 回显（把原始 content 拼入 answer 进入事件/日志）；M3-8 供应链需处理 `deploy/crewai/Dockerfile:4-5` 动态 pip 安装无锁/hash；M3-9 需要 N15 的协作指标才能度量冷启动 SLO。

---

## 5. 探针副作用与结论

- 只读源码/契约/测试；跨机仅 GET `/health` `/ready` `/metrics` 与 `certutil` 哈希；本机仅运行 pytest/verify-all 与只读 import 探针（一次 `get_metrics` 导入名错误，无副作用）。未改任何文件/服务器配置。
- **结论：M3 进行中不建议放行生产；M3-1~M3-7 的工程质量本身不错（I/J/K/L/M 门禁全绿、结构完整），但 N1~N4 四个 P0 与服务器未部署使「生产可用」无从谈起。**
- **M3-8/M3-9 建议**：开工前先把 N15 协作指标补进 M3-3，把日志脱敏与供应链纳入统一门禁；bench 需先固定硬件基线，否则数字不可比。
- **给 DSH 的优先顺序**：① 服务器部署 M3 七模块并重跑门禁；② 补 K8s apiserver NetworkPolicy + observability 清单；③ cgroup 根注入 compose/K8s；④ 协作释放改为等待+重试+锁 TTL 收紧；⑤ Desktop IPC 真机核验；⑥ SBX-07~09 在 Linux 实跑。

> 审查主体：**Tencent Hunyuan Hy（腾讯混元 Hy）** · WorkBuddy Agent 模式
> 基线 `3568349` · .14 → .28:8080 · 2026-11-24
> 本报告不含凭据原文。
