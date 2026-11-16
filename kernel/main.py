"""内核 HTTP 服务骨架（M0-1）：请求接收 + trace_id + 统一日志 + /health /ready。

路由（M0-2）、LLM 封装（M0-3）等后续里程碑在此骨架上扩展。
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from . import __version__, tracing
from .logging import get_logger, log_event, setup_logging
from .router import classify
from .memory import create_short_term_memory
from .llm import create_llm_client
from .tasks import TaskManager
from .protocol import EventBus, CapabilityRegistry, StorageGuard, Envelope
from .mcp import MCPHost, InProcessTransport, MCPError
from .retrieval import RetrievalMCPServer

_UI = Path(__file__).resolve().parent.parent / "ui" / "index.html"

setup_logging()
log = get_logger("kernel.http")


class RouteRequest(BaseModel):
    """Request body for the development-only routing endpoint."""

    content: str
    mode: str = "auto"


class MessageRequest(BaseModel):
    """Request body for POST /sessions/{id}/messages."""

    content: str
    mode: str = "auto"


def create_app() -> FastAPI:
    app = FastAPI(title="UH! Kernel", version=__version__)
    app.state.memory = create_short_term_memory()
    app.state.llm = create_llm_client()
    app.state.bus = EventBus()
    app.state.guard = StorageGuard()
    app.state.rpc = CapabilityRegistry(app.state.bus, app.state.guard)
    app.state.retrieval_server = RetrievalMCPServer()
    if os.environ.get("UH_RETRIEVAL_MCP_COMMAND"):
        app.state.retrieval_host = MCPHost.from_stdio(os.environ["UH_RETRIEVAL_MCP_COMMAND"], bus=app.state.bus)
    elif os.environ.get("UH_RETRIEVAL_MCP_URL"):
        app.state.retrieval_host = MCPHost.from_http(os.environ["UH_RETRIEVAL_MCP_URL"], bus=app.state.bus)
    else:
        app.state.retrieval_host = MCPHost(InProcessTransport(app.state.retrieval_server.handle), bus=app.state.bus)
    # 开发环境保留一条最小内置文档，维持 M0 search 演示可用；用户文档仍由
    # index_document 工具写入。其内容不承担生产知识库职责。
    try:
        if not app.state.retrieval_server.service.store.documents:
            app.state.retrieval_server.service.index_document(
                content="UH! MCP 协议与检索能力的开发演示文档。",
                source="builtin:uh-demo.md", title="UH! MCP demo",
            )
    except Exception:
        pass

    async def _llm_cap(req):
        msgs = req.payload.get("messages") or [{"role": "user", "content": req.payload.get("content", "")}]
        content = await app.state.llm.chat(msgs)
        return Envelope(action=req.action, payload={"content": content}, trace_id=req.trace_id, from_=req.to, to=req.from_)

    async def _retrieval_cap(req):
        try:
            result = await app.state.retrieval_host.call_tool(
                "search", {"query": req.payload.get("query", req.payload.get("q", "")), "top_k": req.payload.get("top_k", 5)},
                trace_id=req.trace_id,
            )
            payload = result.get("structuredContent") if isinstance(result.get("structuredContent"), dict) else result
            return Envelope(action=req.action, payload=payload, trace_id=req.trace_id, from_=req.to, to=req.from_)
        except MCPError as exc:
            return Envelope(
                action=req.action,
                payload={"degraded": True, "reason": "retrieval unavailable", "error_code": exc.code, "error": exc.to_dict()},
                trace_id=req.trace_id, from_=req.to, to=req.from_,
            )

    app.state.rpc.register("llm.generate", _llm_cap)
    app.state.rpc.register("retrieval.search", _retrieval_cap, storage={"retrieval"})
    app.state.tasks = TaskManager(app.state.llm, app.state.memory, app.state.rpc)

    @app.on_event("startup")
    async def register_retrieval_tools() -> None:
        # MCP tools 通过 Host 注册到能力目录；主链路的兼容别名由上面的
        # retrieval.search handler 提供，以保持 M1-1 TaskManager 契约不变。
        try:
            await app.state.retrieval_host.register_capabilities(app.state.rpc)
            app.state.retrieval_ready = True
        except Exception as exc:
            app.state.retrieval_ready = False
            log_event(log, logging.WARNING, "capability.module.degraded", module="retrieval", reason=str(exc))

    @app.middleware("http")
    async def trace_middleware(request: Request, call_next):
        trace_id = request.headers.get("x-trace-id") or tracing.new_trace_id()
        with tracing.request_context(trace_id):
            log_event(log, logging.INFO, "request.received", method=request.method, path=request.url.path)
            start = time.perf_counter()
            response: Response = await call_next(request)
            latency_ms = round((time.perf_counter() - start) * 1000, 2)
            log_event(log, logging.INFO, "request.finished", method=request.method, path=request.url.path, status=response.status_code, latency_ms=latency_ms)
            response.headers["X-Trace-Id"] = trace_id
            return response

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": __version__}

    @app.get("/ready")
    async def ready():
        checks = {}
        try:
            await app.state.memory.key_count()
            checks["memory"] = "ok"
        except Exception:
            checks["memory"] = "error"
        status = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
        return {"status": status, "version": __version__, "checks": checks}

    @app.post("/api/v1/dev/route", include_in_schema=False)
    async def dev_route(req: RouteRequest):
        # 开发/测试端点：M0-3 messages 端点落地后移除；不在公开 OpenAPI 契约内
        try:
            d = classify(req.content, req.mode)
        except ValueError:
            return Response(
                content='{"data":null,"meta":{},"error":{"code":"UH-USR-1003","message":"invalid mode","retryable":false}}',
                status_code=400, media_type="application/json")
        log_event(log, logging.INFO, "kernel.route.decided", task_type=d.task_type, mode=d.mode, rule=d.rule, confidence=d.confidence)
        if d.fallback:
            log_event(log, logging.WARNING, "route.fallback", reason="unrecognized", task_type="chat")
        return {"task_type": d.task_type, "mode": d.mode, "rule": d.rule, "confidence": d.confidence, "fallback": d.fallback}

    @app.get("/")
    async def index():
        return FileResponse(_UI)

    @app.post("/api/v1/sessions", status_code=201)
    async def create_session():
        sid = "uh_ses_" + secrets.token_hex(16)
        now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        return {"data": {"session_id": sid, "status": "active", "created_at": now}, "meta": {"trace_id": tracing.new_trace_id()}, "error": None}

    @app.post("/api/v1/sessions/{session_id}/messages", status_code=202)
    async def send_message(session_id: str, req: MessageRequest):
        task_id, trace_id = app.state.tasks.create(session_id, req.content, req.mode)
        return {"data": {"task_id": task_id, "stream_url": "/api/v1/tasks/" + task_id + "/stream", "trace_id": trace_id}, "meta": {"trace_id": trace_id}, "error": None}

    @app.get("/api/v1/tasks/{task_id}")
    async def get_task(task_id: str):
        st = app.state.tasks.status(task_id)
        if st is None:
            return Response(content=json.dumps({"data": None, "meta": {}, "error": {"code": "UH-USR-1003", "message": "task not found", "retryable": False}}), status_code=404, media_type="application/json")
        return {"data": st, "meta": {}, "error": None}

    @app.post("/api/v1/dev/capability/{name}/toggle")
    async def toggle_cap(name: str, available: bool = True):
        app.state.rpc.set_available(name, available)
        return {"data": {"name": name, "available": app.state.rpc.is_available(name)}, "meta": {}, "error": None}

    @app.get("/api/v1/tasks/{task_id}/stream")
    async def task_stream(task_id: str):
        async def gen():
            try:
                async for ev in app.state.tasks.stream(task_id):
                    yield "data: " + json.dumps(ev, ensure_ascii=False) + "\n\n"
            except KeyError:
                yield "data: " + json.dumps({"event": "error", "payload": {"error": {"code": "UH-USR-1003", "message": "task not found"}}}) + "\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


app = create_app()
