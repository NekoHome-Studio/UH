"""M2-5 Hook 插件运行时。

Hook 订阅使用独立队列和 worker。事件总线只负责把事件入队，Hook 的慢处理、
异常和超时都不会阻塞请求主链路。"""
from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .logging import get_logger, log_event
from .protocol import Envelope, EventBus, validate_event_name

log = get_logger("kernel.hooks")
HookHandler = Callable[[Envelope], Any]


@dataclass
class _Hook:
    name: str
    patterns: tuple[str, ...]
    handler: HookHandler
    timeout_ms: int
    queue: asyncio.Queue[Envelope]
    task: asyncio.Task


class HookManager:
    def __init__(self, bus: EventBus, *, queue_size: int = 256):
        self.bus = bus
        self.queue_size = queue_size
        self._hooks: dict[str, _Hook] = {}

    def register(self, name: str, events: list[str] | tuple[str, ...], handler: HookHandler, *, timeout_ms: int = 5000) -> None:
        if name in self._hooks:
            raise ValueError(f"hook already registered: {name}")
        if timeout_ms < 1:
            raise ValueError("timeout_ms must be positive")
        patterns = tuple(events)
        for event in patterns:
            if "*" not in event:
                validate_event_name(event)
        queue: asyncio.Queue[Envelope] = asyncio.Queue(maxsize=self.queue_size)

        async def enqueue(envelope: Envelope) -> None:
            try:
                queue.put_nowait(envelope)
            except asyncio.QueueFull:
                log_event(log, 30, "hook.queue.dropped", hook=name, event=envelope.action)

        for pattern in patterns:
            self.bus.subscribe(pattern, enqueue)
        task = asyncio.create_task(self._worker(name, queue, handler, timeout_ms))
        self._hooks[name] = _Hook(name, patterns, handler, timeout_ms, queue, task)

    async def _worker(self, name: str, queue: asyncio.Queue[Envelope], handler: HookHandler, timeout_ms: int) -> None:
        while True:
            envelope = await queue.get()
            try:
                result = handler(envelope)
                if inspect.isawaitable(result):
                    await asyncio.wait_for(result, timeout=timeout_ms / 1000.0)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                log_event(log, 30, "hook.timeout", hook=name, event=envelope.action, timeout_ms=timeout_ms)
            except Exception as exc:
                log_event(log, 30, "hook.failed", hook=name, event=envelope.action, error=str(exc))
            finally:
                queue.task_done()

    def unregister(self, name: str) -> None:
        hook = self._hooks.pop(name, None)
        if hook is not None:
            hook.task.cancel()

    async def shutdown(self) -> None:
        hooks = list(self._hooks.values())
        for hook in hooks:
            self.unregister(hook.name)
        if hooks:
            await asyncio.gather(*(hook.task for hook in hooks), return_exceptions=True)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._hooks))


__all__ = ["HookManager"]

