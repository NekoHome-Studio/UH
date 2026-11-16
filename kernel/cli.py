"""UH! CLI（M0-6，§7.2 / §11.4）：uh dev up/down/logs/reset/watch。

一键本地启动（本地优先）。内核服务永远在；--ui 决定 UI 载体（web/desktop/both）。
Desktop 壳（Tauri）M3-6 实装，M0 下 desktop/both 优雅降级为内核+Web 并提示。
配置经 uh-dev.yaml；状态存 .uh/（pid/log）。跨平台进程管理。
"""
from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

import yaml

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
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def _wait_health(port: int, timeout: float = 45.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=1) as r:
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
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def dev_down(args) -> int:
    pid = _read_pid()
    if pid:
        _terminate(pid)
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
    args = parser.parse_args(argv)
    if args.cmd != "dev" or not getattr(args, "action", None):
        parser.print_help(); return 0
    return {"up": dev_up, "down": dev_down, "logs": dev_logs, "reset": dev_reset, "watch": dev_watch}[args.action](args)


if __name__ == "__main__":
    sys.exit(main())
