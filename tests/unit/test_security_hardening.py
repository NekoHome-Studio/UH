"""M3-7 policy, launch plumbing and Linux SBX acceptance probes."""
import asyncio
import contextlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from kernel import confirmation, linux_sandbox, sandbox
from kernel.plugins import PluginManifest

ROOT = Path(__file__).resolve().parents[2]
LINUX_ACCEPTANCE = pytest.mark.skipif(
    not sys.platform.startswith("linux") or os.environ.get("UH_RUN_LINUX_HARDENING") != "1",
    reason="requires a Linux bubblewrap host and UH_RUN_LINUX_HARDENING=1",
)


def _manifest():
    return PluginManifest(
        name="security-test", version="1.0.0", type="tool", description="test",
        author="test", license="MIT", entry="main.py",
        permissions=("fs.sandbox.read", "fs.sandbox.write"),
        config={"timeout_ms": 5000, "memory_limit_mb": 128},
        network=None, risk_level="read-only", raw={},
    )


def test_enforcement_profile_denies_namespace_and_sensitive_syscalls():
    action, rules = linux_sandbox._seccomp_rules("enforce")
    assert action == linux_sandbox._SCMP_ACT_ERRNO | 1
    allows = [rule for rule in rules if rule["action"] == "SCMP_ACT_ALLOW"]
    denied = {"mount", "ptrace", "kexec_load", "init_module", "bpf", "clone3", "unshare", "setns"}
    assert denied.isdisjoint(name for rule in allows for name in rule["names"])
    assert [rule["args"] for rule in allows if "clone" in rule["names"]] == [[
        {"index": 0, "value": 0x7E020080, "valueTwo": 0, "op": "SCMP_CMP_MASKED_EQ"},
    ]]
    assert next(rule["errnoRet"] for rule in rules if "clone3" in rule["names"]) == 38


def test_deployment_files_protect_kernel_with_writable_state():
    service = yaml.safe_load((ROOT / "docker-compose.yaml").read_text("utf-8"))["services"]["uh-kernel"]
    assert service["read_only"] is True
    assert service["user"] == "10001:10001"
    assert service["cap_drop"] == ["ALL"]
    assert "seccomp:./deploy/security/seccomp.json" in service["security_opt"]
    assert "no-new-privileges:true" in service["security_opt"]
    assert any(mount.startswith("/tmp:rw,") for mount in service["tmpfs"])
    assert "uh-state:/app/.uh" in service["volumes"]
    assert service["pids_limit"] > 0 and service["cpus"] > 0 and service["mem_limit"]

    deployment, *_ = yaml.safe_load_all((ROOT / "deploy/k8s/deployments.yaml").read_text("utf-8"))
    pod = deployment["spec"]["template"]["spec"]
    context = pod["securityContext"]
    assert context["runAsNonRoot"] is True and context["runAsUser"] == context["fsGroup"] == 10001
    assert context["seccompProfile"] == {"type": "Localhost", "localhostProfile": "uh/seccomp.json"}
    container = pod["containers"][0]
    assert container["securityContext"] == {
        "readOnlyRootFilesystem": True, "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
    }
    assert {"/tmp", "/app/.uh"} <= {mount["mountPath"] for mount in container["volumeMounts"]}
    assert {volume["name"] for volume in pod["volumes"] if "emptyDir" in volume} == {"tmp", "state"}
    kubelet = yaml.safe_load((ROOT / "deploy/security/kubelet-pids.yaml").read_text("utf-8"))
    assert kubelet["podPidsLimit"] == 256


@pytest.mark.parametrize("mode", ["disabled", "audit", "enforce"])
def test_seccomp_modes_control_fd_and_rules(mode, monkeypatch, tmp_path):
    exported = []

    def export(selected, fd):
        default, rules = linux_sandbox._seccomp_rules(selected)
        exported.append((default, rules))
        os.write(fd, b"bpf")

    monkeypatch.setattr(linux_sandbox, "_export_seccomp", export)
    monkeypatch.setenv("UH_SANDBOX_SECCOMP", mode)
    monkeypatch.delenv("UH_SANDBOX_CGROUP_ROOT", raising=False)
    plan = sandbox.SandboxPlan(("bwrap", "--", "/sandbox/main.py"), tmp_path, {}, "linux",
                               seccomp=linux_sandbox.seccomp_mode())
    with sandbox._spawn_options(plan, _manifest()) as (command, options, group):
        assert group is None
        if mode == "disabled":
            assert command == list(plan.command) and options == {} and exported == []
            return
        fd, = options["pass_fds"]
        assert command[1:3] == ["--seccomp", str(fd)]
        assert os.read(fd, 3) == b"bpf"
        default, rules = exported[0]
        if mode == "audit":
            assert default == linux_sandbox._SCMP_ACT_LOG
            assert all(rule["action"] == "SCMP_ACT_ALLOW" for rule in rules)
            assert not any("mount" in rule["names"] or "clone3" in rule["names"] for rule in rules)
        else:
            assert default == linux_sandbox._SCMP_ACT_ERRNO | 1
            assert any("mount" in rule["names"] and rule["errnoRet"] == 13 for rule in rules)
    with pytest.raises(OSError):
        os.fstat(fd)


def test_seccomp_mode_defaults_to_enforce_and_rejects_typos(monkeypatch):
    monkeypatch.delenv("UH_SANDBOX_SECCOMP", raising=False)
    assert linux_sandbox.seccomp_mode() == "enforce"
    monkeypatch.setenv("UH_SANDBOX_SECCOMP", "permissive")
    with pytest.raises(ValueError, match="UH_SANDBOX_SECCOMP"):
        linux_sandbox.seccomp_mode()


def test_missing_filter_fails_before_spawn(monkeypatch, tmp_path):
    def unavailable(*args):
        raise OSError("libseccomp missing")

    monkeypatch.setattr(linux_sandbox, "_export_seccomp", unavailable)
    plan = sandbox.SandboxPlan(("bwrap",), tmp_path, {}, "linux", seccomp="enforce")
    with pytest.raises(sandbox.SandboxError) as error, sandbox._spawn_options(plan, _manifest()):
        pytest.fail("must fail before spawning")
    assert error.value.detail["violation"] == "linux_hardening"


@pytest.mark.parametrize(("uid", "euid", "oci", "violation"), [
    (0, 0, "0", "sandbox_uid"),
    (10001, 0, "0", "sandbox_uid"),
    (10001, 10001, "1", "sandbox_unavailable"),
])
def test_linux_launch_rejects_root_or_incompatible_outer_profile(uid, euid, oci, violation, monkeypatch, tmp_path):
    linux_os = SimpleNamespace(**vars(os))
    linux_os.name, linux_os.getuid, linux_os.geteuid = "posix", lambda: uid, lambda: euid
    monkeypatch.setattr(sandbox, "os", linux_os)
    monkeypatch.setattr(sandbox, "sys", SimpleNamespace(platform="linux", executable=sys.executable))
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: "/usr/bin/bwrap")
    monkeypatch.setenv("UH_SANDBOX_OCI_STRICT", oci)
    with pytest.raises(sandbox.SandboxError) as error:
        sandbox.SandboxRunner(root=tmp_path).build_plan(_manifest(), tmp_path)
    assert error.value.detail["violation"] == violation


@pytest.mark.parametrize("version", [1, 2])
def test_delegated_cgroup_programs_cpu_memory_and_pids_before_exec(version, monkeypatch, tmp_path):
    if version == 2:
        (tmp_path / "cgroup.controllers").touch()
        settings = {tmp_path: {"cpu.max": "100000 100000", "memory.max": "134217728", "pids.max": "64"}}
    else:
        settings = {
            tmp_path / "cpu,cpuacct": {"cpu.cfs_period_us": "100000", "cpu.cfs_quota_us": "100000"},
            tmp_path / "memory": {"memory.limit_in_bytes": "134217728"},
            tmp_path / "pids": {"pids.max": "64"},
        }
        for controller in settings:
            controller.mkdir()

    mkdir, rmdir = Path.mkdir, Path.rmdir

    def mount_like(path, *args, **kwargs):
        mkdir(path, *args, **kwargs)
        if path.parent in settings:
            for control in ("cgroup.procs", *settings[path.parent]):
                (path / control).touch()

    def remove_like(path):
        if path.parent in settings:
            for control in path.iterdir():
                control.unlink()
        rmdir(path)

    monkeypatch.setattr(Path, "mkdir", mount_like)
    monkeypatch.setattr(Path, "rmdir", remove_like)
    group = linux_sandbox.PluginCgroup.create(tmp_path, 128)
    assert len(group.directories) == len(settings)
    assert len({child.name for child in group.directories}) == 1
    for child in group.directories:
        for control, value in settings[child.parent].items():
            assert (child / control).read_text("ascii") == value
    launcher = group.launch_command(["/usr/bin/bwrap", "--", "/sandbox/main.py"])
    assert launcher[1:4] == ["-I", "-S", "-c"]
    assert launcher[5:] == [*(str(child / "cgroup.procs") for child in group.directories),
                            "--", "/usr/bin/bwrap", "--", "/sandbox/main.py"]
    assert launcher[4].index("control.write") < launcher[4].index("os.execv")
    group.close()
    assert all(not child.exists() for child in group.directories)


def test_popen_and_mcp_use_the_same_spawn_options(monkeypatch, tmp_path):
    plan = sandbox.SandboxPlan(("bwrap", "--", "plugin"), tmp_path, {}, "linux", seccomp="audit")
    runner = sandbox.SandboxRunner(root=tmp_path)
    manifest = _manifest()
    monkeypatch.setattr(runner, "build_plan", lambda *_: plan)
    monkeypatch.setattr(sandbox._WindowsJob, "create", lambda *_: None)
    calls = []

    @contextlib.contextmanager
    def resolved(received_plan, received_manifest):
        assert (received_plan, received_manifest) == (plan, manifest)
        yield ["bwrap", "--seccomp", "6", "--", "plugin"], {"pass_fds": (6,)}, None

    def popen(command, **options):
        calls.append((command, options["pass_fds"]))
        return SimpleNamespace(pid=123)

    async def spawn(*command, **options):
        return popen(list(command), **options)

    monkeypatch.setattr(sandbox, "_spawn_options", resolved)
    monkeypatch.setattr(sandbox.subprocess, "Popen", popen)
    monkeypatch.setattr(sandbox.asyncio, "create_subprocess_exec", spawn)
    runner.launch(manifest, tmp_path)
    transport = sandbox._SandboxStdioTransport(manifest, plan)
    asyncio.run(transport._spawn(list(plan.command), {}))
    assert calls == [(["bwrap", "--seccomp", "6", "--", "plugin"], (6,))] * 2


def test_confirmation_replay_wrong_task_cancel_and_ttl(monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(confirmation, "_now", lambda: now)
    manager = confirmation.ConfirmationManager(ttl_s=60)

    first = manager.request("task-a", "tool", "write-host", "write fixture")
    with pytest.raises(confirmation.ConfirmationError) as wrong_task:
        manager.resolve(first.token, "approved", task_id="task-b")
    assert wrong_task.value.code == "UH-USR-1002" and first.status == "pending"
    manager.resolve(first.token, "approved", task_id="task-a")
    with pytest.raises(confirmation.ConfirmationError) as replay:
        manager.resolve(first.token, "approved", task_id="task-a")
    assert replay.value.detail["status"] == "approved"

    cancelled = manager.request("task-a", "tool", "write-host", "write fixture")
    expired = manager.request("task-b", "tool", "irreversible", "update fixture")
    manager.invalidate_task("task-a")
    with pytest.raises(confirmation.ConfirmationError):
        manager.resolve(cancelled.token, "approved", task_id="task-a")
    assert cancelled.operator == "cancelled" and expired.status == "pending"
    now += timedelta(seconds=61)
    with pytest.raises(confirmation.ConfirmationError):
        manager.resolve(expired.token, "approved", task_id="task-b")
    assert expired.status == "rejected" and expired.operator == "timeout"
    assert [record["operator"] for record in manager.audit] == ["user", "cancelled", "timeout"]


@pytest.fixture
def linux_probe(tmp_path, monkeypatch):
    monkeypatch.setenv("UH_SANDBOX_SECCOMP", "enforce")
    package = tmp_path / "plugin"
    package.mkdir()
    runner = sandbox.SandboxRunner(root=tmp_path)

    def run(source):
        (package / "main.py").write_text(source, encoding="utf-8")
        status, out, err = asyncio.run(runner.run(_manifest(), package))
        assert status == 0, err.decode("utf-8", errors="replace")
        return json.loads(out)

    return run


@LINUX_ACCEPTANCE
def test_sbx_07_linux_seccomp_denies_mount_and_ptrace(linux_probe):
    result = linux_probe("""
import ctypes, errno, json, threading
libc = ctypes.CDLL(None, use_errno=True)
assert libc.mount(None, None, None, ctypes.c_ulong(0), None) == -1
assert ctypes.get_errno() == errno.EACCES
assert libc.ptrace(ctypes.c_int(2), ctypes.c_int(-1), None, None) == -1
assert ctypes.get_errno() == errno.EACCES
thread = threading.Thread(target=lambda: None)
thread.start()
thread.join()
print(json.dumps({"mount": "EACCES", "ptrace": "EACCES", "thread": "ok"}))
""")
    assert result == {"mount": "EACCES", "ptrace": "EACCES", "thread": "ok"}


@LINUX_ACCEPTANCE
def test_sbx_08_linux_root_is_read_only_and_writable_mounts_work(linux_probe):
    result = linux_probe("""
import errno, json, os
from pathlib import Path
assert all(os.statvfs(path).f_flag & os.ST_RDONLY for path in ("/", "/proc", "/dev", "/dev/pts", "/usr"))
try:
    Path("/.uh-sbx-probe").write_text("fixture")
except OSError as error:
    assert error.errno == errno.EROFS
else:
    raise AssertionError("unexpected write to sandbox root")
for directory in ("/sandbox", "/tmp"):
    probe = Path(directory) / "sbx-probe"
    probe.write_text("fixture")
    assert probe.read_text() == "fixture"
print(json.dumps({"root": "EROFS", "sandbox": "ok", "tmp": "ok"}))
""")
    assert result == {"root": "EROFS", "sandbox": "ok", "tmp": "ok"}


@LINUX_ACCEPTANCE
def test_sbx_09_linux_plugin_uid_is_non_root(linux_probe):
    result = linux_probe("""
import json, os
print(json.dumps({"uid": os.getuid(), "euid": os.geteuid()}))
""")
    assert result == {"uid": os.getuid(), "euid": os.getuid()}
    assert result["euid"] != 0


@LINUX_ACCEPTANCE
def test_linux_plugin_inherits_delegated_cgroup(linux_probe):
    if not os.environ.get("UH_SANDBOX_CGROUP_ROOT"):
        pytest.skip("requires a delegated cgroup configured with UH_SANDBOX_CGROUP_ROOT")
    result = linux_probe("""
import json
from pathlib import Path
print(json.dumps(Path("/proc/self/cgroup").read_text().splitlines()))
""")
    if any(entry.startswith("0::") and "uh-plugin-" in entry for entry in result):
        return
    controllers = {
        controller
        for entry in result if "uh-plugin-" in entry
        for controller in entry.split(":")[1].split(",")
    }
    assert {"cpu", "memory", "pids"} <= controllers
