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
    stopping = False

    def spawn() -> int:
        pid = os.fork()
        if pid == 0:
            try:
                uvicorn.Server(config).run(sockets=[sock])
            except BaseException:  # noqa: BLE001 - 子进程必须硬退出，不留残骸
                os._exit(1)
            os._exit(0)
        return pid

    for _ in range(count):
        children.append(spawn())

    # 2026-11-28 审计：原实现 waitpid 一次就返回——任一 worker 异常退出后
    # 父进程把所有子进程丢下自己走掉（孤儿化），既不替换也不收敛。
    # 改为监督循环：回收到的子进程若属非正常退出，立刻补一个，保持 count 个。
    try:
        while not stopping:
            try:
                pid, status = os.wait()
            except ChildProcessError:
                break
            if pid not in children:
                continue
            children.remove(pid)
            exit_code = os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else status
            print(f"worker pid={pid} exited code={exit_code}，重新拉起", file=sys.stderr)
            children.append(spawn())
    except KeyboardInterrupt:
        stopping = True
        for pid in list(children):
            try:
                os.kill(pid, 15)
            except ProcessLookupError:
                pass
    finally:
        for pid in list(children):
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
