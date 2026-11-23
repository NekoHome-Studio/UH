"""Linux seccomp filter compilation and delegated plugin cgroups."""
from __future__ import annotations

import contextlib
import ctypes
import errno
import json
import os
import platform
import sys
import tempfile
import time
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import BinaryIO, Iterator
from uuid import uuid4

from .logging import get_logger, log_event

log = get_logger("kernel.sandbox.linux")
SECCOMP_MODES = ("disabled", "audit", "enforce")
_SCMP_ACT_ALLOW = 0x7FFF0000
_SCMP_ACT_LOG = 0x7FFC0000
_SCMP_ACT_ERRNO = 0x00050000
_SCMP_CMP_MASKED_EQ = 7
_CGROUP_PERIOD_US = 100_000
_CGROUP_PIDS = 64


def seccomp_mode() -> str:
    mode = os.environ.get("UH_SANDBOX_SECCOMP", "enforce")
    if mode not in SECCOMP_MODES:
        raise ValueError("UH_SANDBOX_SECCOMP must be disabled, audit or enforce")
    return mode


def _seccomp_rules(mode: str) -> tuple[int, list[dict]]:
    profile = json.loads(files("deploy.security").joinpath("seccomp.json").read_text(encoding="utf-8"))
    if profile["defaultAction"] != "SCMP_ACT_ERRNO":
        raise ValueError("enforcement requires a default-deny seccomp profile")
    if mode == "audit":
        allowed = [rule for rule in profile["syscalls"] if rule["action"] == "SCMP_ACT_ALLOW"]
        return _SCMP_ACT_LOG, allowed
    if mode != "enforce":
        raise ValueError(f"invalid filter mode: {mode}")
    return _SCMP_ACT_ERRNO | profile["defaultErrnoRet"], profile["syscalls"]


class _ArgCompare(ctypes.Structure):
    _fields_ = [
        ("arg", ctypes.c_uint),
        ("op", ctypes.c_int),
        ("datum_a", ctypes.c_uint64),
        ("datum_b", ctypes.c_uint64),
    ]


def _export_seccomp(mode: str, fd: int) -> None:
    if platform.machine() not in {"x86_64", "aarch64"}:
        raise ValueError("UH seccomp supports Linux x86_64 and aarch64")
    default, rules = _seccomp_rules(mode)
    lib = ctypes.CDLL("libseccomp.so.2")
    lib.seccomp_init.argtypes = (ctypes.c_uint32,)
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_release.argtypes = (ctypes.c_void_p,)
    lib.seccomp_release.restype = None
    lib.seccomp_syscall_resolve_name.argtypes = (ctypes.c_char_p,)
    lib.seccomp_rule_add_array.argtypes = (
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint, ctypes.POINTER(_ArgCompare),
    )
    lib.seccomp_export_bpf.argtypes = (ctypes.c_void_p, ctypes.c_int)
    context = lib.seccomp_init(default)
    if not context:
        raise OSError("seccomp_init failed")
    try:
        for rule in rules:
            if rule["action"] == "SCMP_ACT_ALLOW":
                action = _SCMP_ACT_ALLOW
            elif rule["action"] == "SCMP_ACT_ERRNO":
                action = _SCMP_ACT_ERRNO | rule["errnoRet"]
            else:
                raise ValueError("unsupported seccomp action")
            comparisons = []
            for arg in rule.get("args", []):
                if arg["op"] != "SCMP_CMP_MASKED_EQ":
                    raise ValueError("unsupported seccomp argument comparison")
                comparisons.append(_ArgCompare(arg["index"], _SCMP_CMP_MASKED_EQ, arg["value"], arg["valueTwo"]))
            args = (_ArgCompare * len(comparisons))(*comparisons)
            for name in rule["names"]:
                syscall = lib.seccomp_syscall_resolve_name(name.encode("ascii"))
                if syscall < 0:
                    # clone3 is 435 on both supported native architectures. Older
                    # libseccomp tables can lack it, but glibc still needs ENOSYS.
                    if name != "clone3":
                        continue
                    syscall = 435
                result = lib.seccomp_rule_add_array(context, action, syscall, len(args), args)
                if result < 0:
                    raise OSError(-result, f"libseccomp could not add {name}")
        result = lib.seccomp_export_bpf(context, fd)
        if result < 0:
            raise OSError(-result, "libseccomp could not export the filter")
    finally:
        lib.seccomp_release(context)


@contextlib.contextmanager
def seccomp_filter(mode: str) -> Iterator[BinaryIO | None]:
    if mode == "disabled":
        yield None
        return
    with tempfile.TemporaryFile() as bpf:
        _export_seccomp(mode, bpf.fileno())
        bpf.seek(0)
        yield bpf


def _write_control(path: Path, value: str) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"missing cgroup control: {path.name}")
    path.write_text(value, encoding="ascii")


@dataclass(frozen=True)
class PluginCgroup:
    directories: tuple[Path, ...]

    @classmethod
    def create(cls, root: Path, memory_mb: int) -> PluginCgroup:
        if not root.is_absolute() or not root.is_dir():
            raise ValueError("UH_SANDBOX_CGROUP_ROOT must be an absolute delegated directory")
        if memory_mb <= 0:
            raise ValueError("a delegated cgroup requires a positive memory limit")
        memory_bytes = str(memory_mb * 1024 * 1024)
        if (root / "cgroup.controllers").is_file():
            controllers = [(root, {
                "cpu.max": f"{_CGROUP_PERIOD_US} {_CGROUP_PERIOD_US}",
                "memory.max": memory_bytes,
                "pids.max": str(_CGROUP_PIDS),
            })]
        else:
            cpu = root / "cpu"
            if not cpu.is_dir():
                cpu = root / "cpu,cpuacct"
            controllers = [
                (cpu, {"cpu.cfs_period_us": str(_CGROUP_PERIOD_US), "cpu.cfs_quota_us": str(_CGROUP_PERIOD_US)}),
                (root / "memory", {"memory.limit_in_bytes": memory_bytes}),
                (root / "pids", {"pids.max": str(_CGROUP_PIDS)}),
            ]
        name = f"uh-plugin-{uuid4().hex}"
        created = []
        try:
            for parent, settings in controllers:
                child = parent / name
                child.mkdir()
                created.append(child)
                if not (child / "cgroup.procs").is_file():
                    raise OSError("missing cgroup.procs in delegated child cgroup")
                for setting, value in settings.items():
                    _write_control(child / setting, value)
        except BaseException:
            cls(tuple(created)).close()
            raise
        return cls(tuple(created))

    def launch_command(self, command: list[str]) -> list[str]:
        # Move a trusted, single-threaded launcher before it can fork or exec
        # bwrap. No preexec_fn runs in the multithreaded kernel process.
        controls = [str(path / "cgroup.procs") for path in self.directories]
        launcher = (
            "import os, sys\n"
            "end = sys.argv.index('--')\n"
            "for path in sys.argv[1:end]:\n"
            "    with open(path, 'w', encoding='ascii') as control:\n"
            "        control.write(str(os.getpid()))\n"
            "os.execv(sys.argv[end + 1], sys.argv[end + 1:])\n"
        )
        return [sys.executable, "-I", "-S", "-c", launcher, *controls, "--", *command]

    def close(self) -> None:
        for path in reversed(self.directories):
            for attempt in range(20):
                try:
                    path.rmdir()
                    break
                except FileNotFoundError:
                    break
                except OSError as exc:
                    if attempt == 0 and (path / "cgroup.kill").is_file():
                        with contextlib.suppress(OSError):
                            _write_control(path / "cgroup.kill", "1")
                    if exc.errno not in {errno.EBUSY, errno.ENOTEMPTY} or attempt == 19:
                        log_event(log, 30, "plugin.sandbox.cgroup_cleanup_failed", error_type=type(exc).__name__)
                        break
                    time.sleep(0.01)
