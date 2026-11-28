"""M2-5 Hook 插件运行时。

Hook 订阅使用独立队列和 worker。事件总线只负责把事件入队，Hook 的慢处理、
异常和超时都不会阻塞请求主链路。"""
from __future__ import annotations

import asyncio
import fnmatch
import time
import inspect
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Awaitable, Callable

from .event_mapping import INTERNAL_TO_EXTERNAL
from .logging import get_logger, log_event
from .plugins import PluginManifest, PluginValidationError
from .protocol import Envelope, EventBus, validate_event_name
from .sandbox import SandboxRunner

log = get_logger("kernel.hooks")
HookHandler = Callable[[Envelope], Awaitable[None]]
_ALIASES = {
    "capability.invoked": "capability.module.invoked",
    "capability.finished": "capability.module.finished",
    "capability.failed": "capability.module.failed",
    "memory.write": "memory.record.write",
    "llm.delta": "llm.stream.delta",
    "tool.confirm": "tool.confirmation.requested",
    "tool.confirm.resolved": "tool.confirmation.resolved",
    "safety.check_started": "safety.check.started",
    "safety.check_finished": "safety.check.finished",
    "safety.denied": "safety.action.denied",
    "cua.yield": "cua.session.yielded",
}
_METADATA_FIELDS = frozenset({
    "task_id", "session_id", "task_type", "mode", "rule", "confidence", "fallback",
    "step_id", "module", "status", "duration_ms", "checkpoint_id", "skipped_steps",
    "resumed_from_event_id", "completed_steps", "call_id", "tool_name", "risk_level",
    "requires_confirmation", "decision", "resolved_by", "truncated", "index",
    "reason_code", "check_id", "check_type", "target", "result", "action_taken",
    "related_error_code", "role",
})


@dataclass
class _Hook:
    enqueue: Callable[[Envelope], Awaitable[None]]
    task: asyncio.Task[None]


class HookManager:
    def __init__(self, bus: EventBus, *, queue_size: int = 256):
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        self.bus = bus
        self.queue_size = queue_size
        self._hooks: dict[str, _Hook] = {}

    def register(self, name: str, events: list[str] | tuple[str, ...], handler: HookHandler, *, timeout_ms: int = 5000, rate_limit: int | None = None, drop_policy: str = "drop_new") -> None:
        if name in self._hooks:
            raise ValueError(f"hook already registered: {name}")
        if timeout_ms < 1:
            raise ValueError("timeout_ms must be positive")
        if not inspect.iscoroutinefunction(handler):
            raise TypeError("hook handlers must be async")
        if not events:
            raise ValueError("hook requires at least one event")
        for event in events:
            if "*" not in event:
                validate_event_name(event)
        # 2026-11-28 审计：契约 manifest/hook.schema.json 声明了 rate_limit，
        # 但运行时从未使用——配置被静默忽略。这里落实为「每窗口最大投递数」。
        if rate_limit is not None and (not isinstance(rate_limit, int) or rate_limit < 1):
            raise ValueError('rate_limit must be a positive integer')
        if drop_policy not in {'block', 'drop_oldest', 'drop_new'}:
            raise ValueError('drop_policy must be block/drop_oldest/drop_new')
        _window_s = 1.0
        _budget = rate_limit
        _sent = [0]
        _window_start = [time.monotonic()]
        patterns = tuple(events)
        queue: asyncio.Queue[Envelope] = asyncio.Queue(maxsize=self.queue_size)

        async def enqueue(envelope: Envelope) -> None:
            hook = self._hooks.get(name)
            if hook is None or hook.enqueue is not enqueue:
                return
            if not any(fnmatch.fnmatchcase(envelope.action, pattern) for pattern in patterns):
                return
            payload = {
                key: value for key, value in envelope.payload.items()
                if key in _METADATA_FIELDS and isinstance(value, (str, int, float, bool))
                and (key != "result" or envelope.action == "safety.check.finished")
            }
            error = envelope.payload.get("error")
            if isinstance(error, dict) and isinstance(error.get("code"), str):
                payload["error_code"] = error["code"]
            # 速率限制：契约声明的 rate_limit 在此生效（按秒窗口计）。
            if _budget is not None:
                now = time.monotonic()
                if now - _window_start[0] >= _window_s:
                    _window_start[0] = now
                    _sent[0] = 0
                if _sent[0] >= _budget:
                    log_event(log, 30, "hook.rate_limited", hook=name,
                              action=envelope.action, rate_limit=_budget)
                    return
                _sent[0] += 1
            item = replace(envelope, payload=payload)
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                # 2026-11-28 审计：原实现队列满时**直接丢弃**且只写一行日志——
                # 事件静默丢失，订阅方无从感知。按 drop_policy 处理，并计数。
                if drop_policy == "drop_oldest":
                    try:
                        queue.get_nowait()
                        queue.put_nowait(item)
                    except (asyncio.QueueEmpty, asyncio.QueueFull):
                        pass
                    log_event(log, 30, "hook.queue.dropped_oldest", hook=name,
                              action=envelope.action)
                elif drop_policy == "block":
                    # 仅限显式选择：会阻塞事件总线，需调用方自行承担
                    await queue.put(item)
                    log_event(log, 30, "hook.queue.blocked", hook=name,
                              action=envelope.action)
                else:
                    log_event(log, 30, "hook.queue.dropped", hook=name,
                              action=envelope.action, policy=drop_policy)

        task = asyncio.create_task(self._worker(name, queue, handler, timeout_ms))
        self._hooks[name] = _Hook(enqueue, task)
        self.bus.subscribe("*", enqueue)

    async def _worker(self, name: str, queue: asyncio.Queue[Envelope], handler: HookHandler, timeout_ms: int) -> None:
        while True:
            envelope = await queue.get()
            try:
                await asyncio.wait_for(handler(envelope), timeout=timeout_ms / 1000.0)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                log_event(log, 30, "hook.timeout", hook=name, action=envelope.action, timeout_ms=timeout_ms)
            except Exception as exc:
                log_event(log, 30, "hook.failed", hook=name, action=envelope.action, error_type=type(exc).__name__)
            finally:
                queue.task_done()

    def unregister(self, name: str) -> asyncio.Task[None] | None:
        hook = self._hooks.pop(name, None)
        if hook is not None:
            self.bus.unsubscribe("*", hook.enqueue)
            hook.task.cancel()
            return hook.task
        return None

    async def shutdown(self) -> None:
        tasks = [self.unregister(name) for name in self.names]
        await asyncio.gather(*(task for task in tasks if task is not None), return_exceptions=True)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._hooks))


class HookPluginLoader:
    """Deliver one metadata envelope on stdin per sandboxed invocation."""

    def __init__(self, manager: HookManager, runner: SandboxRunner) -> None:
        self.manager = manager
        self.runner = runner

    async def load(self, manifest: PluginManifest, package_dir: Path) -> None:
        for event in manifest.config["events"]:
            canonical = _ALIASES.get(event, event)
            if canonical not in INTERNAL_TO_EXTERNAL:
                raise PluginValidationError("钩子订阅了未登记事件", detail={"field": "config.events", "reason": event})
        self.runner.build_plan(manifest, package_dir)

    async def activate(self, manifest: PluginManifest, package_dir: Path) -> None:
        events = [_ALIASES.get(event, event) for event in manifest.config["events"]]

        async def deliver(envelope: Envelope) -> None:
            data = (json.dumps(envelope.to_dict(), ensure_ascii=False) + "\n").encode("utf-8")
            status, _, _ = await self.runner.run(manifest, package_dir, data, capture_output=False)
            if status != 0:
                raise RuntimeError("hook process failed")

        self.manager.register(manifest.name, events, deliver, timeout_ms=manifest.config["timeout_ms"])

    async def deactivate(self, manifest: PluginManifest, package_dir: Path) -> None:
        task = self.manager.unregister(manifest.name)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def uninstall(self, manifest: PluginManifest, package_dir: Path) -> None:
        await self.deactivate(manifest, package_dir)

    def force_kill(self, manifest: PluginManifest, package_dir: Path) -> None:
        self.manager.unregister(manifest.name)

