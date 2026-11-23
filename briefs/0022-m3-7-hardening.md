# Brief 0022：M3-7 系统级安全加固（#34）

## 角色与约定
你是实现者（Codex），先读 AGENTS.md。只加/改代码与测试，**不要 git commit**（DSH 审后提交）。
**M3-1~6 已完成**（compose/K8s/指标/OTel/CrewAI/Desktop，350 passed + 11 skip，verify-all **12 项 A~L**）。本任务 #34 承接。

## 硬约束
- **禁 pip install 到本机全局**；新依赖走 optional extra 并说明。
- 禁写 `$null`/`%VAR%`/`~` 未展开路径；密钥严禁明文。
- **契约冻结只增不改**；不得削弱现有 350 测试（11 skip 为 OTel 未装属正常）。
- **本机是 Windows**：seccomp/cgroups/只读根等 Linux 特性**无法本地实测**，须产出可落地配置 + 静态校验 + 文档，并**如实标注未实测**。（参考：DSH 已在 Kylin Linux 用 bwrap 采过 SBX-01~05 证据，见 docs/reviews/2026-11-22-SBX-Linux-矩阵证据.md）

## 必读
- uh-issues/issues/M3-7.md（#34 任务/验收/**风险：seccomp 白名单过严会误杀插件，需提供调试模式**）
- **docs/contracts/sandbox.md**：第 16 行（syscall 过滤分期）、**第 115-119 行 SBX-07/08/09 定义**（危险 syscall mount/ptrace 拦截 / 写只读根拒绝 / 插件进程非 root 断言，均 M3 必过）、第 119/130 行（**SBX-06 归位安装期，M3 专属测试是 07~09，勿混淆**）
- kernel/sandbox.py（bwrap Linux 分支已有 --unshare-pid/net + ro-bind；Windows degraded；ulimit -v 内存限额；SandboxProcess.wait 超时 CAP-5021）
- Dockerfile（已 USER 10001:10001 非 root）、deploy/k8s/deployments.yaml、docker-compose.yaml
- kernel/confirmation.py（**M1-8 已实现一次性防重放 + 任务绑定 + invalidate_task**，本任务只需回归验证勿重造）

## 需求
### 1. seccomp 系统调用白名单（SBX-07）
- `deploy/security/seccomp.json`：默认拒绝 + 白名单放行常规 syscall；**明确拦截 mount / ptrace / kexec_load / init_module / bpf / clone(新 namespace) 等危险调用**。
- bwrap 分支接入 `--seccomp <fd>`（bwrap 需 fd 传参，注意实现方式）；Docker/compose 用 `security_opt: [seccomp:./deploy/security/seccomp.json]`；K8s 用 `securityContext.seccompProfile: {type: Localhost, localhostProfile: ...}`。
- **调试模式（风险项必须）**：`UH_SANDBOX_SECCOMP=disabled|audit|enforce`，audit 模式只记录不拦截（SCMP_ACT_LOG），便于排查误杀；文档写明如何定位被拦 syscall。

### 2. cgroups 层级配额
- Linux：优先 cgroup v2（cpu.max / memory.max / pids.max），v1 回退（现有 ulimit -v 保留兼容）。
- K8s：resources.requests/limits（cpu/memory）+ 可选 LimitRange；compose：`mem_limit`/`cpus`/`pids_limit`。
- **pids.max 防 fork 炸弹**（现有仅内存/超时，缺进程数限制）。

### 3. 只读根 + 可写 tmpfs（SBX-08）
- bwrap：已有 ro-bind 系统目录，**补 `--tmpfs /tmp` 之外的可写路径边界**，确保插件只能写 /sandbox 与 tmpfs。
- Docker/compose：`read_only: true` + `tmpfs: [/tmp]` + 必要 volume 挂载可写数据目录。
- K8s：`readOnlyRootFilesystem: true` + emptyDir 挂 /tmp 与数据目录。

### 4. 插件进程非 root（SBX-09）
- 断言插件子进程 uid != 0；K8s `runAsNonRoot: true` + `runAsUser: 10001` + `allowPrivilegeEscalation: false` + `capabilities.drop: [ALL]`。
- compose `user: "10001:10001"`；Dockerfile 已非 root（复核）。

### 5. 确认令牌防重放回归（配合 M1-8，勿重造）
- 补/核实测试：同 token 二次 resolve 被拒、跨任务 token 被拒、cancel 后 token 失效、TTL 超时。

### 6. 静态校验门禁 + 测试
- `scripts/verify-security-hardening.mjs`：断言 seccomp.json 存在且拦危险 syscall、compose/K8s 含 read_only/rootfs、非 root、cap drop、no privilege escalation、pids 限制、调试开关存在。接入 verify-all **M 项**（保持既有 12 项不变）。
- `tests/unit/test_security_hardening.py`：配置断言 + **Windows 上跳过 Linux 专属实机项（skip 而非 fail）** + 确认令牌防重放回归 + 调试模式三态行为。
- **Linux 实机项标注**：SBX-07/08/09 的真实拦截需 Linux，文档给出在 Kylin/容器内的验证命令（参考已有 SBX Linux 证据报告格式）。

## 验收（#34）
- SBX-07~09 通过 + SBX-06 回归（**注意 06 是安装期签名，07~09 才是本轮新增**）
- §9.10 安全配置可落地（清单齐全可直接 apply）
- 危险系统调用被拦截（Linux 实测或静态配置 + 验证步骤）

## 门禁（跑并报真实数字）
- `python -m pytest -q`（≥350 全绿，skip 说明）
- `node scripts/verify-all.mjs --full`（全绿含新 M 项）
- `node scripts/secret-scan.mjs`（rc=0）

## 报告
新增改动文件 + seccomp 白名单策略与**调试模式用法** + cgroups v1/v2 方案 + 只读根/非 root 落地清单 + SBX-07~09 验证方式（哪些静态哪些待 Linux 实测）+ 三门禁真实数字。**不要提交**。
