# M2-2 沙箱实现对照

> 状态：待评审。当前提交实现策略层、运行计划和内核可选接线，三平台集成验收分阶段完成。

## 当前实现

- `SandboxPolicy` 检查插件私有目录、`fs.sandbox.read/write`、`fs.host.read` 和 `network.allow_hosts`。
- `SandboxRunner` 读取 manifest 的 `timeout_ms` 与 `memory_limit_mb`，生成运行计划并注入沙箱环境变量。
- `ProcessPluginLoader` 已接入主应用的 `PluginDriver`，负责 tool/capability/hook 的独立进程启动、优雅停用和强杀。主题插件不启动进程。
- 插件进程异常退出会自动重启，注册表累计连续崩溃次数，达到 3 次后进入 `quarantined`，不再自动重启。
- Windows 使用 Job Object 绑定插件进程，设置内存上限、进程组终止和 Job 时间上限。
- Linux 有 `bwrap` 时生成 `--unshare-pid`、`--unshare-net`、只读运行时和私有 `/sandbox` 命令。
- macOS 有 `sandbox-exec` 时保留平台计划入口。Windows M2 的文件和网络属于 SDK/代理行为级拦截，状态显式标记 `degraded`。

## 后续验收

- SBX-01/02 在 Windows 上补 SDK/代理真实拦截与审计事件。
- SBX-03/04/05 接入插件驱动，完成超时、内存、崩溃计数和 quarantine 状态闭环。
- Linux/macOS 在对应 CI 或验收机执行平台级隔离测试。

