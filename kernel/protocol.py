"""内部协议（M1-1，§6.6）：统一信封 + 事件总线 + 内部 RPC + 降级 + 存储隔离。

M1 进程内实现，传输无关（后续可换 gRPC/异步消息）。
- 统一信封：envelope_version/trace_id/span_id/parent_span_id/from/to/action/payload/deadline_ms/ts/event_id(bus_seq)
- 事件总线：发布-订阅，内部事件命名三段「域.对象.动作」（ADR-002），bus_seq 单调
- 内部 RPC：请求-响应，deadline 超时，模块不可用/超容 → degraded 降级
- 存储隔离：模块只能访问自身命名空间存储（静态检查）
"""
from __future__ import annotations

import asyncio
import fnmatch
import re
import secrets
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

# ADR-002：内部事件三段「域.对象.动作」
EVENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")


def validate_event_name(name: str) -> None:
    if not EVENT_NAME_RE.match(name):
        raise ValueError("内部事件须三段「域.对象.动作」(ADR-002): " + name)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class Envelope:
    action: str
    payload: dict = field(default_factory=dict)
    trace_id: str = ""
    span_id: str = ""
    parent_span_id: str | None = None
    from_: str = "kernel"
    to: str = "bus"
    envelope_version: str = "1"
    deadline_ms: int = 8000
    ts: str = field(default_factory=_now)
    event_id: int = 0  # bus_seq

    def to_dict(self) -> dict:
        d = asdict(self)
        d["from"] = d.pop("from_")
        return d


class EventBus:
    """发布-订阅总线。订阅者异常隔离（钩子不拖垮主链路）。"""

    def __init__(self) -> None:
        self._subs: list[tuple[str, Callable[[Envelope], Awaitable[None]]]] = []
        self._seq = 0

    def subscribe(self, pattern: str, handler: Callable[[Envelope], Awaitable[None]]) -> None:
        self._subs.append((pattern, handler))

    async def publish(self, name: str, payload: dict, trace_id: str = "", span_id: str = "", from_: str = "kernel", parent_span_id: str | None = None) -> Envelope:
        validate_event_name(name)
        self._seq += 1
        env = Envelope(action=name, payload=payload, trace_id=trace_id, span_id=span_id or ("sp_" + secrets.token_hex(4)), from_=from_, to="bus", parent_span_id=parent_span_id, event_id=self._seq)  # 四审：自动生成 span 供溯源
        for pattern, handler in self._subs:
            if fnmatch.fnmatchcase(name, pattern):
                try:
                    await handler(env)
                except Exception:
                    pass  # 订阅者异常隔离
        return env

    @property
    def bus_seq(self) -> int:
        return self._seq


def module_error_code(mode: str | None = None) -> str:
    """ADR-003：能力模块错误码双模式。strict（默认）→ UH-CAP-4004（语义正确）；
    legacy → UH-CAP-4003（向后兼容旧插件）。LLM 上游错误恒为 CAP-4003，不走此助手。
    mode 可由调用方/插件上下文覆盖（二审 R11：context.error_mode 通道）。"""
    import os
    mode = (mode or os.environ.get("UH_CAP_ERROR_MODE", "strict")).lower()
    return "UH-CAP-4003" if mode == "legacy" else "UH-CAP-4004"


class StorageGuard:
    """存储隔离：模块仅可访问自身命名空间（§6.6 禁止直连他模块存储）。"""

    def __init__(self) -> None:
        self._owned: dict[str, set[str]] = {}

    def declare(self, module: str, namespaces: set[str]) -> None:
        # 二审 N7 真实隔离：命名空间不可被多模块重复声明（防越权共享存储）。
        for ns in namespaces:
            for other, owned in self._owned.items():
                if other != module and ns in owned:
                    raise PermissionError("命名空间 %s 已被模块 %s 声明，%s 不可重复声明（UH-KRN-2010）" % (ns, other, module))
        self._owned[module] = set(namespaces)

    def check(self, module: str, namespace: str) -> None:
        if namespace not in self._owned.get(module, set()):
            raise PermissionError("模块 %s 无权访问存储 %s（UH-KRN-2010）" % (module, namespace))

    def access(self, module: str, namespace: str) -> None:
        """运行时存储访问校验（二审 N7）：存储层访问前调用，校验真实目标命名空间归属。"""
        self.check(module, namespace)

    def authorize(self, module: str) -> None:
        """RPC 调度前校验：模块声明的命名空间归属一致（无冲突）。"""
        for ns in self._owned.get(module, set()):
            self.check(module, ns)


class CapabilityRegistry:
    """内部 RPC：能力模块注册 handler，内核按 action 调用，超时/不可用降级。"""

    def __init__(self, bus: EventBus, guard: StorageGuard) -> None:
        self.bus = bus
        self.guard = guard
        self._handlers: dict[str, Callable[[Envelope], Awaitable[Envelope]]] = {}
        self._available: dict[str, bool] = {}

    def register(self, name: str, handler: Callable[[Envelope], Awaitable[Envelope]], storage: set[str] | None = None) -> None:
        self._handlers[name] = handler
        self._available[name] = True
        if storage:
            self.guard.declare(name, storage)

    def unregister(self, name: str) -> None:
        self._handlers.pop(name, None)
        self._available.pop(name, None)

    def set_available(self, name: str, available: bool) -> None:
        if name in self._handlers:
            self._available[name] = available

    def is_available(self, name: str) -> bool:
        return self._available.get(name, False)

    def _degraded(self, req: Envelope, reason: str, code: str = "UH-CAP-5021") -> Envelope:
        return Envelope(action=req.action, payload={"degraded": True, "reason": reason, "error_code": code}, trace_id=req.trace_id, span_id=req.span_id, from_=req.to, to=req.from_, parent_span_id=req.span_id)

    async def call(self, action: str, payload: dict, trace_id: str = "", deadline_ms: int = 8000, from_: str = "kernel") -> Envelope:
        req = Envelope(action=action, payload=payload, trace_id=trace_id, from_=from_, to=action, deadline_ms=deadline_ms)
        handler = self._handlers.get(action)
        if handler is None or not self._available.get(action, False):
            await self.bus.publish("capability.module.degraded", {"action": action, "reason": "unavailable"}, trace_id=trace_id)
            return self._degraded(req, "module unavailable", code=module_error_code())  # 二审：不可用≠超时(5021)}
        self.guard.authorize(action)  # 存储隔离生产校验点
        try:
            resp = await asyncio.wait_for(handler(req), timeout=deadline_ms / 1000.0)
            return resp
        except asyncio.TimeoutError:
            await self.bus.publish("capability.module.degraded", {"action": action, "reason": "timeout"}, trace_id=trace_id)
            return self._degraded(req, "deadline exceeded")
        except Exception as e:
            await self.bus.publish("capability.module.failed", {"action": action, "error": str(e)}, trace_id=trace_id)
            return self._degraded(req, "module error: " + str(e), code=module_error_code(req.payload.get("error_mode")))
