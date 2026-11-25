"""任务执行准入控制（M3-9 提交/执行解耦）。

问题：create() 立即 asyncio.create_task(_run(...))，100 并发下 100 个长任务
与请求提交共享同一事件循环，提交虽只需 6ms 却要等调度（实测 P95 8.9s）。

方案：有界并发闸（Semaphore）+ 排队。提交路径只做入队与状态登记即返回；
执行协程在闸外等待，不占用循环的 CPU 时间片。超出上限的任务排队而非
同时抢占，使少量在跑任务快速完成、提交请求及时得到调度。

这不改变外部契约：task_id 立即返回、事件流与终态语义不变。
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Awaitable, Callable


def execution_limit() -> int:
    """同时在跑的任务上限。默认按 CPU 数放大，可 UH_TASK_CONCURRENCY 覆盖。"""
    raw = os.environ.get("UH_TASK_CONCURRENCY")
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return max(4, (os.cpu_count() or 4) * 2)


class ExecutionGate:
    """有界执行闸：限制同时运行的任务数，其余排队。"""

    def __init__(self, limit: int | None = None) -> None:
        self.limit = limit if limit is not None else execution_limit()
        self._sem = asyncio.Semaphore(self.limit)
        self.running = 0
        self.queued = 0
        self.completed = 0

    async def run(self, coro_factory: Callable[[], Awaitable[Any]]) -> Any:
        """在闸内执行；排队期间不占用 CPU（Semaphore 等待是纯挂起）。

        M3-9：入口先 sleep(0) 让出一次循环，使 create() 的调用方（HTTP handler）
        能先把 202 响应写完再开始执行。否则 create_task 会立刻调度执行协程，
        在响应发出前抢占循环，把提交延迟拖成任务延迟。
        """
        await asyncio.sleep(0)
        self.queued += 1
        try:
            await self._sem.acquire()
        finally:
            self.queued -= 1
        self.running += 1
        try:
            return await coro_factory()
        finally:
            self.running -= 1
            self.completed += 1
            self._sem.release()

    def stats(self) -> dict[str, int]:
        return {"limit": self.limit, "running": self.running,
                "queued": self.queued, "completed": self.completed}
