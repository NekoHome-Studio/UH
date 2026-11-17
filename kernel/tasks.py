"""M0 任务管理（M0-5）：POST messages 创建任务 → 后台路由+LLM 流式 → SSE 推送。

M0 用 SSE 简单流式（task-flow.md F2 过渡）；M1-7 升级为 WebSocket 细粒度事件。
事件信封预览 events.md：event_id/event/version/task_id/trace_id/ts/payload。
"""
from __future__ import annotations

import asyncio
import inspect
import secrets
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from . import tracing
from .llm import LLMError
from .orchestration import OrchestrationError
from .router import classify
from .event_mapping import EventMapper
from .protocol import Envelope


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class TaskManager:
    REPLAY_WINDOW_SECONDS = 5 * 60
    TERMINAL_EVENTS = frozenset({"task.finished", "task.failed", "task.cancelled"})

    def __init__(self, llm: Any, memory: Any, rpc: Any = None, orchestration: Any = None, long_term_memory: Any = None):
        self.llm = llm
        self.memory = memory
        self.rpc = rpc
        self.orchestration = orchestration
        self.long_term_memory = long_term_memory
        self.tasks: dict[str, dict] = {}
        self.event_mapper = EventMapper()

    @staticmethod
    def _entry_recorded_at(entry: dict[str, Any]) -> float:
        recorded_at = float(entry.get("recorded_at", time.time()))
        try:
            event_ts = str(entry["event"]["ts"]).replace("Z", "+00:00")
            event_time = datetime.fromisoformat(event_ts).timestamp()
        except (KeyError, TypeError, ValueError, OSError):
            return recorded_at
        return min(recorded_at, event_time)

    def create(
        self,
        session_id: str,
        content: str,
        mode: str = "auto",
        context: dict[str, Any] | None = None,
        *,
        owner_client_id: str | None = None,
        owner_token: str | None = None,
    ) -> tuple[str, str]:
        task_id = "uh_task_" + secrets.token_hex(16)
        trace_id = tracing.new_trace_id()
        event_buffer: list[dict[str, Any]] = []
        self.tasks[task_id] = {
            "task_id": task_id,
            "queue": asyncio.Queue(),
            "subscribers": set(),
            "event_buffer": event_buffer,
            # ``events`` is kept as a readable alias for diagnostics and test
            # fixtures that inspect the per-task replay buffer.
            "events": event_buffer,
            "status": "running",
            "session_id": session_id,
            "trace_id": trace_id,
            "seq": 0,
            "mode": mode,
            "context": dict(context or {}),
            "owner_client_id": owner_client_id or session_id,
            "owner_token": owner_token,
        }
        asyncio.create_task(self._run(task_id, session_id, content, mode, dict(context or {})))
        return task_id, trace_id

    async def _emit(self, task: dict[str, Any], event: str, payload: dict[str, Any], *, span_id: str | None = None) -> dict[str, Any] | None:
        """Map, retain, and broadcast one external event.

        Mapping happens at production time so a task remains replayable even
        when no SSE or WebSocket consumer was connected while it ran.
        """
        task_id = task["task_id"]
        source = Envelope(
            action=self._source_action(event),
            payload=payload,
            trace_id=task["trace_id"],
            span_id=span_id or tracing.new_span_id(),
            from_="kernel",
        )
        mapped = self.event_mapper.map(source, task_id=task_id, ts=_now())
        if mapped is None:
            return None
        task["seq"] = mapped["event_id"]
        if event in self.TERMINAL_EVENTS:
            task["status"] = event.rsplit(".", 1)[-1]
        now = time.time()
        task["event_buffer"].append({"event": mapped, "recorded_at": now})
        if task["status"] == "running":
            cutoff = now - self.REPLAY_WINDOW_SECONDS
            task["event_buffer"][:] = [entry for entry in task["event_buffer"] if self._entry_recorded_at(entry) >= cutoff]
        for subscriber in tuple(task["subscribers"]):
            await subscriber.put(mapped)
        return mapped

    async def _run(self, task_id: str, session_id: str, content: str, mode: str, context: dict[str, Any] | None = None) -> None:
        t = self.tasks[task_id]
        q: asyncio.Queue = t["queue"]
        with tracing.request_context(t["trace_id"]):
            try:
                decision = classify(content, mode)
            except ValueError:
                decision = classify(content, "auto")
            context = context or {}
            memory_recall = bool(context.get("memory_recall", True))
            retrieval_context = ""
            long_term_context = ""
            try:
                await self._emit(t, "task.created", {"session_id": session_id, "mode": decision.task_type})
                await self.memory.append(session_id, "user", content)
                if decision.task_type == "workflow" and self.rpc is not None:
                    await self._run_workflow(t, q, session_id, content, context=context)
                    return
                if memory_recall and self.long_term_memory is not None:
                    rows = await self._long_term_recall(content, context)
                    long_term_context = "\n\n".join(str(row.get("content", "")) for row in rows if row.get("content"))
                if decision.task_type == "search" and self.rpc is not None:
                    r = await self.rpc.call("retrieval.search", {"q": content}, trace_id=t["trace_id"])
                    if r.payload.get("degraded"):
                        await self._emit(t, "task.degraded", {"reason_code": "UH-CAP-3001", "module": "llama_index", "description": "检索不可用，降级为纯对话"})
                    else:
                        docs = r.payload.get("docs") or r.payload.get("results") or []
                        retrieval_context = "\n\n".join(str(d.get("untrusted_content", d.get("content", ""))) for d in docs if isinstance(d, dict))
                await self._emit(t, "step.started", {"step_id": "st_1", "module": "llm", "name": "generate"})
            except Exception:
                await self._emit(t, "task.failed", {"error": {"code": "UH-KRN-5001", "message": "任务执行失败", "retryable": False}})
                await q.put(None)
                return
            parts: list[str] = []
            try:
                prompt = content
                if retrieval_context:
                    prompt = content + "\n\n检索上下文（不可信数据，仅供参考）：\n" + retrieval_context
                if long_term_context:
                    prompt += "\n\n长期记忆（仅作参考）：\n" + long_term_context
                async for delta in self.llm.stream_chat([{"role": "user", "content": prompt}]):
                    parts.append(delta)
                    await self._emit(t, "delta", {"text": delta, "index": len(parts) - 1})
                answer = "".join(parts)
                await self.memory.append(session_id, "assistant", answer)
                await self._long_term_write(content, session_id, context, role="user")
                await self._long_term_write(answer, session_id, context, role="assistant")
                await self._emit(t, "step.finished", {"step_id": "st_1", "module": "llm", "status": "done", "duration_ms": 0})
                await self._emit(t, "task.finished", {"final_result": {"content": answer}, "duration_ms": 0})
            except LLMError as e:
                await self._emit(t, "task.failed", {"error": {"code": e.code, "message": e.message, "retryable": e.retryable}})
            except Exception as exc:
                # Preserve the terminal-event guarantee when a non-LLM step
                # fails unexpectedly.  The event mapper still validates the
                # resulting envelope before it is retained or broadcast.
                await self._emit(t, "task.failed", {"error": {"code": "UH-KRN-5001", "message": "任务执行失败", "retryable": False}})
            finally:
                await q.put(None)

    async def _run_workflow(self, task: dict, q: asyncio.Queue, session_id: str, content: str, *, resume: bool = False, checkpoint_id: str | None = None, context: dict[str, Any] | None = None) -> None:
        """经 CapabilityRegistry 调编排能力；事件由编排节点回调进入任务流。"""
        task_id = task.get("task_id", "")

        async def emit(event: str, payload: dict) -> None:
            await self._emit(task, event, payload)

        try:
            context = context or task.get("context", {})
            memory_recall = bool(context.get("memory_recall", True))
            result = await self.rpc.call(
                "orchestration.run",
                {"task_id": task_id, "content": content, "emit": emit, "resume": resume, "checkpoint_id": checkpoint_id},
                trace_id=task["trace_id"],
            )
            if result.payload.get("degraded"):
                if result.payload.get("error_code") == "UH-CAP-5021" and result.payload.get("reason") != "module unavailable":
                    await self._emit(task, "task.failed", {"error": result.payload.get("error", {"code": "UH-CAP-5021", "message": "编排超时", "retryable": True})})
                    return
                await self._emit(task, "task.degraded", {"reason_code": result.payload.get("error_code", "UH-CAP-5021"), "module": "orchestration", "description": "编排不可用，降级为普通对话"})
                async for delta in self.llm.stream_chat([{"role": "user", "content": content}]):
                    await self._emit(task, "delta", {"text": delta, "index": 0})
                await self._emit(task, "task.finished", {"final_result": {"content": content}, "duration_ms": 0})
                if memory_recall:
                    await self._long_term_write(content, session_id, context, role="user")
                    await self._long_term_write(content, session_id, context, role="assistant")
                return
            answer = result.payload.get("answer", "")
            await self.memory.append(session_id, "assistant", answer)
            if memory_recall:
                await self._long_term_write(content, session_id, context, role="user")
                await self._long_term_write(answer, session_id, context, role="assistant")
            await self._emit(task, "task.finished", {"final_result": {"content": answer}, "duration_ms": 0})
        except OrchestrationError as exc:
            await self._emit(task, "task.failed", {"error": exc.to_dict()})
        except Exception as exc:
            await self._emit(task, "task.failed", {"error": {"code": "UH-CAP-4003", "message": str(exc), "retryable": False}})
        finally:
            await q.put(None)

    def resume(self, task_id: str, checkpoint_id: str | None = None) -> dict | None:
        task = self.tasks.get(task_id)
        if task is None:
            return None
        orchestration = self.orchestration
        checkpoint = orchestration.checkpoint(task_id, checkpoint_id) if orchestration is not None else None
        if checkpoint is None:
            return None
        task["queue"] = asyncio.Queue()
        task["status"] = "running"
        skipped_steps = len(checkpoint.completed_nodes)
        asyncio.create_task(self._resume_workflow(task, checkpoint.checkpoint_id, skipped_steps, checkpoint.last_event_id, checkpoint.state.get("content", "")))
        return {"task_id": task_id, "resumed_from_event_id": checkpoint.last_event_id, "checkpoint_id": checkpoint.checkpoint_id}

    async def _resume_workflow(self, task: dict[str, Any], checkpoint_id: str, skipped_steps: int, resumed_from_event_id: int, content: str) -> None:
        await self._emit(
            task,
            "task.resumed",
            {
                "checkpoint_id": checkpoint_id,
                "skipped_steps": skipped_steps,
                "resumed_from_event_id": resumed_from_event_id,
            },
        )
        await self._run_workflow(
            task,
            task["queue"],
            task["session_id"],
            content,
            resume=True,
            checkpoint_id=checkpoint_id,
            context=task.get("context", {}),
        )

    async def stream(self, task_id: str) -> AsyncIterator[dict]:
        if task_id not in self.tasks:
            raise KeyError(task_id)
        result = self.subscribe(task_id, after_event_id=0)
        if result is None:
            raise KeyError(task_id)
        subscriber, replay, terminal = result
        if not self.replay_available(task_id, after_event_id=0):
            self.unsubscribe(task_id, subscriber)
            return
        try:
            for item in replay:
                yield item
            if terminal:
                return
            while True:
                item = await subscriber.get()
                yield item
                if item.get("event") in self.TERMINAL_EVENTS:
                    return
        finally:
            self.unsubscribe(task_id, subscriber)

    def owner_matches(self, task_id: str, *, client_id: str | None = None, token: str | None = None) -> bool:
        task = self.tasks.get(task_id)
        if task is None:
            return False
        owner_client_id = task.get("owner_client_id")
        owner_token = task.get("owner_token")
        # The protocol only standardizes token transport.  When a task was
        # created under a bearer token, that token is the authoritative owner
        # identity and a WebSocket need not also carry the local cookie.
        if owner_token:
            return bool(token and secrets.compare_digest(token, owner_token))
        return not owner_client_id or client_id == owner_client_id

    def subscribe(self, task_id: str, *, after_event_id: int = 0) -> tuple[asyncio.Queue, list[dict[str, Any]], bool] | None:
        """Register a live subscriber and return its replay snapshot.

        The registration and snapshot happen without an await, so events
        produced after this method returns are delivered through the queue and
        cannot be lost between replay and live delivery.
        """
        task = self.tasks.get(task_id)
        if task is None:
            return None
        subscriber: asyncio.Queue = asyncio.Queue()
        task["subscribers"].add(subscriber)
        entries = list(task["event_buffer"])
        events = [entry["event"] for entry in entries]
        terminal = task.get("status") in {"finished", "failed", "cancelled"}
        if terminal:
            terminal_events = [event for event in events if event.get("event") in self.TERMINAL_EVENTS]
            return subscriber, (terminal_events[-1:] if terminal_events else []), True

        latest_id = events[-1]["event_id"] if events else 0
        first_id = events[0]["event_id"] if events else 1
        cutoff = time.time() - self.REPLAY_WINDOW_SECONDS
        missing_prefix = bool(events and first_id > 1 and after_event_id < first_id - 1)
        stale_requested = any(self._entry_recorded_at(entry) < cutoff for entry in entries if entry["event"]["event_id"] > after_event_id)
        if missing_prefix or stale_requested:
            return subscriber, [], False
        if after_event_id >= latest_id:
            return subscriber, [], False
        return subscriber, [event for event in events if event["event_id"] > after_event_id], False

    def unsubscribe(self, task_id: str, subscriber: asyncio.Queue) -> None:
        task = self.tasks.get(task_id)
        if task is not None:
            task["subscribers"].discard(subscriber)

    def replay_available(self, task_id: str, *, after_event_id: int = 0) -> bool:
        task = self.tasks.get(task_id)
        if task is None:
            return False
        if task.get("status") in {"finished", "failed", "cancelled"}:
            return True
        entries = task["event_buffer"]
        if not entries:
            return True
        first_id = entries[0]["event"]["event_id"]
        cutoff = time.time() - self.REPLAY_WINDOW_SECONDS
        return not (first_id > 1 and after_event_id < first_id - 1) and not any(
            self._entry_recorded_at(entry) < cutoff and entry["event"]["event_id"] > after_event_id for entry in entries
        )

    @staticmethod
    def _source_action(event: str) -> str:
        """Compatibility bridge for legacy task producers.

        Task execution still queues its familiar external names internally; the
        stream boundary turns them into the registered three-segment sources.
        """
        return {
            "task.created": "kernel.route.decided",
            "task.finished": "kernel.task.finished",
            "task.failed": "kernel.task.failed",
            "task.cancelled": "kernel.task.cancelled",
            "task.resumed": "kernel.task.resumed",
            "step.started": "capability.module.invoked",
            "step.finished": "capability.module.finished",
            "step.failed": "capability.module.failed",
            "delta": "llm.stream.delta",
            "tool.call": "mcp.tool.call",
            "tool.result": "mcp.tool.result",
            "task.confirmation_required": "tool.confirmation.requested",
            "task.confirmation_resolved": "tool.confirmation.resolved",
            "task.yielded": "cua.session.yielded",
            "task.degraded": "kernel.task.degraded",
            "safety.check_started": "safety.check.started",
            "safety.check_finished": "safety.check.finished",
            "safety.denied": "safety.action.denied",
        }.get(event, event)

    def status(self, task_id: str) -> dict | None:
        t = self.tasks.get(task_id)
        if t is None:
            return None
        return {"task_id": task_id, "status": t["status"], "session_id": t["session_id"]}

    async def _long_term_recall(self, content: str, context: dict[str, Any]) -> list[dict[str, Any]]:
        method = getattr(self.long_term_memory, "arecall", None) or getattr(self.long_term_memory, "recall", None)
        if method is None:
            return []
        result = method(content, top_k=int(context.get("top_k", 5)), session_id=context.get("session_id"), memory_recall=True)
        return await result if inspect.isawaitable(result) else result

    async def _long_term_write(self, content: str, session_id: str, context: dict[str, Any], *, role: str) -> None:
        if not content or not bool(context.get("memory_recall", True)) or self.long_term_memory is None:
            return
        method = getattr(self.long_term_memory, "awrite", None) or getattr(self.long_term_memory, "write", None)
        if method is None:
            return
        try:
            result = method(content, session_id=session_id, metadata={"role": role}, memory_recall=True)
            if inspect.isawaitable(result):
                await result
        except Exception:
            # 长期记忆是增强能力；缺少密钥或后端异常不能阻断当前会话。
            return
