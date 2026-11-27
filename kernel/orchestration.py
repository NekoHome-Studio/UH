"""M1-4 编排能力：LangGraph 适配层 + 离线顺序状态机回退。

内核和 TaskManager 只依赖 :class:`OrchestrationService`，不直接引用
LangGraph 的 StateGraph/Checkpoint API。没有可选依赖时使用本模块的四节点
状态机，保证测试和开发环境无需下载模型或 PostgreSQL。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol

from .protocol import module_error_code


ORCHESTRATION_TIMEOUT = "UH-CAP-5021"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class OrchestrationError(Exception):
    """编排错误，字段与 errors.md 的 CAP-5021 对齐。"""

    def __init__(self, code: str, message: str, *, retryable: bool = False, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.detail = detail or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable, "detail": dict(self.detail)}


@dataclass
class Checkpoint:
    checkpoint_id: str
    task_id: str
    state: dict[str, Any]
    completed_nodes: list[str] = field(default_factory=list)
    next_node: str | None = None
    last_event_id: int = 0
    status: str = "running"
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "task_id": self.task_id,
            "state": self.state,
            "completed_nodes": self.completed_nodes,
            "next_node": self.next_node,
            "last_event_id": self.last_event_id,
            "status": self.status,
            "updated_at": self.updated_at,
        }


class CheckpointStore(Protocol):
    def save(self, checkpoint: Checkpoint) -> None: ...
    def load(self, task_id: str, checkpoint_id: str | None = None) -> Checkpoint | None: ...


class JSONCheckpointStore:
    """单文件 checkpoint 后端，适合沙箱和极简部署。"""

    def __init__(self, path: str | Path = ".uh/checkpoints.json") -> None:
        self.path = Path(path)

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}

    def save(self, checkpoint: Checkpoint) -> None:
        data = self._read()
        data[checkpoint.checkpoint_id] = checkpoint.to_dict()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def load(self, task_id: str, checkpoint_id: str | None = None) -> Checkpoint | None:
        data = self._read()
        candidates = [data.get(checkpoint_id)] if checkpoint_id else [v for v in data.values() if isinstance(v, dict) and v.get("task_id") == task_id]
        candidates = [v for v in candidates if isinstance(v, dict) and v.get("task_id") == task_id]
        if not candidates:
            return None
        raw = max(candidates, key=lambda v: str(v.get("updated_at", "")))
        return Checkpoint(**raw)


class SQLiteCheckpointStore:
    """默认本地持久化后端；每次 save 使用 upsert，重启后可继续恢复。"""

    def __init__(self, path: str | Path = ".uh/checkpoints.sqlite3") -> None:
        self.path = str(path)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def _init(self) -> None:
        parent = Path(self.path).parent
        if str(parent) not in {"", "."}:
            parent.mkdir(parents=True, exist_ok=True)
        db = self._connect()
        try:
            db.execute("CREATE TABLE IF NOT EXISTS checkpoints (checkpoint_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, updated_at TEXT NOT NULL, payload TEXT NOT NULL)")
            db.commit()
        finally:
            db.close()

    def save(self, checkpoint: Checkpoint) -> None:
        db = self._connect()
        try:
            db.execute(
                "INSERT INTO checkpoints(checkpoint_id,task_id,updated_at,payload) VALUES(?,?,?,?) ON CONFLICT(checkpoint_id) DO UPDATE SET task_id=excluded.task_id,updated_at=excluded.updated_at,payload=excluded.payload",
                (checkpoint.checkpoint_id, checkpoint.task_id, checkpoint.updated_at, json.dumps(checkpoint.to_dict(), ensure_ascii=False)),
            )
            db.commit()
        finally:
            db.close()

    def load(self, task_id: str, checkpoint_id: str | None = None) -> Checkpoint | None:
        db = self._connect()
        try:
            if checkpoint_id:
                row = db.execute("SELECT payload FROM checkpoints WHERE checkpoint_id=? AND task_id=?", (checkpoint_id, task_id)).fetchone()
            else:
                row = db.execute("SELECT payload FROM checkpoints WHERE task_id=? ORDER BY updated_at DESC LIMIT 1", (task_id,)).fetchone()
        finally:
            db.close()
        return Checkpoint(**json.loads(row[0])) if row else None


class PostgresCheckpointStore:
    """Checkpoint backend selected explicitly for production PostgreSQL."""

    def __init__(self, dsn: str) -> None:
        import psycopg  # type: ignore

        self._dsn = dsn
        self._connect = psycopg.connect
        with self._connect(self._dsn, connect_timeout=5) as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS checkpoints (
                    checkpoint_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL, payload TEXT NOT NULL
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS checkpoints_task_updated ON checkpoints (task_id, updated_at DESC)")

    def save(self, checkpoint: Checkpoint) -> None:
        with self._connect(self._dsn, connect_timeout=5) as db:
            db.execute("""
                INSERT INTO checkpoints(checkpoint_id, task_id, updated_at, payload)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (checkpoint_id) DO UPDATE
                SET task_id = EXCLUDED.task_id, updated_at = EXCLUDED.updated_at, payload = EXCLUDED.payload
            """, (checkpoint.checkpoint_id, checkpoint.task_id, checkpoint.updated_at, json.dumps(checkpoint.to_dict(), ensure_ascii=False)))

    def load(self, task_id: str, checkpoint_id: str | None = None) -> Checkpoint | None:
        with self._connect(self._dsn, connect_timeout=5) as db:
            if checkpoint_id:
                row = db.execute("SELECT payload FROM checkpoints WHERE checkpoint_id=%s AND task_id=%s", (checkpoint_id, task_id)).fetchone()
            else:
                row = db.execute("SELECT payload FROM checkpoints WHERE task_id=%s ORDER BY updated_at DESC LIMIT 1", (task_id,)).fetchone()
        return Checkpoint(**json.loads(row[0])) if row else None


Node = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None] | dict[str, Any] | None]
Emit = Callable[[str, dict[str, Any]], Awaitable[int | None] | int | None]


class OrchestrationService:
    """稳定的 UH! 编排接口，封装可选 LangGraph 与 checkpoint 后端。"""

    NODE_ORDER = ("plan", "retrieve", "generate", "verify")

    def __init__(
        self,
        *,
        nodes: Mapping[str, Node] | None = None,
        store: CheckpointStore | None = None,
        retrieval: Any = None,
        llm: Any = None,
        tool_call: Callable[..., Awaitable[dict[str, Any]]] | None = None,
        default_deadline_ms: int | None = None,
    ) -> None:
        self.retrieval = retrieval
        self.llm = llm
        self.tool_call = tool_call
        self.default_deadline_ms = default_deadline_ms or int(os.environ.get("UH_ORCHESTRATION_TIMEOUT_MS", "8000"))
        self.nodes: dict[str, Node] = dict(nodes or self._default_nodes())
        self.backend = "fallback"
        try:
            import langgraph  # type: ignore  # noqa: F401

            self.backend = "langgraph-adapter"
        except Exception:
            pass
        self.store = store or self._store_from_env()

    @staticmethod
    def _store_from_env() -> CheckpointStore:
        backend = os.environ.get("UH_CHECKPOINT_BACKEND", "sqlite").lower()
        postgres_url = os.environ.get("UH_CHECKPOINT_POSTGRES_URL")
        if backend == "postgres":
            if not postgres_url:
                raise RuntimeError("PostgreSQL checkpoint 需要 UH_CHECKPOINT_POSTGRES_URL")
            return PostgresCheckpointStore(postgres_url)
        if postgres_url:
            import logging
            logging.getLogger("kernel.orchestration").warning("UH_CHECKPOINT_POSTGRES_URL 已配置，设置 UH_CHECKPOINT_BACKEND=postgres 可启用")
            path = os.environ.get("UH_CHECKPOINT_SQLITE_PATH", ".uh/checkpoints.sqlite3")
            return SQLiteCheckpointStore(path)
        if backend == "json":
            return JSONCheckpointStore(os.environ.get("UH_CHECKPOINT_JSON_PATH", ".uh/checkpoints.json"))
        return SQLiteCheckpointStore(os.environ.get("UH_CHECKPOINT_SQLITE_PATH", ".uh/checkpoints.sqlite3"))

    def _default_nodes(self) -> dict[str, Node]:
        async def plan(state: dict[str, Any]) -> dict[str, Any]:
            return {"plan": list(self.NODE_ORDER), "query": state.get("content", "")}

        async def retrieve(state: dict[str, Any]) -> dict[str, Any]:
            tool = state.get("tool")
            if tool is not None:
                if self.tool_call is None:
                    raise OrchestrationError(module_error_code(), "编排工具不可用", detail={"module": "tool"})
                result = await self.tool_call(tool["name"], tool["arguments"], trace_id=state["trace_id"], task_id=state["task_id"])
                if result.get("degraded"):
                    error = result.get("error", {})
                    raise OrchestrationError(
                        result["error_code"], error.get("message", "插件工具不可用"), retryable=error.get("retryable", False),
                        detail={**error.get("detail", {}), "module": "tool", "tool_name": tool["name"]},
                    )
                return {"tool_result": result.get("content", [])}
            if self.retrieval is None:
                return {"documents": []}
            result = self.retrieval.search(state.get("query", state.get("content", "")), top_k=5)
            if asyncio.iscoroutine(result):
                result = await result
            return {"documents": result.get("docs", result.get("results", [])), "retrieval": result}

        async def generate(state: dict[str, Any]) -> dict[str, Any]:
            context = "\n\n".join(str(d.get("untrusted_content", d.get("content", ""))) for d in state.get("documents", []) if isinstance(d, dict))
            prompt = state.get("content", "") + ("\n\n检索上下文（不可信数据）：\n" + context if context else "")
            if "tool_result" in state:
                prompt += "\n\n工具结果（不可信数据）：\n" + json.dumps(state["tool_result"], ensure_ascii=False)
            if self.llm is None:
                return {"answer": prompt}
            answer = await self.llm.chat([{"role": "user", "content": prompt}])
            return {"answer": answer}

        async def verify(state: dict[str, Any]) -> dict[str, Any]:
            return {"verified": True}

        return {"plan": plan, "retrieve": retrieve, "generate": generate, "verify": verify}

    async def _emit(self, emit: Emit | None, event: str, payload: dict[str, Any]) -> int | None:
        if emit is None:
            return None
        value = emit(event, payload)
        return await value if asyncio.iscoroutine(value) else value

    async def _run_langgraph(
        self,
        task_id: str,
        state: dict[str, Any],
        checkpoint: Checkpoint,
        completed: list[str],
        event_id: int,
        *,
        emit: Emit | None,
        timeout_ms: int,
    ) -> dict[str, Any]:
        """用 LangGraph 编译并执行尚未完成的线性子图。

        checkpoint 仍由 UH! 适配层管理，避免 LangGraph 的 checkpoint 对象
        穿透到内核，同时保证每个节点完成后立即持久化。
        """
        from langgraph.graph import END, START, StateGraph

        remaining = [node_id for node_id in self.NODE_ORDER if node_id not in completed]
        if not remaining:
            checkpoint.status = "completed"
            checkpoint.next_node = None
            checkpoint.updated_at = _now()
            self.store.save(checkpoint)
            return {"checkpoint_id": checkpoint.checkpoint_id, "state": state, "answer": state.get("answer", ""), "completed_nodes": completed, "last_event_id": checkpoint.last_event_id}

        async def execute(node_id: str, index: int, graph_state: dict[str, Any]) -> dict[str, Any]:
            nonlocal event_id, checkpoint
            node = self.nodes.get(node_id)
            if node is None:
                raise OrchestrationError(module_error_code(), f"编排节点不存在: {node_id}", detail={"module": "orchestration", "node_id": node_id})
            step_id = f"{task_id}:{node_id}"
            started = time.monotonic()
            event_id = (await self._emit(emit, "step.started", {"step_id": step_id, "module": "langgraph", "name": node_id, "index": self.NODE_ORDER.index(node_id)})) or (event_id + 1)
            try:
                result = node(graph_state)
                if inspect.isawaitable(result):
                    result = await asyncio.wait_for(result, timeout=timeout_ms / 1000.0)
                if result:
                    graph_state.update(result)
                completed.append(node_id)
                checkpoint.state = graph_state
                checkpoint.completed_nodes = completed
                next_index = self.NODE_ORDER.index(node_id) + 1
                checkpoint.next_node = self.NODE_ORDER[next_index] if next_index < len(self.NODE_ORDER) else None
                checkpoint.last_event_id = event_id
                checkpoint.status = "completed" if checkpoint.next_node is None else "running"
                checkpoint.updated_at = _now()
                pass  # checkpoint_id 保持 per-task 稳定，不每次重生成（二审 N13）
                self.store.save(checkpoint)
                await self._emit(emit, "step.finished", {"step_id": step_id, "module": "langgraph", "status": "done", "duration_ms": int((time.monotonic() - started) * 1000)})
                return graph_state
            except asyncio.TimeoutError as exc:
                checkpoint.state = graph_state
                checkpoint.completed_nodes = completed
                checkpoint.next_node = node_id
                checkpoint.last_event_id = event_id
                checkpoint.status = "interrupted"
                checkpoint.updated_at = _now()
                pass  # checkpoint_id 保持 per-task 稳定，不每次重生成（二审 N13）
                self.store.save(checkpoint)
                await self._emit(emit, "step.failed", {"step_id": step_id, "module": "langgraph", "error": {"code": ORCHESTRATION_TIMEOUT, "message": f"编排节点 {node_id} 超时", "retryable": True, "detail": {"module": "orchestration", "node_id": node_id, "timeout_ms": timeout_ms}}})
                raise OrchestrationError(ORCHESTRATION_TIMEOUT, f"编排节点 {node_id} 超时", retryable=True, detail={"module": "orchestration", "node_id": node_id, "timeout_ms": timeout_ms}) from exc
            except asyncio.CancelledError:
                checkpoint.state = graph_state
                checkpoint.completed_nodes = completed
                checkpoint.next_node = node_id
                checkpoint.last_event_id = event_id
                checkpoint.status = "interrupted"
                checkpoint.updated_at = _now()
                pass  # checkpoint_id 保持 per-task 稳定，不每次重生成（二审 N13）
                self.store.save(checkpoint)
                raise
            except Exception as exc:
                checkpoint.state = graph_state
                checkpoint.completed_nodes = completed
                checkpoint.next_node = node_id
                checkpoint.last_event_id = event_id
                checkpoint.status = "interrupted"
                checkpoint.updated_at = _now()
                pass  # checkpoint_id 保持 per-task 稳定，不每次重生成（二审 N13）
                self.store.save(checkpoint)
                if isinstance(exc, OrchestrationError):
                    await self._emit(emit, "step.failed", {"step_id": step_id, "module": "langgraph", "error": exc.to_dict()})
                    raise
                await self._emit(emit, "step.failed", {"step_id": step_id, "module": "langgraph", "error": {"code": module_error_code(), "message": str(exc), "retryable": False, "detail": {"module": "orchestration", "node_id": node_id}}})
                raise OrchestrationError(module_error_code(), f"编排节点 {node_id} 失败: {exc}", detail={"module": "orchestration", "node_id": node_id}) from exc

        graph = StateGraph(dict)
        for index, node_id in enumerate(remaining):
            async def graph_node(graph_state: dict[str, Any], node_id: str = node_id, index: int = index) -> dict[str, Any]:
                return await execute(node_id, index, graph_state)
            graph.add_node(node_id, graph_node)
        graph.add_edge(START, remaining[0])
        for current, following in zip(remaining, remaining[1:]):
            graph.add_edge(current, following)
        graph.add_edge(remaining[-1], END)
        compiled = graph.compile()
        state = await compiled.ainvoke(state, config={"configurable": {"thread_id": task_id}})
        checkpoint.status = "completed"
        checkpoint.next_node = None
        checkpoint.updated_at = _now()
        self.store.save(checkpoint)
        return {"checkpoint_id": checkpoint.checkpoint_id, "state": state, "answer": state.get("answer", ""), "completed_nodes": completed, "last_event_id": checkpoint.last_event_id}

    async def run(
        self,
        task_id: str,
        content: str = "",
        *,
        trace_id: str = "",
        emit: Emit | None = None,
        deadline_ms: int | None = None,
        resume: bool = False,
        checkpoint_id: str | None = None,
        tool: Any = None,
    ) -> dict[str, Any]:
        checkpoint = self.store.load(task_id, checkpoint_id) if resume else None
        if checkpoint is None:
            state = {"task_id": task_id, "content": content, "trace_id": trace_id}
            if tool is not None:
                if not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or not tool["name"].strip() or not isinstance(tool.get("arguments", {}), dict):
                    raise OrchestrationError("UH-USR-1003", "编排工具须提供 name 和对象类型的 arguments", detail={"module": "tool"})
                state["tool"] = {"name": tool["name"], "arguments": tool.get("arguments", {})}
            checkpoint = Checkpoint(
                checkpoint_id="uh_ckpt_" + task_id, task_id=task_id,  # 二审 N13：per-task 稳定 id
                state=state,
            )
            self.store.save(checkpoint)
        state = dict(checkpoint.state)
        completed = list(checkpoint.completed_nodes)
        event_id = checkpoint.last_event_id
        timeout_ms = int(deadline_ms or self.default_deadline_ms)
        if self.backend == "langgraph-adapter":
            return await self._run_langgraph(task_id, state, checkpoint, completed, event_id, emit=emit, timeout_ms=timeout_ms)
        for index, node_id in enumerate(self.NODE_ORDER):
            if node_id in completed:
                continue
            node = self.nodes.get(node_id)
            if node is None:
                raise OrchestrationError(module_error_code(), f"编排节点不存在: {node_id}", detail={"module": "orchestration", "node_id": node_id})
            step_id = f"{task_id}:{node_id}"
            started = time.monotonic()
            event_id = (await self._emit(emit, "step.started", {"step_id": step_id, "module": "langgraph", "name": node_id, "index": index})) or (event_id + 1)
            try:
                result = node(state)
                if inspect.isawaitable(result):
                    result = await asyncio.wait_for(result, timeout=timeout_ms / 1000.0)
                if result:
                    state.update(result)
                completed.append(node_id)
                checkpoint.state = state
                checkpoint.completed_nodes = completed
                checkpoint.next_node = self.NODE_ORDER[index + 1] if index + 1 < len(self.NODE_ORDER) else None
                checkpoint.last_event_id = event_id
                checkpoint.status = "completed" if checkpoint.next_node is None else "running"
                checkpoint.updated_at = _now()
                pass  # checkpoint_id 保持 per-task 稳定，不每次重生成（二审 N13）
                self.store.save(checkpoint)
                await self._emit(emit, "step.finished", {"step_id": step_id, "module": "langgraph", "status": "done", "duration_ms": int((time.monotonic() - started) * 1000)})
            except asyncio.TimeoutError as exc:
                checkpoint.state = state
                checkpoint.completed_nodes = completed
                checkpoint.next_node = node_id
                checkpoint.last_event_id = event_id
                checkpoint.status = "interrupted"
                checkpoint.updated_at = _now()
                pass  # checkpoint_id 保持 per-task 稳定，不每次重生成（二审 N13）
                self.store.save(checkpoint)
                await self._emit(emit, "step.failed", {"step_id": step_id, "module": "langgraph", "error": {"code": ORCHESTRATION_TIMEOUT, "message": f"编排节点 {node_id} 超时", "retryable": True, "detail": {"module": "orchestration", "node_id": node_id, "timeout_ms": timeout_ms}}})
                raise OrchestrationError(ORCHESTRATION_TIMEOUT, f"编排节点 {node_id} 超时", retryable=True, detail={"module": "orchestration", "node_id": node_id, "timeout_ms": timeout_ms}) from exc
            except asyncio.CancelledError:
                checkpoint.state = state
                checkpoint.completed_nodes = completed
                checkpoint.next_node = node_id
                checkpoint.last_event_id = event_id
                checkpoint.status = "interrupted"
                checkpoint.updated_at = _now()
                pass  # checkpoint_id 保持 per-task 稳定，不每次重生成（二审 N13）
                self.store.save(checkpoint)
                raise
            except Exception as exc:
                checkpoint.state = state
                checkpoint.completed_nodes = completed
                checkpoint.next_node = node_id
                checkpoint.last_event_id = event_id
                checkpoint.status = "interrupted"
                checkpoint.updated_at = _now()
                pass  # checkpoint_id 保持 per-task 稳定，不每次重生成（二审 N13）
                self.store.save(checkpoint)
                if isinstance(exc, OrchestrationError):
                    await self._emit(emit, "step.failed", {"step_id": step_id, "module": "langgraph", "error": exc.to_dict()})
                    raise
                await self._emit(emit, "step.failed", {"step_id": step_id, "module": "langgraph", "error": {"code": module_error_code(), "message": str(exc), "retryable": False, "detail": {"module": "orchestration", "node_id": node_id}}})
                raise OrchestrationError(module_error_code(), f"编排节点 {node_id} 失败: {exc}", detail={"module": "orchestration", "node_id": node_id}) from exc
        checkpoint.status = "completed"
        checkpoint.next_node = None
        checkpoint.updated_at = _now()
        self.store.save(checkpoint)
        return {"checkpoint_id": checkpoint.checkpoint_id, "state": state, "answer": state.get("answer", ""), "completed_nodes": completed, "last_event_id": checkpoint.last_event_id}

    def checkpoint(self, task_id: str, checkpoint_id: str | None = None) -> Checkpoint | None:
        return self.store.load(task_id, checkpoint_id)

class OrchestrationMCPServer:
    """与 kernel.mcp.MCPHost 兼容的编排 MCP Server（M3 复审 P1）。

    背景：原实现编排只在**内核进程内**实例化，deploy/k8s 与 compose 里
    uh-langgraph 的副本数对业务路径无意义，OOM/HPA 演练证明不了隔离。
    远程模式下 emit 回调无法跨进程传递，故把事件**随结果返回**，由内核侧回放，
    保持任务事件流语义不变。
    """

    def __init__(self, service: OrchestrationService | None = None) -> None:
        if service is None:
            from .llm import create_llm_client

            # 远程进程不持有检索/工具回调：检索走各自的 MCP 服务，
            # 工具调用需回内核（此处留空，用到时明确报不可用而非静默降级）。
            service = OrchestrationService(llm=create_llm_client(), tool_call=None)
        self.service = service

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        method = request.get("method")
        request_id = request.get("id")
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": request_id, "result": {
                "protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                "serverInfo": {"name": "uh-orchestration", "version": "0.1.0"}}}
        if method in {"notifications/initialized", "notifications/cancelled"}:
            return None
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": [
                {"name": "run", "description": "执行编排；事件随结果返回（events 数组）",
                 "inputSchema": {"type": "object",
                   "properties": {"task_id": {"type": "string"}, "content": {"type": "string"},
                     "trace_id": {"type": "string"}, "deadline_ms": {"type": "integer"},
                     "resume": {"type": "boolean"}, "checkpoint_id": {"type": "string"}},
                   "required": ["task_id", "content"]}},
                {"name": "checkpoint", "description": "读取任务 checkpoint",
                 "inputSchema": {"type": "object",
                   "properties": {"task_id": {"type": "string"}, "checkpoint_id": {"type": "string"}},
                   "required": ["task_id"]}},
            ]}}
        if method == "tools/call":
            params = request.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            try:
                if name == "run":
                    events: list[list[Any]] = []

                    async def collect(event: str, payload: dict[str, Any]) -> None:
                        events.append([event, payload])

                    result = _run_async(self.service.run(
                        str(args.get("task_id", "")), str(args.get("content", "")),
                        trace_id=str(args.get("trace_id", "")), emit=collect,
                        deadline_ms=args.get("deadline_ms"),
                        resume=bool(args.get("resume", False)),
                        checkpoint_id=args.get("checkpoint_id"),
                    ))
                    result = {**result, "events": events}
                elif name == "checkpoint":
                    ckpt = self.service.checkpoint(str(args.get("task_id", "")), args.get("checkpoint_id"))
                    result = {"checkpoint": ckpt.to_dict() if ckpt is not None and hasattr(ckpt, "to_dict") else None}
                else:
                    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "unknown tool: " + str(name)}}
                return {"jsonrpc": "2.0", "id": request_id, "result": {
                    "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                    "structuredContent": result, "isError": False, **result}}
            except OrchestrationError as exc:
                return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": exc.message, "data": exc.to_dict()}}
            except Exception as exc:
                return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": str(exc)}}
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "unknown method: " + str(method)}}


def _run_async(coro: Any) -> Any:
    """在同步 MCP handler 中执行协程（无运行中循环则直接 run）。"""
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def create_mcp_app():
    """把编排 JSON-RPC handler 暴露为私有 HTTP 端点（POST /mcp）。"""
    from fastapi import FastAPI, Request, Response

    server = OrchestrationMCPServer()
    app = FastAPI(title="UH! Orchestration MCP")

    @app.post("/mcp")
    async def mcp_endpoint(request: Request) -> Response:
        payload = await request.json()
        response = server.handle(payload)
        if response is None:
            return Response(content="", status_code=202)
        return Response(content=json.dumps(response, ensure_ascii=False),
                        media_type="application/json")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "role": "orchestration"}

    @app.get("/ready")
    async def ready() -> dict[str, Any]:
        return {"status": "ok", "role": "orchestration"}

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_mcp_app(), host="0.0.0.0", port=int(os.environ.get("UH_MCP_PORT", "8080")),
                access_log=False, log_level="warning")
