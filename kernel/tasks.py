"""M0 任务管理（M0-5）：POST messages 创建任务 → 后台路由+LLM 流式 → SSE 推送。

M0 用 SSE 简单流式（task-flow.md F2 过渡）；M1-7 升级为 WebSocket 细粒度事件。
事件信封预览 events.md：event_id/event/version/task_id/trace_id/ts/payload。
"""
from __future__ import annotations

import asyncio
import secrets
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from . import tracing
from .llm import LLMError
from .router import classify


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class TaskManager:
    def __init__(self, llm: Any, memory: Any, rpc: Any = None):
        self.llm = llm
        self.memory = memory
        self.rpc = rpc
        self.tasks: dict[str, dict] = {}

    def create(self, session_id: str, content: str, mode: str = "auto") -> tuple[str, str]:
        task_id = "uh_task_" + secrets.token_hex(16)
        trace_id = tracing.new_trace_id()
        self.tasks[task_id] = {"queue": asyncio.Queue(), "status": "running", "session_id": session_id, "trace_id": trace_id, "seq": 0}
        asyncio.create_task(self._run(task_id, session_id, content, mode))
        return task_id, trace_id

    async def _run(self, task_id: str, session_id: str, content: str, mode: str) -> None:
        t = self.tasks[task_id]
        q: asyncio.Queue = t["queue"]
        with tracing.request_context(t["trace_id"]):
            try:
                decision = classify(content, mode)
            except ValueError:
                decision = classify(content, "auto")
            await q.put({"event": "task.created", "payload": {"session_id": session_id, "mode": decision.task_type}})
            await self.memory.append(session_id, "user", content)
            if decision.task_type == "search" and self.rpc is not None:
                r = await self.rpc.call("retrieval.search", {"q": content}, trace_id=t["trace_id"])
                if r.payload.get("degraded"):
                    await q.put({"event": "task.degraded", "payload": {"reason_code": "UH-CAP-3001", "module": "retrieval", "description": "检索不可用，降级为纯对话"}})
            await q.put({"event": "step.started", "payload": {"step_id": "st_1", "module": "llm", "name": "generate"}})
            parts: list[str] = []
            try:
                async for delta in self.llm.stream_chat([{"role": "user", "content": content}]):
                    parts.append(delta)
                    await q.put({"event": "delta", "payload": {"text": delta, "index": len(parts) - 1}})
                answer = "".join(parts)
                await self.memory.append(session_id, "assistant", answer)
                await q.put({"event": "step.finished", "payload": {"step_id": "st_1", "module": "llm", "status": "done"}})
                await q.put({"event": "task.finished", "payload": {"final_result": {"content": answer}}})
                t["status"] = "finished"
            except LLMError as e:
                await q.put({"event": "task.failed", "payload": {"error": {"code": e.code, "message": e.message, "retryable": e.retryable}}})
                t["status"] = "failed"
            finally:
                await q.put(None)

    async def stream(self, task_id: str) -> AsyncIterator[dict]:
        t = self.tasks.get(task_id)
        if t is None:
            raise KeyError(task_id)
        while True:
            item = await t["queue"].get()
            if item is None:
                break
            t["seq"] += 1
            yield {"event_id": t["seq"], "event": item["event"], "version": 1, "task_id": task_id, "trace_id": t["trace_id"], "ts": _now(), "payload": item["payload"]}

    def status(self, task_id: str) -> dict | None:
        t = self.tasks.get(task_id)
        if t is None:
            return None
        return {"task_id": task_id, "status": t["status"], "session_id": t["session_id"]}
