"""多进程启动器（M3-9）：uvicorn.run(workers=N) 走 multiprocessing spawn/fork。

CLI `-m uvicorn --workers N` 在部分版本不真起子进程；用 Python API 明确指定。
Linux 默认 fork，Windows 需 spawn（且要求 __main__ 保护）。
"""
import os

import uvicorn


def main() -> None:
    workers = int(os.environ.get("UH_WORKERS", "1"))
    host = os.environ.get("UH_HOST", "127.0.0.1")
    port = int(os.environ.get("UH_PORT", "8080"))
    uvicorn.run(
        "kernel.main:app",
        host=host,
        port=port,
        workers=workers if workers > 1 else None,
        access_log=False,
        log_level=os.environ.get("UH_LOG_LEVEL", "warning"),
    )


if __name__ == "__main__":
    main()
