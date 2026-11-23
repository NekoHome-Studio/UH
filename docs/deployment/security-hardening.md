# M3-7 系统级安全加固

本轮在 Windows 验证了配置、启动参数、确认令牌和安装签名回归。Linux 内核实际拦截、cgroup 配额生效及容器启动仍需采证。既有 Kylin 记录覆盖 M2 的 SBX-01~05。 [Kylin 矩阵证据](../reviews/2026-11-22-SBX-Linux-矩阵证据.md)

## Linux 宿主机

以专用非 root 账号启动内核，准备可创建用户、PID、网络和挂载命名空间的 bubblewrap，以及系统库 `libseccomp.so.2`。Python 通过标准库 `ctypes` 调用该库，Linux 目标为 x86_64 和 aarch64。Dockerfile 安装 Debian 的 `libseccomp2`，Python 安装依赖无需增加。

bubblewrap 在切换根目录前配置挂载，再从继承的文件描述符读取 BPF。插件使用宿主启动账号映射后的非零 UID/GID，执行入口前再次检查有效 UID。根目录、运行时、SDK、`/proc` 和设备挂载点只读，插件目录 `/sandbox` 与私有 tmpfs `/tmp` 可写。

| `UH_SANDBOX_SECCOMP` | bubblewrap 子进程 |
| --- | --- |
| `enforce`（默认） | 常规 I/O、线程、进程、信号等调用按白名单放行，未列出的调用返回 EPERM。mount、ptrace、bpf、模块装载及其他显式拒绝项返回 EACCES。clone 只允许不含 namespace 标志的调用。clone3 返回 ENOSYS，让 libc 回退到可检查标志的 clone。 |
| `audit` | 常规调用直接放行，白名单之外的调用交给 `SCMP_ACT_LOG`，由内核记录并执行。 |
| `disabled` | 省略 bubblewrap 的 seccomp fd。命名空间、挂载、身份和已配置的资源配额仍适用。 |

遇到插件误杀时，在隔离的调试环境以 `UH_SANDBOX_SECCOMP=audit` 重启内核并重跑可信插件。查看 `sudo ausearch -m SECCOMP -ts recent -i` 或 `sudo journalctl -k --since "10 minutes ago"`，按进程名、PID、时间及审计记录中的 `arch`、`syscall` 定位。宿主机需支持 LOG action，且 `/proc/sys/kernel/seccomp/actions_logged` 包含 `log`。以 `scmp_sys_resolver` 在对应架构解析数字 syscall。完成分析后恢复 `enforce`。OCI 外层策略继续生效，容器 profile 的日志调试见下文。 [bubblewrap 参数](https://github.com/containers/bubblewrap/blob/main/bubblewrap.c) · [Kubernetes seccomp 教程](https://kubernetes.io/docs/tutorials/security/seccomp/)

### cgroups

将 `UH_SANDBOX_CGROUP_ROOT` 指向内核服务账号可写的已委派目录。每次启动创建独立叶节点，启动器先加入 cgroup，之后才执行 bubblewrap，因此它的所有后代继承配额。默认预算为 1 核、插件 manifest 的内存上限和 64 个任务（含线程及 bubblewrap 自身）。退出后回收叶节点。

- **v2** 使用 `cpu.max`、`memory.max`、`pids.max`。委派目录需启用 `cpu memory pids`，内核服务进程放入该目录下独立的叶节点。配置目录没有对应控制器、权限或挂载时，插件启动报错。
- **v1** 从委派目录下的 `cpu`（也接受 `cpu,cpuacct`）、`memory`、`pids` 分别创建配额。可由运维把这三个入口链接到对应控制器的委派子树。在 Kylin 上确认 `memory.use_hierarchy` 已启用。
- 未设置委派目录时，兼容路径保留 `ulimit -v` 单进程虚拟内存限制，内核记录 `plugin.sandbox.cgroup_unconfigured`。为生产 Linux 宿主机配置委派才能启用每插件 CPU、内存总量和 PID 上限。容器另有服务或 Pod 级配额。

例如，在支持 cgroup v2 和用户级委派的 systemd 验收机上，从检出目录启动独立 scope 的 shell。

```bash
systemd-run --user --scope -p Delegate=yes /bin/bash --norc -i
```

在该 shell 中创建内核服务叶节点，然后运行内核或验收测试。

```bash
cg=$(awk -F: '$1 == "0" {print $3}' /proc/self/cgroup)
base="/sys/fs/cgroup${cg}"
mkdir -p "$base/kernel"
printf '%s\n' "$$" > "$base/kernel/cgroup.procs"
printf '%s\n' '+cpu +memory +pids' > "$base/cgroup.subtree_control"
export UH_SANDBOX_CGROUP_ROOT="$base"
```

生产 systemd 单元使用 `Delegate=yes`，在启动时用同样的顺序移到服务叶节点并启用控制器。用户管理器缺少控制器委派时，由管理员在系统服务单元配置委派。 [Linux cgroup v2](https://docs.kernel.org/admin-guide/cgroup-v2.html)

## Compose 和 Kubernetes

加固覆盖四个 `uh-*` 应用服务。应用用户为 `10001:10001`，根文件系统只读，禁提权并删除 capabilities。Compose 挂载带大小限制的 `/tmp` tmpfs，内核通过 `uh-state` 卷写 `/app/.uh`。K8s 用内存型 emptyDir 挂载 `/tmp`，用普通 emptyDir 保存内核 Pod 本地数据。Pod 被替换后会丢失这部分状态，需持久化时为各副本配置独立卷。依赖存储服务使用它们自己的镜像用户和数据卷。

Compose 应用服务各有 1 核、1 GiB 和 256 PID 上限。K8s 的 CPU、内存 requests/limits 已在 Deployment 生效，Pod PID 限额由 kubelet 管理。将 [PID 配置](../../deploy/security/kubelet-pids.yaml) 的 `podPidsLimit: 256` 合并到目标节点的现有 kubelet 配置，按集群流程重载，保留其余节点设置。 [Kubernetes PID 限额](https://kubernetes.io/docs/concepts/policy/pid-limiting/)

Compose 从检出目录读取 `deploy/security/seccomp.json`。K8s 的 `Localhost` profile 需在所有可调度目标 Linux 节点上预置。以下命令使用 kubelet 默认根目录，自定义根目录时使用对应的 `seccomp` 目录。

```bash
sudo install -D -m 0644 deploy/security/seccomp.json /var/lib/kubelet/seccomp/uh/seccomp.json
kubectl apply --dry-run=server -f deploy/k8s/deployments.yaml
kubectl apply -f deploy/k8s/deployments.yaml
```

完整启动流程参见 [Compose 部署](docker-compose.md) 和 [Kubernetes 部署](kubernetes.md)。 [Docker seccomp](https://docs.docker.com/engine/security/seccomp/) · [Kubernetes Localhost profile](https://kubernetes.io/docs/tutorials/security/seccomp/)

### 在容器中运行插件

OCI 的 seccomp 会被后代继承。默认严格 profile 拒绝 bubblewrap 建立隔离时所需的 mount 和 namespace clone，因此默认清单会明确拒绝启动需执行子进程的插件。原生 Linux 部署使用完整的 bubblewrap 策略。需要在容器中热安装和运行此类插件时，外层改用允许隔离初始化的派生 profile，插件入口仍由内层 `enforce` profile 约束。该部署需要运行时和节点 LSM 允许非 root bubblewrap 创建自己的用户及挂载命名空间。

```bash
mkdir -p .work
node scripts/render-seccomp-profile.mjs bwrap > .work/seccomp-bwrap.json
docker compose -f docker-compose.yaml -f deploy/security/compose-bwrap.yaml config --quiet
docker compose -f docker-compose.yaml -f deploy/security/compose-bwrap.yaml up -d --build
```

K8s 使用同一份生成文件。在每个目标节点执行第一条命令预置 profile，部署时用第二条命令更新内核 Deployment。

```bash
sudo install -D -m 0644 .work/seccomp-bwrap.json /var/lib/kubelet/seccomp/uh/seccomp-bwrap.json
kubectl -n uh-prod patch deployment uh-kernel --type=strategic --patch-file deploy/security/k8s-bwrap-patch.yaml
```

Compose 和 K8s 均保留非 root、只读根、禁提权和资源配额。节点拒绝创建命名空间时，选择原生 Linux 路径。容器启动成功后仍需用下文的 SBX 用例采证。

若需诊断 OCI 外层的误杀，在隔离的调试环境用 `node scripts/render-seccomp-profile.mjs audit` 生成 `SCMP_ACT_LOG` profile，临时替换目标容器的外层 profile 后重新创建容器。恢复默认文件并重建容器可恢复外层白名单。OCI 的策略切换由运行时完成。

## 验收与取证

从 Kylin 或另一台允许 bubblewrap 的 Linux 主机，以非 root 账号在项目虚拟环境运行。设置上述 cgroup 委派目录后，第四个用例还会验证插件实际加入了对应控制组。

```bash
UH_RUN_LINUX_HARDENING=1 python -m pytest -vv tests/unit/test_security_hardening.py -k linux
python -m pytest -q tests/unit/test_plugins.py::test_signature_mismatch_is_rejected_before_extraction
```

| 编号 | 已验证 | Linux 采证预期 |
| --- | --- | --- |
| SBX-06 | 安装期签名回归 | 签名不匹配的包在解包前被拒绝 |
| SBX-07 | 显式拒绝规则、clone 掩码、三态及 fd 传递 | mount 与 ptrace 返回 EACCES，普通线程可启动 |
| SBX-08 | 只读根及 tmpfs/状态卷静态配置 | 根写入返回 EROFS，`/sandbox` 与 `/tmp` 可写 |
| SBX-09 | Compose/K8s/Dockerfile 非 root 静态配置 | 插件内查询得到非零 UID 与 EUID |

在默认 OCI profile 下，可以用同一探针检查运行中的 Compose 容器或 K8s Pod。该探针覆盖的是容器外层。记录节点、内核版本、bubblewrap 版本、cgroup 版本、运行时、profile、命令退出码和输出。

```bash
container_probe() {
  "$@" python - <<'PY'
import ctypes
import errno
import os
from pathlib import Path
from tempfile import TemporaryDirectory

libc = ctypes.CDLL(None, use_errno=True)
assert libc.mount(None, None, None, ctypes.c_ulong(0), None) == -1
assert ctypes.get_errno() == errno.EACCES
assert libc.ptrace(ctypes.c_int(2), ctypes.c_int(-1), None, None) == -1
assert ctypes.get_errno() == errno.EACCES
assert os.statvfs("/").f_flag & os.ST_RDONLY
for directory in ("/tmp", "/app/.uh"):
    with TemporaryDirectory(dir=directory) as fixture:
        Path(fixture, "probe").write_text("ok")
assert os.getuid() == os.geteuid() == 10001
print("SBX-07 outer EACCES, SBX-08 outer read-only root, SBX-09 outer UID=10001")
PY
}
container_probe docker compose exec -T uh-kernel
container_probe kubectl -n uh-prod exec -i deploy/uh-kernel --
```

在 Linux 节点上使用容器 PID 的 `/proc/<pid>/cgroup` 找到实际控制组，核对容器或 Pod 的 `cpu.max`、`memory.max`、`pids.max` 或 v1 对应文件。启用派生 profile 时，再以已签名测试插件重复 SBX-07~09，记录插件内证据。
