"""多进程启动器（M3-9）。

为什么不用 uvicorn.run(workers=N)：实测（uvicorn 0.54 / Linux）设 UH_WORKERS=4
后 ps -ef | grep kernel.serve 仍只有 1 个进程——uvicorn 的 workers 参数经
Multiprocess 分支未能真正拉起子进程，导致 M3-9「4 worker + Redis」的压测
一直是单 worker 在跑（p95 长期停留在 6.7~7.4s，且提高 UH_TASK_CONCURRENCY
反而更慢，因为单进程内 GIL 争用）。

这里改为显式 fork：父进程绑定一次监听 socket，子进程继承 fd 各自 serve，
N 个 worker 真正并行。Windows 无 os.fork 且 spawn 无法共享 socket，
因此 Windows 明确退化为单进程（开发用），生产多 worker 走 Linux。
"""
import os
import sys

import uvicorn


def _config() -> uvicorn.Config:
    return uvicorn.Config(
        "kernel.main:app",
        host=os.environ.get("UH_HOST", "127.0.0.1"),
        port=int(os.environ.get("UH_PORT", "8080")),
        access_log=False,
        log_level=os.environ.get("UH_LOG_LEVEL", "warning"),
    )


def _fork_workers(config: uvicorn.Config, count: int) -> int:
    """fork count 个 worker 共享同一监听 socket。返回退出码。"""
    sock = config.bind_socket()
    children: list[int] = []
    for _ in range(count):
        pid = os.fork()
        if pid == 0:
            # 子进程：继承已绑定的 socket，直接 serve
            try:
                uvicorn.Server(config).run(sockets=[sock])
            except BaseException:  # noqa: BLE001 - 子进程必须硬退出，不留残骸
                os._exit(1)
            os._exit(0)
        children.append(pid)
    try:
        for pid in children:
            os.waitpid(pid, 0)
    except KeyboardInterrupt:
        for pid in children:
            try:
                os.kill(pid, 15)
            except ProcessLookupError:
                pass
    finally:
        for pid in children:
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
    return 0


def main() -> int:
    workers = int(os.environ.get("UH_WORKERS", "1"))
    config = _config()
    if workers <= 1:
        uvicorn.Server(config).run()
        return 0
    if not hasattr(os, "fork"):
        # Windows：spawn 无法共享监听 socket，多 worker 不成立。
        print("UH_WORKERS>1 需要 fork（Linux/macOS）；Windows 退化为单进程。", file=sys.stderr)
        uvicorn.Server(config).run()
        return 0
    return _fork_workers(config, workers)


if __name__ == "__main__":
    sys.exit(main())
