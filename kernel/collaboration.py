"""Task-scoped CrewAI lifecycle and its internal capability transport."""
from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager

import httpx
from redis.exceptions import RedisError

from . import tracing
from .logging import get_logger, log_event
from .protocol import Envelope, EventBus, module_error_code
from .scaler import CrewAIScaler, ScaleResult

_TARGET = "uh-crewai"
_STARTUP_SECONDS = 30
_WORKER_SECONDS = 120
COLLABORATION_DEADLINE_MS = (_STARTUP_SECONDS + _WORKER_SECONDS + 5) * 1000
_LEASE_SECONDS = 180  # Longer than the entire bounded capability call.
_LOCK = "uh:collaboration:transition"
_RELEASE_SECONDS = 40.0
_RELEASE_ATTEMPTS = 3
# 锁 TTL 须大于单次释放上限(40s)+重试退避，否则过期后他人可并发缩容（双重删除）
_LOCK_TTL_SECONDS = 90
_ACTIVE = "uh:collaboration:active"
_STATE = "uh:collaboration:worker"
_LOG = get_logger(__name__)


class CrewAIWorker:
    def __init__(self, url: str) -> None:
        self.url = url.rstrip("/")

    async def wait_ready(self) -> None:
        """Poll the worker readiness endpoint through its service address."""
        async with httpx.AsyncClient(timeout=1, trust_env=False) as client:
            while True:
                try:
                    response = await client.get(f"{self.url}/ready")
                    if response.status_code == 200:
                        return
                except httpx.RequestError:
                    pass
                await asyncio.sleep(0.5)

    async def run(self, req: Envelope) -> str:
        # This is the transport behind the registered capability. Only task input
        # and correlation fields cross the process boundary.
        async with httpx.AsyncClient(timeout=_WORKER_SECONDS, trust_env=False) as client:
            response = await client.post(f"{self.url}/rpc/collaboration.run", json=req.to_dict())
            if response.status_code == 504:
                raise TimeoutError("CrewAI worker deadline exceeded")
            response.raise_for_status()
            answer = response.json()["answer"]
            if not isinstance(answer, str) or not answer.strip():
                raise ValueError("empty collaboration answer")
            return answer


class CollaborationManager:
    def __init__(
        self, *, scaler: CrewAIScaler | None, bus: EventBus, redis, worker: CrewAIWorker,
        startup_timeout_s: float = _STARTUP_SECONDS,
    ) -> None:
        if not 0 < startup_timeout_s <= _STARTUP_SECONDS:
            raise ValueError("collaboration startup timeout must be within 30 seconds")
        self.scaler = scaler  # Compose operators own container start/stop.
        self.bus = bus
        self.redis = redis
        self.worker = worker
        self.startup_timeout_s = startup_timeout_s
        self._running: set[asyncio.Task] = set()
        self._cleanup: set[asyncio.Task] = set()
        self._reapers: dict[asyncio.AbstractEventLoop, asyncio.Task] = {}

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        if self.scaler is not None and loop not in self._reapers:
            self._reapers[loop] = loop.create_task(self._reconcile_idle())

    async def close(self) -> None:
        loop = asyncio.get_running_loop()
        tasks = [task for task in tuple(self._running) if task.get_loop() is loop]
        reaper = self._reapers.pop(loop, None)
        if reaper is not None:
            tasks.append(reaper)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        while cleanup := [task for task in tuple(self._cleanup) if task.get_loop() is loop]:
            await asyncio.gather(*cleanup, return_exceptions=True)

    def _background(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._cleanup.add(task)
        task.add_done_callback(self._cleanup.discard)
        return task

    async def run(self, req: Envelope) -> Envelope:
        task = asyncio.current_task()
        self._running.add(task)
        lease = secrets.token_hex(16)
        try:
            try:
                async with asyncio.timeout(self.startup_timeout_s):
                    if self.scaler is None:
                        await self.worker.wait_ready()
                        result, scaled = ScaleResult("ok"), False
                    else:
                        result, scaled = await self._acquire(req, lease)
            except TimeoutError:
                result, scaled = ScaleResult("degraded", "timeout"), False
            except RedisError:
                result, scaled = ScaleResult("degraded", "coordination_unavailable"), False

            if result.status != "ok":
                return await self._degraded(req, result.reason, phase="startup")
            if scaled:
                await self._event("scaled_up", req.trace_id, req.payload.get("task_id"), replicas=1)
            await self._event("ready", req.trace_id, req.payload.get("task_id"), replicas=1)

            try:
                async with asyncio.timeout(_WORKER_SECONDS):
                    answer = await self.worker.run(req)
            except (TimeoutError, httpx.TimeoutException):
                return await self._degraded(req, "timeout", phase="execution")
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                return await self._degraded(req, "worker_unavailable", phase="execution")
            return self._reply(req, {"answer": answer})
        finally:
            if self.scaler is not None:
                # Cancellation or a timed-out PATCH can leave a pending start.
                # Resolve it independently of the task and its fallback latency.
                self._background(self._release(lease, req.trace_id, req.payload.get("task_id")))
            self._running.discard(task)

    @staticmethod
    def _reply(req: Envelope, payload: dict) -> Envelope:
        return Envelope(action=req.action, payload=payload, trace_id=req.trace_id, from_=req.to, to=req.from_)

    async def _degraded(self, req: Envelope, reason: str, *, phase: str) -> Envelope:
        action = "timeout" if reason == "timeout" else "degraded"
        await self._event(action, req.trace_id, req.payload.get("task_id"), reason=reason, phase=phase)
        return self._reply(req, {
            "degraded": True, "reason": reason,
            "error_code": "UH-CAP-5021" if reason == "timeout" else module_error_code(),
        })

    async def _event(self, action: str, trace_id: str, task_id: str | None, **fields) -> None:
        payload = dict(fields, target=_TARGET, task_id=task_id)
        if "reason" in fields:
            with tracing.request_context(trace_id):
                log_event(_LOG, logging.WARNING, "audit.collaboration.result", **payload)
        await self.bus.publish(f"collaboration.worker.{action}", payload, trace_id=trace_id)

    @asynccontextmanager
    async def _transition(self):
        # Only transitions hold the distributed lock. Worker executions overlap.
        # Its expiry exceeds either bounded scale call plus Redis bookkeeping.
        owner = secrets.token_hex(16)
        try:
            while not await self.redis.set(_LOCK, owner, nx=True, ex=_LOCK_TTL_SECONDS):
                await asyncio.sleep(0.05)
            yield
        finally:
            release = self._background(self._unlock(owner))
            if not asyncio.current_task().cancelling():
                await asyncio.shield(release)

    async def _unlock(self, owner: str) -> None:
        try:
            async with asyncio.timeout(2), self.redis.pipeline(transaction=True) as pipe:
                await pipe.watch(_LOCK)
                if await pipe.get(_LOCK) == owner:
                    pipe.multi()
                    pipe.delete(_LOCK)
                    await pipe.execute()
        except (RedisError, TimeoutError):
            # A competing owner can acquire after expiry. WATCH keeps its lock intact.
            pass

    async def _prune_leases(self) -> int:
        now, _ = await self.redis.time()
        await self.redis.zremrangebyscore(_ACTIVE, "-inf", now)
        return now

    async def _acquire(self, req: Envelope, lease: str) -> tuple[ScaleResult, bool]:
        async with self._transition():
            now = await self._prune_leases()
            active = await self.redis.zcard(_ACTIVE)
            state = await self.redis.hgetall(_STATE)
            await self.redis.zadd(_ACTIVE, {lease: now + _LEASE_SECONDS})
            if active and state.get("ready") == "1":
                return ScaleResult("ok"), False

            # Persist the scale intent before PATCH. If the process dies after
            # sending it, any kernel replica can reconcile the worker later.
            await self.redis.hset(_STATE, mapping={
                "ready": "0", "trace_id": req.trace_id, "task_id": req.payload.get("task_id") or "",
            })
            result = await self.scaler.scale(_TARGET, 1, trace_id=req.trace_id)
            if result.status == "ok":
                # The Service can lag readyReplicas while endpoints propagate.
                await self.worker.wait_ready()
                await self.redis.hset(_STATE, "ready", "1")
            elif not state and (result.status == "rejected" or result.reason == "cluster_unavailable"):
                await self.redis.delete(_STATE)  # No PATCH could have been applied.
            return result, result.status == "ok"

    async def _release(self, lease: str | None, trace_id: str = "", task_id: str | None = None) -> None:
        # 二轮复检阻断④：scale(0) 失败须重试，否则副本永久停在 1；
        # 锁 TTL 提到 90s（> 单次释放 40s 上限 + 重试窗口），消除双重删除竞态。
        last: ScaleResult | None = None
        try:
            async with asyncio.timeout(_RELEASE_SECONDS), self._transition():
                if lease is not None:
                    await self.redis.zrem(_ACTIVE, lease)
                await self._prune_leases()
                if await self.redis.zcard(_ACTIVE):
                    return
                state = await self.redis.hgetall(_STATE)
                if not state:
                    return
                trace_id = trace_id or state["trace_id"]
                task_id = task_id or state["task_id"] or None
                await self.redis.hset(_STATE, "ready", "0")
                for attempt in range(_RELEASE_ATTEMPTS):
                    last = await self.scaler.scale(_TARGET, 0, trace_id=trace_id)
                    if last.status == "ok":
                        # 只有确认缩容成功才清状态；否则保留供 reconciler 下轮重试
                        await self.redis.delete(_STATE)
                        break
                    if attempt + 1 < _RELEASE_ATTEMPTS:
                        await asyncio.sleep(0.5 * (attempt + 1))
        except (TimeoutError, RedisError) as exc:
            reason = "timeout" if isinstance(exc, TimeoutError) else "coordination_unavailable"
            await self._event("degraded", trace_id, task_id, reason=reason, phase="shutdown")
            return
        if last is not None and last.status == "ok":
            await self._event("scaled_down", trace_id, task_id, replicas=0)
        else:
            # 重试耗尽仍失败：保留 _STATE，reconciler 每 10s 会再试
            await self._event("degraded", trace_id, task_id,
                              reason=(last.reason if last is not None else "no_attempt"), phase="shutdown")

    async def _reconcile_idle(self) -> None:
        while True:
            await self._release(None)
            await asyncio.sleep(10)
