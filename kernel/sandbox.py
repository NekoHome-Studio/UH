"""M2-2 沙箱策略与进程运行器的第一层实现。

插件驱动负责可信安装，沙箱负责运行时的进程、路径、网络和资源边界。
Windows M2 使用 Job Object 加进程级资源限制，文件和网络策略由 SDK/代理
通过 :class:`SandboxPolicy` 执行。Linux/macOS 生成各自的最小隔离命令，
后续验收环境再补平台级集成测试。
"""
from __future__ import annotations

import asyncio
import fnmatch
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import urlparse

from .plugins import PluginManifest


class SandboxError(Exception):
    def __init__(self, code: str, message: str, *, detail: dict[str, Any] | None = None, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}
        self.retryable = retryable

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "retry_after_ms": 0,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class SandboxLimits:
    timeout_ms: int
    memory_limit_mb: int
    cpu_seconds: int = 1


@dataclass
class SandboxPolicy:
    """运行时权限检查，供 SDK 与代理使用。"""

    plugin_dir: Path
    permissions: set[str] = field(default_factory=set)
    allow_hosts: tuple[str, ...] = ()

    def _inside_plugin_dir(self, value: str | Path) -> bool:
        root = self.plugin_dir.resolve()
        candidate = Path(value).resolve()
        return candidate == root or root in candidate.parents

    def check_path(self, value: str | Path, *, write: bool = False) -> Path:
        candidate = Path(value)
        inside = self._inside_plugin_dir(candidate)
        if inside:
            if write and "fs.sandbox.write" not in self.permissions:
                raise SandboxError("UH-KRN-2010", "插件未声明沙箱写权限", detail={"resource": str(value), "violation": "fs.sandbox.write"})
            if not write and "fs.sandbox.read" not in self.permissions:
                raise SandboxError("UH-KRN-2010", "插件未声明沙箱读权限", detail={"resource": str(value), "violation": "fs.sandbox.read"})
            return candidate.resolve()
        if not write and "fs.host.read" in self.permissions:
            return candidate.resolve()
        raise SandboxError("UH-KRN-2010", "插件越权访问文件", detail={"resource": str(value), "violation": "filesystem"})

    def check_host(self, host_or_url: str) -> str:
        if "network.outbound" not in self.permissions:
            raise SandboxError("UH-KRN-2010", "插件未声明出网权限", detail={"resource": host_or_url, "violation": "network.outbound"})
        parsed = urlparse(host_or_url if "://" in host_or_url else "//" + host_or_url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if not host or not any(fnmatch.fnmatchcase(host, pattern.lower()) for pattern in self.allow_hosts):
            raise SandboxError("UH-KRN-2010", "出站目标不在插件白名单", detail={"resource": host_or_url, "violation": "network.allow_hosts"})
        return host


@dataclass(frozen=True)
class SandboxPlan:
    command: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str]
    platform: str
    degraded: bool = False
    reason: str | None = None


def _entry_command(manifest: PluginManifest, package_dir: Path) -> list[str]:
    if manifest.type == "theme":
        raise SandboxError("UH-USR-1003", "主题插件没有可执行入口", detail={"plugin": manifest.name})
    if not manifest.entry:
        raise SandboxError("UH-USR-1003", "插件缺少执行入口", detail={"plugin": manifest.name})
    entry = (package_dir / manifest.entry).resolve()
    if package_dir.resolve() not in entry.parents:
        raise SandboxError("UH-KRN-2010", "插件入口路径越界", detail={"plugin": manifest.name, "violation": "entry"})
    if entry.suffix.lower() == ".py":
        return [sys.executable, str(entry)]
    if entry.suffix.lower() in {".js", ".mjs"}:
        return ["node", str(entry)]
    return [str(entry)]


class SandboxProcess:
    def __init__(self, process: subprocess.Popen, job: Any = None):
        self.process = process
        self.job = job

    def terminate(self) -> None:
        if self.job is not None:
            self.job.terminate()
        elif self.process.poll() is None:
            self.process.kill()

    def close(self) -> None:
        if self.job is not None:
            self.job.close()

    async def wait(self, timeout_s: float | None = None) -> int:
        if timeout_s is None:
            return await asyncio.to_thread(self.process.wait)
        return await asyncio.to_thread(self.process.wait, timeout=timeout_s)


class ProcessPluginLoader:
    """把插件生命周期连接到独立沙箱进程。

    MCP/RPC/Hook 协议握手由后续 SDK 负责。此加载器先保证每个插件有独立
    进程、可检测启动失败，并能在 deactivate 超时后被 PluginDriver 强杀。
    """

    def __init__(self, runner: "SandboxRunner", on_crash: Callable[[PluginManifest, Path], Awaitable[bool]] | None = None) -> None:
        self.runner = runner
        self.on_crash = on_crash
        self.processes: dict[str, SandboxProcess] = {}
        self.monitors: dict[str, asyncio.Task] = {}

    async def load(self, manifest: PluginManifest, package_dir: Path) -> None:
        self.runner.build_plan(manifest, package_dir)

    async def activate(self, manifest: PluginManifest, package_dir: Path) -> None:
        if manifest.name in self.processes:
            return
        process = self.runner.launch(manifest, package_dir)
        await asyncio.sleep(0)
        if process.process.poll() is not None:
            process.close()
            raise SandboxError("UH-KRN-2010", "插件进程启动后立即退出", detail={"plugin": manifest.name, "violation": "plugin_crash"})
        self.processes[manifest.name] = process
        self.monitors[manifest.name] = asyncio.create_task(self._monitor(manifest, package_dir, process))

    async def _monitor(self, manifest: PluginManifest, package_dir: Path, process: SandboxProcess) -> None:
        try:
            await process.wait()
        except Exception:
            return
        current = self.processes.get(manifest.name)
        if current is not process:
            process.close()
            return
        self.processes.pop(manifest.name, None)
        self.monitors.pop(manifest.name, None)
        process.close()
        if self.on_crash is None:
            return
        try:
            restart = await self.on_crash(manifest, package_dir)
            if restart:
                await self.activate(manifest, package_dir)
        except Exception:
            # A failed restart is accounted for by the next explicit lifecycle
            # action. The registry remains quarantined once the driver reaches
            # its crash threshold.
            return

    async def deactivate(self, manifest: PluginManifest, package_dir: Path) -> None:
        process = self.processes.pop(manifest.name, None)
        monitor = self.monitors.pop(manifest.name, None)
        if monitor is not None:
            monitor.cancel()
        if process is None:
            return
        process.terminate()
        try:
            await process.wait(timeout_s=5.0)
        finally:
            process.close()

    async def uninstall(self, manifest: PluginManifest, package_dir: Path) -> None:
        return None

    def force_kill(self, manifest: PluginManifest, package_dir: Path) -> None:
        process = self.processes.pop(manifest.name, None)
        monitor = self.monitors.pop(manifest.name, None)
        if monitor is not None:
            monitor.cancel()
        if process is not None:
            process.terminate()
            process.close()


class SandboxRunner:
    def __init__(self, *, enforce: bool = True, root: str | Path | None = None):
        self.enforce = enforce
        self.root = Path(root or os.environ.get("UH_PLUGIN_ROOT", ".uh/plugins"))

    def policy(self, manifest: PluginManifest, package_dir: str | Path) -> SandboxPolicy:
        package_path = Path(package_dir).resolve()
        root = self.root.resolve()
        if package_path != root and root not in package_path.parents:
            raise SandboxError("UH-KRN-2010", "插件工作目录越界", detail={"plugin": manifest.name, "violation": "plugin_root"})
        return SandboxPolicy(
            plugin_dir=package_path,
            permissions=set(manifest.permissions),
            allow_hosts=tuple((manifest.network or {}).get("allow_hosts", [])),
        )

    def build_plan(self, manifest: PluginManifest, package_dir: str | Path) -> SandboxPlan:
        package_path = Path(package_dir).resolve()
        policy = self.policy(manifest, package_path)
        command = _entry_command(manifest, package_path)
        if manifest.type == "hook":
            # Only inherit OS runtime variables. Hook children receive event
            # metadata, and do not need credentials or application settings.
            env = {name: os.environ[name] for name in ("SYSTEMROOT", "WINDIR", "SYSTEMDRIVE") if name in os.environ}
            env["PATH"] = os.defpath
        else:
            env = os.environ.copy()
        env.update({
            "UH_SANDBOX_ROOT": str(package_path),
            "UH_SANDBOX_NETWORK": "allow" if "network.outbound" in policy.permissions else "deny",
            "UH_SANDBOX_ALLOW_HOSTS": ",".join(policy.allow_hosts),
        })
        limits = SandboxLimits(
            timeout_ms=int(manifest.config.get("timeout_ms", 5000)),
            memory_limit_mb=int(manifest.config.get("memory_limit_mb", 256)),
        )
        env["UH_SANDBOX_TIMEOUT_MS"] = str(limits.timeout_ms)
        env["UH_SANDBOX_MEMORY_LIMIT_MB"] = str(limits.memory_limit_mb)

        if os.name == "nt":
            return SandboxPlan(tuple(command), package_path, env, "windows", degraded=True, reason="M2 Windows FS/network enforcement is SDK/代理级")
        if manifest.type != "hook" and sys.platform == "darwin" and shutil.which("sandbox-exec"):
            return SandboxPlan(tuple(command), package_path, env, "macos")
        if sys.platform.startswith("linux") and shutil.which("bwrap"):
            wrapped = [
                "bwrap", "--die-with-parent", "--unshare-pid", "--unshare-net",
                "--ro-bind", "/usr", "/usr", "--ro-bind", "/bin", "/bin",
                "--ro-bind", "/lib", "/lib", "--bind", str(package_path), "/sandbox",
                "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--chdir", "/sandbox",
            ]
            if manifest.type == "hook":
                entry = Path(command[-1]).relative_to(package_path).as_posix()
                command[-1] = "/sandbox/" + entry
                if command[0] == sys.executable:
                    command[0] = "/usr/bin/python3"
                if Path("/lib64").exists():
                    wrapped.extend(["--ro-bind", "/lib64", "/lib64"])
                env["UH_SANDBOX_ROOT"] = "/sandbox"
            wrapped += command
            return SandboxPlan(tuple(wrapped), package_path, env, "linux")
        if self.enforce:
            raise SandboxError("UH-KRN-2010", "当前平台没有可用的 M2 沙箱运行器", detail={"plugin": manifest.name, "violation": "sandbox_unavailable"})
        return SandboxPlan(tuple(command), package_path, env, sys.platform, degraded=True, reason="sandbox runner unavailable")

    def launch(self, manifest: PluginManifest, package_dir: str | Path, *, capture_output: bool = True) -> SandboxProcess:
        plan = self.build_plan(manifest, package_dir)
        process = subprocess.Popen(
            list(plan.command),
            cwd=str(plan.cwd),
            env=dict(plan.env),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE if capture_output else subprocess.DEVNULL,
            stderr=subprocess.PIPE if capture_output else subprocess.DEVNULL,
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW) if os.name == "nt" else 0,
        )
        try:
            job = _WindowsJob.create(manifest, process, int(manifest.config.get("memory_limit_mb", 256)), int(manifest.config.get("timeout_ms", 5000))) if os.name == "nt" else None
        except Exception:
            process.kill()
            process.communicate()
            raise
        return SandboxProcess(process, job)

    async def run(self, manifest: PluginManifest, package_dir: str | Path, input_data: bytes | None = None, *, capture_output: bool = True) -> tuple[int, bytes, bytes]:
        sandbox_process = self.launch(manifest, package_dir, capture_output=capture_output)
        timeout_s = int(manifest.config.get("timeout_ms", 5000)) / 1000.0
        communication = asyncio.create_task(asyncio.to_thread(sandbox_process.process.communicate, input_data))
        try:
            stdout, stderr = await asyncio.wait_for(asyncio.shield(communication), timeout=timeout_s)
        except asyncio.TimeoutError as exc:
            sandbox_process.terminate()
            await communication
            raise SandboxError("UH-CAP-5021", "插件执行超时", retryable=True, detail={"module": "plugin", "timeout_ms": int(timeout_s * 1000)}) from exc
        except asyncio.CancelledError:
            sandbox_process.terminate()
            await communication
            raise
        finally:
            sandbox_process.close()
        return sandbox_process.process.returncode, stdout or b"", stderr or b""


class _WindowsJob:
    """Windows Job Object 适配。类在非 Windows 上保持不可调用。"""

    def __init__(self, handle: Any, kernel32: Any):
        self.handle = handle
        self.kernel32 = kernel32

    @classmethod
    def create(cls, manifest: PluginManifest, process: subprocess.Popen, memory_limit_mb: int, timeout_ms: int):
        if os.name != "nt":
            return None
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise SandboxError("UH-KRN-2010", "无法创建 Windows Job Object", detail={"plugin": manifest.name, "violation": "job_object"})

        class BasicLimit(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong), ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimit), ("IoInfo", IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        info = ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = 0x100 | 0x2000  # PROCESS_MEMORY + KILL_ON_JOB_CLOSE
        info.ProcessMemoryLimit = memory_limit_mb * 1024 * 1024
        if timeout_ms > 0:
            info.BasicLimitInformation.LimitFlags |= 0x4  # JOB_TIME
            info.BasicLimitInformation.PerJobUserTimeLimit = timeout_ms * 10000
        if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(handle)
            raise SandboxError("UH-KRN-2010", "无法配置 Windows Job Object", detail={"plugin": manifest.name, "violation": "job_limits"})
        if not kernel32.AssignProcessToJobObject(handle, wintypes.HANDLE(process._handle)):
            kernel32.CloseHandle(handle)
            raise SandboxError("UH-KRN-2010", "无法绑定插件进程到 Job Object", detail={"plugin": manifest.name, "violation": "job_assign"})
        return cls(handle, kernel32)

    def terminate(self) -> None:
        if self.handle:
            self.kernel32.TerminateJobObject(self.handle, 1)

    def close(self) -> None:
        if self.handle:
            self.kernel32.CloseHandle(self.handle)
            self.handle = None


__all__ = ["ProcessPluginLoader", "SandboxError", "SandboxLimits", "SandboxPlan", "SandboxPolicy", "SandboxProcess", "SandboxRunner"]
