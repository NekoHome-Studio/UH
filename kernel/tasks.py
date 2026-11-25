"""M0 任务管理（M0-5）：POST messages 创建任务 → 后台路由+LLM 流式 → SSE 推送。

M0 用 SSE 简单流式（task-flow.md F2 过渡）；M1-7 升级为 WebSocket 细粒度事件。
事件信封预览 events.md：event_id/event/version/task_id/trace_id/ts/payload。
"""
from __future__ import annotations

import asyncio
import inspect
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from . import otel, tracing
from .collaboration import COLLABORATION_DEADLINE_MS
from .llm import LLMError
from .orchestration import OrchestrationError
from .router import RouteDecision, classify
from .event_mapping import EventMapper
from .protocol import Envelope, module_error_code
from .mcp import MCPError
from .confirmation import ConfirmationManager


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ResumeConflictError(Exception):
    """任务已终态仍请求 resume（违反终态唯一性，审查 P0-2 修复）。"""


class TaskManager:
    REPLAY_WINDOW_SECONDS = 5 * 60
    TERMINAL_EVENTS = frozenset({"task.finished", "task.failed", "task.cancelled"})

    def __init__(self, llm: Any, memory: Any, rpc: Any = None, orchestration: Any = None, long_term_memory: Any = None, confirmations: Any = None, bus: Any = None, *, plugin_host: Any = None):
        self.llm = llm
        self.memory = memory
        self.rpc = rpc
        self.orchestration = orchestration
        self.plugin_host = plugin_host
        self.long_term_memory = long_term_memory
        self.confirmations = confirmations or ConfirmationManager()
        self._confirm_futures: dict[str, asyncio.Future] = {}
        self.tasks: dict[str, dict] = {}
        self.event_mapper = EventMapper()
        self.bus = bus
        if bus is not None:
            self._attach_bus(bus)

    def _attach_bus(self, bus: Any) -> None:
        """bus→task 事件桥（审查 P1-4）：任务关联的内部工具事件映射进任务外部流，
        使 tool.call/tool.result 拥有真实运行时发射器（而非仅 schema fixture）。"""
        async def handler(env: Envelope) -> None:
            tid = env.payload.get("task_id")
            task = self.tasks.get(tid) if tid else None
            if task is None:
                return
            external = {"mcp.tool.call": "tool.call", "mcp.tool.result": "tool.result"}.get(env.action)
            if external is None:
                return
            payload = {k: v for k, v in env.payload.items() if k != "task_id"}
            await self._emit(task, external, payload, span_id=env.span_id)  # 三审 P1-3：保留内部 span_id 供审计反查
        bus.subscribe("mcp.tool.*", handler)

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
        try:
            decision = classify(content, mode)
        except ValueError:
            decision = classify(content, "auto")
        task_id = "uh_task_" + secrets.token_hex(16)
        trace_id = tracing.get_trace_id() if otel.active() else tracing.new_trace_id()
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
            "task_type": decision.task_type,
            "context": dict(context or {}),
            "owner_client_id": owner_client_id or session_id,
            "owner_token": owner_token,
        }
        self._start_runner(self.tasks[task_id], self._run(task_id, session_id, content, decision, dict(context or {})))
        return task_id, trace_id

    def create_tool_task(
        self,
        session_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        risk_level: str,
        host: Any,
        *,
        owner_client_id: str | None = None,
        owner_token: str | None = None,
    ) -> tuple[str, str]:
        """M1-8：高危工具任务，执行前经人工确认门。"""
        task_id = "uh_task_" + secrets.token_hex(16)
        trace_id = tracing.get_trace_id() if otel.active() else tracing.new_trace_id()
        event_buffer: list[dict[str, Any]] = []
        self.tasks[task_id] = {
            "task_id": task_id, "queue": asyncio.Queue(), "subscribers": set(),
            "event_buffer": event_buffer, "events": event_buffer, "status": "running",
            "session_id": session_id, "trace_id": trace_id, "seq": 0, "mode": "tool",
            "context": {}, "owner_client_id": owner_client_id or session_id, "owner_token": owner_token,
        }
        self._start_runner(self.tasks[task_id], self._run_tool(task_id, session_id, tool_name, arguments, risk_level, host))
        return task_id, trace_id

    def _start_runner(self, task: dict, coro) -> None:
        if otel.active():
            if task["trace_id"] == tracing.get_trace_id():
                task["_trace_headers"] = otel.outbound_headers()
            runner = otel.start_task(coro, task["trace_id"], carrier=task.get("_trace_headers"))
        else:
            runner = asyncio.create_task(coro)
        task["_runner"] = runner

    async def _run_tool(self, task_id: str, session_id: str, tool_name: str, arguments: dict[str, Any], risk_level: str, host: Any) -> None:
        t = self.tasks[task_id]
        q: asyncio.Queue = t["queue"]
        with tracing.request_context(t["trace_id"], span_id=tracing.get_span_id() if otel.active() else None):
            await self._emit(t, "task.created", {"session_id": session_id, "mode": "tool"})

            async def confirm_cb(name: str, risk: str, summary: str, call_id: str) -> str:
                req = self.confirmations.request(task_id, name, risk, summary, call_id)
                t.pop("confirm_timeout", None)  # 二审 N9：每轮重置，防跨轮污染审计
                t["status"] = "pending_confirmation"
                fut: asyncio.Future = asyncio.get_running_loop().create_future()
                self._confirm_futures[task_id] = fut
                await self._emit(t, "task.confirmation_required", {
                    "call_id": req.call_id, "tool_name": name, "risk_level": risk,
                    "summary": summary, "confirmation_token": req.token, "expires_at": req.expires_at,
                })
                try:
                    decision = await asyncio.wait_for(fut, timeout=self.confirmations.ttl_s)
                except asyncio.TimeoutError:
                    # TTL 自动超时收敛：拒绝执行 + resolved_by=timeout + 审计
                    decision = "rejected"
                    t["confirm_timeout"] = True
                    self.confirmations.mark_timeout(req)
                await self._emit(t, "task.confirmation_resolved", {
                    "call_id": req.call_id, "decision": decision,
                    "resolved_by": "timeout" if t.get("confirm_timeout") else "user",
                })
                return decision

            try:
                if self.plugin_host is not None:
                    host = self.plugin_host(tool_name) or host
                result = await host.call_tool(tool_name, arguments, risk_level=risk_level, confirm=confirm_cb, trace_id=t["trace_id"], task_id=task_id)
                await self._emit(t, "task.finished", {"final_result": {"content": str(result.get("content", result))}, "duration_ms": 0})
            except MCPError as exc:
                if exc.code == "UH-USR-1002":
                    await self._emit(t, "task.cancelled", {
                        "reason": "timeout" if t.get("confirm_timeout") else "confirmation_rejected",
                        "error": {"code": "UH-USR-1002", "message": exc.message, "retryable": False},
                        "completed_steps": 0,
                    })
                else:
                    await self._emit(t, "task.failed", {"error": exc.to_dict()})
            except Exception:
                # 审查修复：非 MCPError 异常也必须送达终态，不得永久 running
                await self._emit(t, "task.failed", {"error": {"code": "UH-KRN-5001", "message": "工具任务执行异常", "retryable": False}})
            finally:
                self._confirm_futures.pop(task_id, None)
                await q.put(None)

    def steps(self, task_id: str) -> list[dict[str, Any]] | None:
        """超窗回退载体（task-flow §5.4 / 决议 C8）：seq = step.started 的 event_id。"""
        task = self.tasks.get(task_id)
        if task is None:
            return None
        # 三审 P1-5：读永久 step_log（非裁剪缓冲），超窗仍可全量重建
        return list(task.get("step_log", {}).values())

    async def cancel(self, task_id: str, reason: str = "user_request") -> dict | None:
        """取消任务（task-flow §4）：终态任务抛 ResumeConflictError（端点转 409）。"""
        task = self.tasks.get(task_id)
        if task is None:
            return None
        if task.get("status") in {"finished", "failed", "cancelled"}:
            raise ResumeConflictError("任务已终态，不可取消")
        completed = sum(1 for e in task["event_buffer"] if e["event"]["event"] == "step.finished")
        runner = task.get("_runner")
        if runner is not None and not runner.done():
            runner.cancel()
        # 二审 GPT-6：取消使 pending 确认令牌失效，防取消后仍可批准
        self.confirmations.invalidate_task(task_id)
        fut = self._confirm_futures.get(task_id)
        if fut is not None and not fut.done():
            fut.set_result("rejected")
        task["status"] = "cancelling"  # 非 running 避免 _emit 裁剪缓冲；_emit 置终态
        # 二审 R7：取消时为未闭合的 step 补 step.failed(status=cancelled)，满足 §5.3 配对
        started = {e["event"]["payload"].get("step_id"): e["event"]["payload"] for e in task["event_buffer"] if e["event"]["event"] == "step.started"}
        closed = {e["event"]["payload"].get("step_id") for e in task["event_buffer"] if e["event"]["event"] in {"step.finished", "step.failed"}}
        for sid, payload in started.items():
            if sid and sid not in closed:
                await self._emit(task, "step.failed", {"step_id": sid, "module": payload.get("module", "llm"), "error": {"code": "UH-KRN-5001", "message": "任务取消，步骤未闭合", "retryable": False}}, audit_fields={"cancelled": True})
        await self._emit(task, "task.cancelled", {"reason": reason, "completed_steps": completed})
        return {"task_id": task_id, "reason": reason, "completed_steps": completed}

    def resolve_confirmation(self, task_id: str, token: str, decision: str, operator: str = "user"):
        """REST/CLI 确认入口：裁决令牌并唤醒等待中的任务。"""
        req = self.confirmations.resolve(token, decision, operator=operator, task_id=task_id)
        fut = self._confirm_futures.get(task_id)
        if fut is not None and not fut.done():
            fut.set_result(decision)
        return req

    async def _emit(self, task: dict[str, Any], event: str, payload: dict[str, Any], *, span_id: str | None = None, audit_fields: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Map, retain, and broadcast one external event.

        Mapping happens at production time so a task remains replayable even
        when no SSE or WebSocket consumer was connected while it ran.
        """
        task_id = task["task_id"]
        if event == "task.failed" and otel.active():
            # The HTTP span may already have ended when a 202 task fails.
            with otel.span("task.failed"):
                otel.mark_error()
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
        # 三审 P1-5：step 永久日志（不随 5min 缓冲裁剪），供超窗全量回退；duration_ms 恒 integer
        if event in {"step.started", "step.finished", "step.failed"}:
            slog = task.setdefault("step_log", {})
            sid = payload.get("step_id")
            if sid:
                if event == "step.started":
                    slog[sid] = {"seq": mapped["event_id"], "step_id": sid, "module": payload.get("module"), "name": payload.get("name"), "status": "running", "duration_ms": 0}
                elif sid in slog:
                    slog[sid]["status"] = "done" if event == "step.finished" else "failed"
                    slog[sid]["duration_ms"] = int(payload.get("duration_ms") or 0)
        if event in self.TERMINAL_EVENTS:
            task["status"] = event.rsplit(".", 1)[-1]
        now = time.time()
        task["event_buffer"].append({"event": mapped, "recorded_at": now})
        if task["status"] == "running":
            # M3-9 性能：原实现每条事件都全量重建 buffer（O(n)），delta 高频下
            # 100 并发 × 45 token 会退化成 O(n²)。改为按计数节流 + 从头裁剪
            # （buffer 按时间递增，遇到未过期即可停止）。
            buffer = task["event_buffer"]
            task["_trim_counter"] = task.get("_trim_counter", 0) + 1
            if len(buffer) > 256 and task["_trim_counter"] % 32 == 0:
                cutoff = now - self.REPLAY_WINDOW_SECONDS
                drop = 0
                for entry in buffer:
                    if self._entry_recorded_at(entry) >= cutoff:
                        break
                    drop += 1
                if drop:
                    del buffer[:drop]
        elif event in self.TERMINAL_EVENTS:
            task["event_buffer"][:] = [e for e in task["event_buffer"] if e["event"]["event"] != "delta"]  # 四审 P1-4：终态释放 delta bulk，保留结构事件（step_log 保全量步骤）
        for subscriber in tuple(task["subscribers"]):
            await subscriber.put(mapped)
        # M3-9 性能：delta 是 UI 流式增量（每 token 一条），不承载审计/指标语义。
        # hooks 订阅 "*"、metrics 订阅具体事件，让 delta 进内部总线会把
        # 45 token × 100 并发 = 4500 次 publish 放大成钩子队列洪泛（实测 P95 >12s）。
        # delta 仍走 task["subscribers"] 推给 SSE/WS 消费者，外部契约不变。
        if self.bus is not None and event not in {"tool.call", "tool.result", "delta"}:
            if event == "task.created":
                await self.bus.publish(
                    "kernel.request.received", {"task_id": task_id, "session_id": task["session_id"]},
                    trace_id=source.trace_id,
                )
            internal_payload = {"index": payload["index"]} if event == "delta" else dict(payload)
            internal_payload.update(audit_fields or {}, task_id=task_id)
            internal_payload.pop("confirmation_token", None)
            internal_payload.pop("final_result", None)
            await self.bus.publish(source.action, internal_payload, trace_id=source.trace_id, span_id=source.span_id)
        return mapped

    async def _run(self, task_id: str, session_id: str, content: str, decision: RouteDecision, context: dict[str, Any] | None = None) -> None:
        t = self.tasks[task_id]
        q: asyncio.Queue = t["queue"]
        with tracing.request_context(t["trace_id"], span_id=tracing.get_span_id() if otel.active() else None):
            context = context or {}
            memory_recall = bool(context.get("memory_recall", True))
            retrieval_context = ""
            long_term_context = ""
            answer = None
            try:
                await self._emit(t, "task.created", {"session_id": session_id, "mode": decision.task_type}, audit_fields={
                    "task_type": decision.task_type, "rule": decision.rule,
                    "confidence": decision.confidence, "fallback": decision.fallback,
                })
                await self.memory.append(session_id, "user", content)
                if decision.task_type == "workflow" and self.rpc is not None:
                    await self._run_workflow(t, q, session_id, content, context=context)
                    return
                if decision.task_type == "collaborate" and self.rpc is not None:
                    result = await self.rpc.call(
                        "collaboration.run", {"task_id": task_id, "content": content},
                        trace_id=t["trace_id"], deadline_ms=COLLABORATION_DEADLINE_MS,
                    )
                    if result.payload.get("degraded"):
                        await self._emit(t, "task.degraded", {
                            "reason_code": result.payload.get("error_code", module_error_code()),
                            "module": "crewai", "description": "协作服务不可用，降级为普通对话",
                        })
                    else:
                        answer = result.payload["answer"]
                if answer is None and memory_recall and self.long_term_memory is not None:
                    rows = await self._long_term_recall(content, context, session_id=session_id)
                    long_term_context = "\n\n".join(str(row.get("content", "")) for row in rows if row.get("content"))
                if decision.task_type == "search" and self.rpc is not None:
                    r = await self.rpc.call("retrieval.search", {"q": content}, trace_id=t["trace_id"])
                    if r.payload.get("degraded"):
                        await self._emit(t, "task.degraded", {"reason_code": "UH-CAP-3001", "module": "llama_index", "description": "检索不可用，降级为纯对话"})
                    else:
                        docs = r.payload.get("docs") or r.payload.get("results") or []
                        retrieval_context = "\n\n".join(str(d.get("untrusted_content", d.get("content", ""))) for d in docs if isinstance(d, dict))
                module = "llm" if answer is None else "crewai"
                await self._emit(t, "step.started", {"step_id": "st_1", "module": module, "name": "generate"})
            except asyncio.CancelledError:
                await q.put(None)
                raise
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
                if answer is None:
                    async for delta in self.llm.stream_chat([{"role": "user", "content": prompt}]):
                        parts.append(delta)
                        await self._emit(t, "delta", {"text": delta, "index": len(parts) - 1})
                    answer = "".join(parts)
                await self.memory.append(session_id, "assistant", answer)
                await self._long_term_write(content, session_id, context, role="user")
                await self._long_term_write(answer, session_id, context, role="assistant")
                await self._emit(t, "step.finished", {"step_id": "st_1", "module": module, "status": "done", "duration_ms": 0})
                await self._emit(t, "task.finished", {"final_result": {"content": answer}, "duration_ms": 0})
            except LLMError as e:
                # 审查 P1-5：step.started 须配对 step.failed，前端阶段条才不卡 running
                await self._emit(t, "step.failed", {"step_id": "st_1", "module": module, "error": {"code": e.code, "message": e.message, "retryable": e.retryable}})
                await self._emit(t, "task.failed", {"error": {"code": e.code, "message": e.message, "retryable": e.retryable}})
            except Exception as exc:
                await self._emit(t, "step.failed", {"step_id": "st_1", "module": module, "error": {"code": "UH-KRN-5001", "message": "任务执行失败", "retryable": False}})
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
            # 审查 P0-5：任务级 deadline 须 ≥ 节点预算之和，否则 RPC 默认 8s 抢先杀真实 workflow。
            task_deadline_ms = int(os.environ.get("UH_TASK_DEADLINE_MS", "120000"))
            result = await self.rpc.call(
                "orchestration.run",
                {"task_id": task_id, "content": content, "emit": emit, "resume": resume, "checkpoint_id": checkpoint_id, "tool": context.get("tool")},
                trace_id=task["trace_id"],
                deadline_ms=task_deadline_ms,
            )
            if result.payload.get("degraded"):
                error = result.payload.get("error", {})
                if error.get("detail", {}).get("module") == "tool":
                    await self._emit(task, "task.failed", {"error": error})
                    return
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
            await self._emit(task, "task.failed", {"error": {"code": module_error_code(), "message": "编排任务执行异常", "retryable": False}})
        finally:
            await q.put(None)

    def resume(self, task_id: str, checkpoint_id: str | None = None) -> dict | None:
        task = self.tasks.get(task_id)
        if task is None:
            return None
        # 三审 N3：终态 guard 前移——任何终态任务 resume 一致 409（含无 checkpoint 的 chat 任务）
        if task.get("status") in {"finished", "failed", "cancelled"}:
            raise ResumeConflictError("任务已终态，不可 resume；重跑请创建新任务")
        orchestration = self.orchestration
        checkpoint = orchestration.checkpoint(task_id, checkpoint_id) if orchestration is not None else None
        if checkpoint is None:
            return None
        if checkpoint.status == "completed":
            raise ResumeConflictError("任务已终态，不可 resume；重跑请创建新任务")
        task["queue"] = asyncio.Queue()
        task["status"] = "running"
        skipped_steps = len(checkpoint.completed_nodes)
        self._start_runner(task, self._resume_workflow(task, checkpoint.checkpoint_id, skipped_steps, checkpoint.last_event_id, checkpoint.state.get("content", "")))
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
        # 四审 P0-2：双因子——设了 token 须 token 匹配，设了 client_id 须 client_id 匹配，
        # 防单共享 token 下 client_id 分支被旁路致越权。注：client_id 为自 asserted，
        # 完全多用户隔离需 per-user principal（M2/M3）；单共享 token 模型假设单用户设备。
        if owner_token and not (token and secrets.compare_digest(token, owner_token)):
            return False
        if owner_client_id and client_id != owner_client_id:
            return False
        return True

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

    async def _long_term_recall(self, content: str, context: dict[str, Any], session_id: str | None = None) -> list[dict[str, Any]]:
        method = getattr(self.long_term_memory, "arecall", None) or getattr(self.long_term_memory, "recall", None)
        if method is None:
            return []
        # ADR-003：记忆召回范围双模式——global 跨会话（默认）/ session 仅本会话；
        # context.memory_scope 覆盖全局 UH_MEMORY_SCOPE，插件可指定。
        scope = str(context.get("memory_scope") or os.environ.get("UH_MEMORY_SCOPE", "global")).lower()
        recall_session = session_id if scope == "session" else context.get("session_id")
        # M3-9：同步召回同样是纯 CPU（embedding + 余弦），移出事件循环。
        if inspect.iscoroutinefunction(method):
            result = await method(content, top_k=int(context.get("top_k", 5)), session_id=recall_session, memory_recall=True)
        else:
            result = await asyncio.to_thread(
                method, content, top_k=int(context.get("top_k", 5)), session_id=recall_session, memory_recall=True
            )
        return await result if inspect.isawaitable(result) else result

    async def _long_term_write(self, content: str, session_id: str, context: dict[str, Any], *, role: str) -> None:
        if not content or not bool(context.get("memory_recall", True)) or self.long_term_memory is None:
            return
        method = getattr(self.long_term_memory, "awrite", None) or getattr(self.long_term_memory, "write", None)
        if method is None:
            return
        try:
            await self._write_memory(method, content, session_id, role)
        except Exception:
            # 长期记忆是增强能力；缺少密钥或后端异常不能阻断当前会话。
            return

    @otel.instrument_async("memory.write")
    async def _write_memory(self, method, content: str, session_id: str, role: str) -> None:
        # M3-9：同步长期记忆写入含 embedding + AES-GCM 加密 + 写盘（实测 ~110ms/次），
        # 直接在事件循环里跑会连续占用 CPU 无抢占点，高并发下饿死请求提交路径。
        if inspect.iscoroutinefunction(method):
            result = await method(content, session_id=session_id, metadata={"role": role}, memory_recall=True)
        else:
            result = await asyncio.to_thread(
                method, content, session_id=session_id, metadata={"role": role}, memory_recall=True
            )
        if inspect.isawaitable(result):
            result = await result
        if result is not None and self.bus is not None:
            span_id = otel.envelope_ids()[0] if otel.active() else ""
            await self.bus.publish("memory.record.write", {"session_id": session_id, "role": role},
                                   trace_id=tracing.get_trace_id() or "", span_id=span_id)
