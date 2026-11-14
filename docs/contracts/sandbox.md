# P0-4 契约：沙箱分期与平台矩阵

> 版本：v0.1.0（契约冻结候选）· 状态：needs-review（内核负责人评审）
> 基线：v3 §5.5 / §9.3 / §10.2 / §8.8 · 关联：errors.md（KRN-2010 / CAP-5021）、manifest schema（network.allow_hosts / memory_limit_mb / timeout_ms）
> 定位：§5.5 是**目标态**（M3 达成），§9.3 是**本期交付态**（M2 强制项）。本文件是两者的唯一展开事实源，Blocks M2-2 / M3-7。

## 1. 分期表（维度 × M2 / M3）

| 维度 | M2 最小版（必须交付，§9.3） | M3 加固版 | §5.5 目标态对应 |
|---|---|---|---|
| 进程隔离 | 每插件独立子进程，父进程监控；崩溃自动重启，连续 3 次 quarantined（SBX-05） | + 非 root 用户运行 | — |
| 文件系统 | 限定插件私有目录；越界 → UH-KRN-2010 + 审计（SBX-01） | + 只读根文件系统 + 可写 tmpfs | chroot（Linux 专属，跨平台等价见 §2） |
| 网络 | 默认禁止出网；按 manifest network.allow_hosts 放行；未声明即拦截 + 审计（SBX-02） | + 出网强制经内核代理，平台级强制 | 出站白名单 |
| 内存 | config.memory_limit_mb 强制，超限 OOM Kill 不拖垮内核（SBX-04） | + cgroups 层级配额（Linux） | 进程级上限 |
| CPU | 单核时间片 | + cgroups cpu.max（Linux）/ 平台等价 | cgroups 配额 |
| 系统调用 | **不做**过滤（显式声明，见 §3 降级） | seccomp 白名单（Linux）/ Seatbelt（macOS）/ AppContainer（Windows）（SBX-07 新增） | seccomp 白名单 |
| 超时 | 内核侧 config.timeout_ms 计时，超时取消 → UH-CAP-5021（SBX-03） | 同 M2 | — |
| 供应链 | SHA-256 + 签名 + 静态扫描 + 权限三态（M2-1；SBX-06 属安装期） | + SBOM（M3-8） | — |

原则：M2 每个维度都有**可执行**的平台机制（§2）；做不到的显式降级（§3），绝不写"已实现"。

## 2. 平台矩阵（M2 最小版 —— 三平台均可执行）

### 2.1 Linux（首选：bubblewrap，无特权用户命名空间）

```bash
# 默认无网络（--unshare-net）；资源上限经 prlimit；随父进程退出
prlimit --as=$((MEMORY_LIMIT_MB * 1024 * 1024)) --cpu=$CPU_SECONDS --nofile=256 \
bwrap \
  --ro-bind /usr /usr --ro-bind /lib /lib --ro-bind /lib64 /lib64 --ro-bind /bin /bin \
  --bind "$PLUGIN_DIR" /sandbox --proc /proc --dev /dev --tmpfs /tmp \
  --unshare-net --unshare-pid --die-with-parent --chdir /sandbox \
  python3 /sandbox/main.py
```

- 文件系统：私有目录 bind 为 /sandbox，其余只读最小运行时 → SBX-01 OS 级拦截。
- 网络：默认 --unshare-net 全禁；声明 network.outbound 时改用 slirp4netns 用户态网络栈 + 内核出网代理（代理校验 allow_hosts，Flatpak 同款组合）→ SBX-02 OS 级拦截。
- 内存/CPU：prlimit RLIMIT_AS / RLIMIT_CPU；超时由内核计时 Kill。
- 降级条件：内核禁用 unprivileged user namespace（部分容器/旧内核）→ 退化为 §3 补偿控制模式并显式告警。

### 2.2 macOS（Seatbelt / sandbox-exec）

```scheme
(version 1)
(deny default)
(allow process-fork process-exec)
(allow file-read* (subpath "/usr/lib") (subpath "/System/Library") (subpath "/Library/Frameworks"))
(allow file-read* file-write* (subpath "$PLUGIN_DIR"))
(deny network*)
; 声明 network.outbound 时追加（仅放行内核代理）：
; (allow network-outbound (remote ip "127.0.0.1:8899"))
```

- 执行：sandbox-exec -f plugin.sb python3 main.py；资源经 setrlimit + task_policy。
- SBX-01/02 为 OS 级拦截（Seatbelt 强制）。
- 风险披露：sandbox-exec 被 Apple 标记 deprecated（仍可用）；M3 评估替代（App Sandbox entitlements / Endpoint Security），届时修订本契约。

### 2.3 Windows（Job Object + ACL + 代理，显式降级）

- 进程：CreateProcess 子进程 + AssignProcessToJobObject（JOB_OBJECT_LIMIT_PROCESS_MEMORY、JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE、CPU rate 限制）→ SBX-03/04/05 OS 级。
- 文件系统：私有目录 ACL 仅授插件运行身份；SDK 路径校验 + 内核代理 FS 接口 → SBX-01 **行为级**拦截（SDK/代理拒绝 + KRN-2010 + 审计）。
- 网络：默认仅注入内核代理环境变量，代理校验 allow_hosts；静态扫描拦截直接 socket 调用 → SBX-02 **行为级**拦截。
- **M2 无法 OS 级强制**：绕过 SDK 的 raw socket / 直接 CreateFile 越界 / 危险系统调用 → 见 §3 降级与补偿。M3 以 AppContainer 补齐（§4）。

### 2.4 M2 平台能力速览

| 能力 | Linux (bwrap) | macOS (Seatbelt) | Windows (Job Object) |
|---|---|---|---|
| FS 越界拦截 | OS 级 | OS 级 | 行为级（SDK/代理+审计） |
| 出网拦截 | OS 级（netns） | OS 级 | 行为级（代理+静态扫描） |
| 内存 OOM Kill | OS 级 | OS 级 | OS 级 |
| CPU/超时 Kill | OS 级 | OS 级 | OS 级 |
| 崩溃 quarantine | 内核 | 内核 | 内核 |
| 系统调用过滤 | ✗（M3） | ✗（M3） | ✗（M3） |

## 3. Windows M2 降级策略与风险披露（任务硬要求）

做不到清单（M2，显式承认）：
1. 无法 OS 级阻止绕过代理的 raw socket 出网。
2. 无法 OS 级阻止私有目录外的直接文件读写（ACL 仅约束插件身份对受保护路径的访问，用户自有文件仍可读）。
3. 无系统调用过滤（无 seccomp 等价）。

补偿控制（叠加生效）：
- 默认全签（P0-2 签名策略）+ uh plugin validate 静态扫描：直接 socket / 越界路径 / 危险调用 → 拒绝安装或显著告警。
- permission_prompt 安装时逐项确认；UI 常驻徽标："该插件在 Windows 运行于降级沙箱（M3 补齐 OS 级隔离）"。
- 审计全量记录 FS/网络行为；连续 3 次崩溃 quarantined；高危工具人工确认（§9.6）。
- 企业生产环境建议：Windows 插件负载调度至 Linux 容器节点（K8s 混合节点池），契约不强制。

风险披露文案（安装时展示，进入安装报告）：
> Windows M2 沙箱为进程级 + 行为级隔离：资源与崩溃由系统强制，文件/网络越权由 SDK 与代理拦截并审计，但无法在操作系统层面阻止恶意插件绕过 SDK 的直接访问。请仅安装签名可信插件；OS 级隔离（AppContainer）在 M3 交付。

## 4. 平台矩阵（M3 加固版）

| 维度 | Linux | macOS | Windows |
|---|---|---|---|
| 系统调用 | seccomp-bpf 白名单（libseccomp，bwrap --seccomp）；屏蔽 reboot/mount/ptrace 等 | Seatbelt 收紧：deny file-ioctl、限制 mach-lookup、deny network*（仅代理） | AppContainer（CreateAppContainerProfile）：无 capability 即无网络/受限 FS |
| 资源配额 | cgroups v2：memory.max / cpu.max / pids.max | setrlimit + task_policy（无 cgroups 等价，显式声明） | Job Object（M2 已有）+ AppContainer 资源策略 |
| 根文件系统 | 只读 bind + tmpfs 可写层 | Seatbelt 只读 subpath 白名单 | AppContainer SID ACL：仅私有目录可写 |
| 非 root | 专用 uid/gid（bwrap --uid/--gid） | 专用服务账户（_uhplugin，可选） | AppContainer 天然低完整性 |
| 网络 | netns + slirp4netns + 代理强制 | 仅代理地址放行 | internetClient capability 仅在声明时授予 + 代理 |
| 确认令牌 | 防重放（M1-8 / M3-7 联动，跨平台一致） | 同 | 同 |

macOS 无 seccomp/cgroups：Seatbelt + rlimit 为平台等价物，本表即其事实源，不再另行表述。

## 5. SBX 验收 × 平台 × 分期（M2-2 / M3-7 验收依据）

| 测试 | 场景 | Linux M2 | macOS M2 | Windows M2 | M3 |
|---|---|---|---|---|---|
| SBX-01 | 读未声明文件 | OS 拦截+KRN-2010+审计 | OS 拦截+同 | SDK/代理拦截+KRN-2010+审计（行为级） | 三平台 OS 级 |
| SBX-02 | 未声明出网 | OS 拦截+审计 | OS 拦截+审计 | 代理拦截+审计+静态扫描前置（行为级） | 三平台 OS 级 |
| SBX-03 | 死循环 | 超时 Kill+CAP-5021 | 同 | 同 | 同 |
| SBX-04 | 内存暴涨 | OOM Kill 不拖垮内核 | 同 | 同 | +cgroups 层级配额 |
| SBX-05 | 连续崩溃 3 次 | quarantined | 同 | 同 | 同 |
| SBX-06 | 签名不符 .uhpkg | 安装拒绝+告警（**安装期，属 M2-1**，三平台一致） | 同 | 同 | 回归 |
| SBX-07（新增） | 危险系统调用（mount/ptrace） | seccomp 拦截 | Seatbelt 拦截 | AppContainer 拦截 | M3 必过 |
| SBX-08（新增） | 写只读根 | 拒绝 | 拒绝 | 拒绝 | M3 必过 |
| SBX-09（新增） | 插件进程身份 | 非 root 断言 | 非 root/服务账户断言 | AppContainer 低完整性断言 | M3 必过 |

登记：v3 §10.2 的 SBX-06 是安装期签名测试，M3-7 验收原误引 SBX-06；M3 加固专属测试以 SBX-07~09 新增（只增不改），修订见 R3。

## 6. 决议记录

| # | 决议 | 理由 |
|---|---|---|
| S1 | §5.5 标目标态、§9.3 标交付态，本文件为展开事实源 | 任务要求；消除"文档写成已实现" |
| S2 | Linux M2 用 bwrap+slirp4netns（无特权），不用 chroot | chroot 需 root 且非跨平台；bwrap 为 Flatpak 验证过的无特权方案 |
| S3 | macOS M2 用 Seatbelt，披露 deprecated 风险 | 当前唯一可执行 OS 级方案；M3 评估替代 |
| S4 | Windows M2 显式降级（行为级）+ 补偿控制 + 风险披露；M3 AppContainer 补齐 | 无 chroot/seccomp 等价；诚实分期优于虚假承诺 |
| S5 | SBX-01/02 分平台定义通过判据（OS 级 vs 行为级） | 否则 M2-2 验收在 Windows 上无法判定 |
| S6 | SBX-06 归位安装期（M2-1）；新增 SBX-07~09 供 M3-7 | 原 M3-7 误引 SBX-06，M3 无专属测试 |
| S7 | macOS 无 seccomp/cgroups，Seatbelt+rlimit 为等价物并显式声明 | 平台事实，避免照抄 Linux 表述 |
