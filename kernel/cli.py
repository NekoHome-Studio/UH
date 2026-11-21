"""UH! CLI（M0-6，§7.2 / §11.4）：uh dev up/down/logs/reset/watch。

一键本地启动（本地优先）。内核服务永远在；--ui 决定 UI 载体（web/desktop/both）。
Desktop 壳（Tauri）M3-6 实装，M0 下 desktop/both 优雅降级为内核+Web 并提示。
配置经 uh-dev.yaml；状态存 .uh/（pid/log）。跨平台进程管理。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error
import webbrowser
from pathlib import Path

import yaml

from .long_term_memory import create_long_term_memory
from .plugins import PluginDriver, PluginError, PluginManifest
from .protocol import CapabilityRegistry, EventBus, StorageGuard
from .sandbox import SandboxRunner

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / ".uh"
PID_FILE = STATE / "dev.pid"
LOG_FILE = STATE / "kernel.log"

DEFAULT_CONFIG = {
    "mode": "local",
    "ui": {"mode": "web", "port": 8080},
    "desktop": {"shell": "tauri"},
    "llm": {"provider": "openai", "model": "gpt-4o", "base_url": "https://api.openai.com/v1"},
    "memory": {"short_term": "redis://localhost:6379", "long_term": "chroma://./data/vector"},
    "logging": {"level": "info"},
}


def load_config(path: Path | None = None) -> dict:
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULT_CONFIG.items()}
    p = path or (ROOT / "uh-dev.yaml")
    if p.exists():
        loaded = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        for k, v in loaded.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


def resolve_ui(mode: str) -> dict:
    if mode not in ("web", "desktop", "both"):
        raise ValueError("invalid ui mode: " + mode)
    return {"kernel": True, "web": mode in ("web", "both"), "desktop": mode in ("desktop", "both"), "open_browser": mode in ("web", "both")}


def _read_pid() -> int | None:
    if PID_FILE.exists():
        try:
            return int(PID_FILE.read_text().strip())
        except ValueError:
            return None
    return None


def _terminate(pid: int) -> None:
    if os.name == "nt":
        import _winapi

        result = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        if result.returncode and _alive(pid):
            try:
                process = _winapi.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
            except OSError as exc:
                if exc.winerror != 87:  # ERROR_INVALID_PARAMETER: process already exited
                    raise
            else:
                try:
                    _winapi.TerminateProcess(process, 1)
                finally:
                    _winapi.CloseHandle(process)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def _wait_health(port: int, timeout: float = 45.0) -> bool:
    deadline = time.time() + timeout
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # localhost 健康检查禁走系统代理
    while time.time() < deadline:
        try:
            with opener.open("http://127.0.0.1:%d/health" % port, timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def dev_up(args) -> int:
    cfg = load_config()
    port = args.port or cfg["ui"]["port"]
    ui = resolve_ui(args.ui or cfg["ui"]["mode"])
    STATE.mkdir(exist_ok=True)
    if _read_pid() and _alive(_read_pid()):
        print("UH! 已在运行（pid %s），先 uh dev down" % _read_pid())
        return 1
    log = open(LOG_FILE, "ab")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "kernel.main:app", "--host", args.host, "--port", str(port)],
        cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT,
    )
    PID_FILE.write_text(str(proc.pid))
    if not _wait_health(port):
        print("内核启动超时，查看 uh dev logs")
        return 1
    print("UH! 内核已起：http://%s:%d （pid %s）" % (args.host, port, proc.pid))
    if ui["desktop"]:
        print("提示：Desktop 壳（Tauri）M3-6 实装，M0 已起内核+Web；desktop/both 暂降级为 Web。")
    if ui["open_browser"] and not args.no_browser:
        webbrowser.open("http://127.0.0.1:%d" % port)
    return 0


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        import _winapi

        try:
            process = _winapi.OpenProcess(_winapi.SYNCHRONIZE, False, pid)
        except OSError as exc:
            return exc.winerror == 5  # ERROR_ACCESS_DENIED: the process exists
        try:
            return _winapi.WaitForSingleObject(process, 0) == _winapi.WAIT_TIMEOUT
        finally:
            _winapi.CloseHandle(process)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def dev_down(args) -> int:
    pid = _read_pid()
    if pid:
        _terminate(pid)
        # 三审 GPT-6 P2-3：等待子进程完全退出，防 reset 删日志后子进程重建（flaky 根因）
        for _ in range(30):
            if not _alive(pid):
                break
            time.sleep(0.1)
        PID_FILE.unlink(missing_ok=True)
        print("UH! 已停止（pid %s）" % pid)
    else:
        print("UH! 未在运行")
    return 0


def dev_logs(args) -> int:
    if not LOG_FILE.exists():
        print("无日志（先 uh dev up）")
        return 0
    if not args.follow:
        print(LOG_FILE.read_text(encoding="utf-8", errors="replace"))
        return 0
    with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
        f.seek(0, 2)
        try:
            while True:
                line = f.readline()
                if line:
                    print(line, end="")
                else:
                    time.sleep(0.3)
        except KeyboardInterrupt:
            pass
    return 0


def dev_reset(args) -> int:
    dev_down(args)
    cleaned = []
    if LOG_FILE.exists():
        LOG_FILE.unlink(); cleaned.append("日志")
    for f in STATE.glob("*.pid"):
        f.unlink()
    if not args.keep_vectors:
        vec = ROOT / "data" / "vector"
        if vec.exists():
            import shutil
            shutil.rmtree(vec, ignore_errors=True); cleaned.append("向量库")
    cfg = load_config()
    st = cfg["memory"]["short_term"]
    if st.startswith("redis://"):
        try:
            import redis
            r = redis.from_url(st)
            r.flushdb(); r.close(); cleaned.append("Redis")
        except Exception as e:
            print("Redis 清理跳过：%s" % e)
    print("已清理：" + ("、".join(cleaned) if cleaned else "无残留"))
    return 0


def dev_watch(args) -> int:
    cfg = load_config()
    port = args.port or cfg["ui"]["port"]
    print("热重载模式（uvicorn --reload），端口 %d" % port)
    return subprocess.call([sys.executable, "-m", "uvicorn", "kernel.main:app", "--host", args.host, "--port", str(port), "--reload"], cwd=str(ROOT))


def memory_purge(args) -> int:
    try:
        count = create_long_term_memory().purge(args.before)
    except (TypeError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    result = {"purged_count": count}
    if args.before:
        result["before"] = args.before
    print(json.dumps(result, ensure_ascii=False))
    return 0


def memory_export(args) -> int:
    try:
        # CLI 默认只导出脱敏内容；敏感明文导出需通过受保护的 REST 授权路径。
        result = create_long_term_memory().export(authorize_sensitive=False, redact_sensitive=True)
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps({"entries": result}, ensure_ascii=False))
    return 0


def confirm_task(args) -> int:
    """M1-8：CLI 确认/拒绝高危工具（经 REST，与 UI 同等效力）。"""
    port = args.port or 8080
    decision = "approved" if args.approve else "rejected"
    url = "http://%s:%d/api/v1/tasks/%s/confirm" % (args.host, port, args.task)
    body = json.dumps({"confirmation_token": args.token, "decision": decision, "operator": args.operator}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=10) as r:
            print(r.read().decode("utf-8"))
            return 0
    except urllib.error.HTTPError as exc:
        print(exc.read().decode("utf-8"), file=sys.stderr)
        return 1
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


def _plugin_driver() -> PluginDriver:
    return PluginDriver(sandbox_runner=SandboxRunner(), capability_registry=CapabilityRegistry(EventBus(), StorageGuard()))


def plugin_validate(args) -> int:
    try:
        result = _plugin_driver().validate(args.source, no_verify=args.no_verify)
    except PluginError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def plugin_install(args) -> int:
    async def install():
        driver = _plugin_driver()
        try:
            return await driver.install(args.source, no_verify=args.no_verify)
        finally:
            await driver.shutdown()

    try:
        result = asyncio.run(install())
    except PluginError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def plugin_uninstall(args) -> int:
    try:
        result = asyncio.run(_plugin_driver().uninstall(args.name))
    except PluginError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def plugin_list(args) -> int:
    print(json.dumps(_plugin_driver().list(), ensure_ascii=False, indent=2))
    return 0


def plugin_audit(args) -> int:
    rows = []
    for item in _plugin_driver().list():
        rows.append({
            "name": item.get("name"),
            "version": item.get("version"),
            "type": item.get("type"),
            "status": item.get("status"),
            "risk_level": item.get("risk_level"),
            "permissions": item.get("permissions", []),
            "package_sha256": item.get("package_sha256"),
        })
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def plugin_dev(args) -> int:
    source = Path(args.source).resolve()
    if not source.is_dir():
        print("uh plugin dev 需要插件源目录", file=sys.stderr)
        return 1
    manifest_path = source / "manifest.yaml"
    if not manifest_path.exists():
        print("插件目录缺少 manifest.yaml", file=sys.stderr)
        return 1
    try:
        manifest = PluginManifest.from_dict(yaml.safe_load(manifest_path.read_text(encoding="utf-8")))
        runner = SandboxRunner(enforce=False, root=source.parent)
        plan = runner.build_plan(manifest, source)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    env = dict(plan.env)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    watched = [manifest_path, source / (manifest.entry or "main.py")]
    process = None

    def start():
        nonlocal process
        # F3：--once 需 stdio 管道灌 JSON-RPC；热重载模式保持继承（插件自行走 stdio）
        pipes = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE} if args.once else {}
        process = subprocess.Popen(list(plan.command), cwd=str(plan.cwd), env=env, **pipes)

    def stop():
        nonlocal process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
        process = None

    def handshake_once() -> int:
        """F3 修复：--once 真灌 MCP 握手（initialize→tools/list→tools/call）并打印响应，
        而非静默 start+wait（此前 rc=0 无输出，第三方无法判断插件可用）。"""
        assert process is not None and process.stdin and process.stdout
        def rpc(payload: dict) -> dict | None:
            assert process is not None and process.stdin and process.stdout
            process.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
            process.stdin.flush()
            line = process.stdout.readline()
            if not line:
                return None
            try:
                return json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                return {"raw": line.decode("utf-8", errors="replace").rstrip()}
        init = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "uh-dev", "version": "0.1.0"}}})
        print(json.dumps({"request": "initialize", "response": init}, ensure_ascii=False))
        if init is None:
            print("插件未响应 initialize（进程可能已退出）", file=sys.stderr)
            return 1
        listed = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        print(json.dumps({"request": "tools/list", "response": listed}, ensure_ascii=False))
        tools_ = ((listed or {}).get("result") or {}).get("tools") or []
        if tools_:
            name = str(tools_[0].get("name", ""))
            called = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": name, "arguments": {}}})
            print(json.dumps({"request": "tools/call", "tool": name, "response": called}, ensure_ascii=False))
        return 0

    try:
        start()
        if args.once:
            try:
                return handshake_once()
            finally:
                stop()
        mtimes = {path: path.stat().st_mtime_ns for path in watched if path.exists()}
        while True:
            time.sleep(0.5)
            changed = any(path.exists() and path.stat().st_mtime_ns != mtimes.get(path) for path in watched)
            if changed:
                mtimes = {path: path.stat().st_mtime_ns for path in watched if path.exists()}
                stop()
                start()
    except KeyboardInterrupt:
        return 0
    finally:
        stop()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="uh", description="UH! Ultra Harness CLI")
    sub = parser.add_subparsers(dest="cmd")
    dev = sub.add_parser("dev", help="本地开发")
    devsub = dev.add_subparsers(dest="action")
    up = devsub.add_parser("up"); up.add_argument("--ui", choices=["web", "desktop", "both"]); up.add_argument("--port", type=int); up.add_argument("--host", default="127.0.0.1"); up.add_argument("--no-browser", action="store_true")
    devsub.add_parser("down")
    lg = devsub.add_parser("logs"); lg.add_argument("-f", "--follow", action="store_true")
    rs = devsub.add_parser("reset"); rs.add_argument("--keep-vectors", action="store_true")
    wa = devsub.add_parser("watch"); wa.add_argument("--port", type=int); wa.add_argument("--host", default="127.0.0.1")
    memory = sub.add_parser("memory", help="长期记忆")
    memorysub = memory.add_subparsers(dest="action")
    purge = memorysub.add_parser("purge", help="清理长期记忆")
    purge.add_argument("--before", help="清理此日期/时间之前的记忆")
    export = memorysub.add_parser("export", help="导出长期记忆")
    confirm = sub.add_parser("confirm", help="高危工具确认（M1-8）")
    confirm.add_argument("--task", required=True, help="任务 ID")
    confirm.add_argument("--token", required=True, help="confirmation_token")
    cg = confirm.add_mutually_exclusive_group(required=True)
    cg.add_argument("--approve", action="store_true", help="批准执行")
    cg.add_argument("--reject", action="store_true", help="拒绝执行")
    confirm.add_argument("--operator", default="cli-user", help="确认人")
    confirm.add_argument("--port", type=int)
    confirm.add_argument("--host", default="127.0.0.1")
    plugin = sub.add_parser("plugin", help="插件管理")
    pluginsub = plugin.add_subparsers(dest="action")
    validate = pluginsub.add_parser("validate", help="校验插件 manifest、包体和签名")
    validate.add_argument("source")
    validate.add_argument("--no-verify", action="store_true")
    install = pluginsub.add_parser("install", help="安装 .uhpkg 插件")
    install.add_argument("source")
    install.add_argument("--no-verify", action="store_true")
    uninstall = pluginsub.add_parser("uninstall", help="卸载插件")
    uninstall.add_argument("name")
    pluginsub.add_parser("list", help="列出已安装插件")
    pluginsub.add_parser("audit", help="输出插件权限矩阵")
    dev_plugin = pluginsub.add_parser("dev", help="开发目录热重载")
    dev_plugin.add_argument("source")
    dev_plugin.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if args.cmd == "confirm":
        return confirm_task(args)
    if args.cmd == "memory":
        if not getattr(args, "action", None):
            parser.print_help(); return 0
        return {"purge": memory_purge, "export": memory_export}[args.action](args)
    if args.cmd == "plugin":
        if not getattr(args, "action", None):
            plugin.print_help(); return 0
        return {
            "validate": plugin_validate,
            "install": plugin_install,
            "uninstall": plugin_uninstall,
            "list": plugin_list,
            "audit": plugin_audit,
            "dev": plugin_dev,
        }[args.action](args)
    if args.cmd != "dev" or not getattr(args, "action", None):
        parser.print_help(); return 0
    return {"up": dev_up, "down": dev_down, "logs": dev_logs, "reset": dev_reset, "watch": dev_watch}[args.action](args)


if __name__ == "__main__":
    sys.exit(main())
