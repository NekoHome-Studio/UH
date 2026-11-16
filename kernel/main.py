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

from fastapi import FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from . import __version__, tracing
from .logging import get_logger, log_event, setup_logging
from .router import classify
from .memory import create_short_term_memory
from .long_term_memory import create_long_term_memory
from .llm import create_llm_client
from .tasks import TaskManager
from .protocol import EventBus, CapabilityRegistry, StorageGuard, Envelope
from .mcp import MCPHost, InProcessTransport, MCPError
from .retrieval import RetrievalMCPServer
from .orchestration import OrchestrationService, OrchestrationError

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
    context: dict = {}


class ResumeRequest(BaseModel):
    checkpoint_id: str | None = None


def _error_payload(
    code: str,
    message: str,
    *,
    retryable: bool = False,
    retry_after_ms: int = 0,
    detail: dict | None = None,
) -> dict:
    """Build the error shape defined by docs/contracts/errors.md."""
    return {
        "code": code,
        "message": message,
        "retryable": retryable,
        "retry_after_ms": retry_after_ms,
        "detail": dict(detail or {}),
    }


def _envelope(data=None, *, error: dict | None = None, **meta) -> dict:
    """Build a business response; the middleware fills the request trace ID."""
    return {"data": data, "meta": meta, "error": error}


def _error_for_status(status_code: int, message: str = "请求失败", detail: dict | None = None) -> dict:
    mapping = {
        400: ("UH-USR-1003", False),
        401: ("UH-USR-1001", False),
        403: ("UH-USR-1002", False),
        404: ("UH-USR-1003", False),
        429: ("UH-USR-1004", True),
    }
    code, retryable = mapping.get(status_code, ("UH-KRN-5001", False))
    return _error_payload(code, message, retryable=retryable, detail=detail)


def create_app() -> FastAPI:
    app = FastAPI(title="UH! Kernel", version=__version__)
    app.state.idempotency_cache = {}
    app.state.plugins = []
    app.state.memory = create_short_term_memory()
    app.state.long_term_memory = create_long_term_memory()
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
    app.state.orchestration = OrchestrationService(retrieval=app.state.retrieval_server.service, llm=app.state.llm)

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

    async def _orchestration_cap(req):
        try:
            result = await app.state.orchestration.run(
                req.payload.get("task_id", ""), req.payload.get("content", ""), trace_id=req.trace_id,
                emit=req.payload.get("emit"), deadline_ms=req.deadline_ms,
                resume=bool(req.payload.get("resume", False)), checkpoint_id=req.payload.get("checkpoint_id"),
            )
            return Envelope(action=req.action, payload=result, trace_id=req.trace_id, from_=req.to, to=req.from_)
        except OrchestrationError as exc:
            return Envelope(action=req.action, payload={"degraded": True, "reason": "orchestration error", "error_code": exc.code, "error": exc.to_dict()}, trace_id=req.trace_id, from_=req.to, to=req.from_)

    app.state.rpc.register("llm.generate", _llm_cap)
    app.state.rpc.register("retrieval.search", _retrieval_cap, storage={"retrieval"})
    app.state.rpc.register("orchestration.run", _orchestration_cap, storage={"orchestration"})
    app.state.tasks = TaskManager(app.state.llm, app.state.memory, app.state.rpc, app.state.orchestration, app.state.long_term_memory)

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

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        fields = []
        for item in exc.errors():
            loc = ".".join(str(part) for part in item.get("loc", ()) if part != "body")
            fields.append({"field": loc or "request", "reason": item.get("msg", "invalid value")})
        return Response(
            content=json.dumps(_envelope(
                error=_error_payload("UH-USR-1003", "请求参数校验失败", detail={"fields": fields}),
            ), ensure_ascii=False),
            status_code=400,
            media_type="application/json",
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception):
        log.exception("unhandled business exception", exc_info=exc)
        return Response(
            content=json.dumps(_envelope(
                error=_error_payload("UH-KRN-5001", "内核未捕获异常"),
            ), ensure_ascii=False),
            status_code=500,
            media_type="application/json",
        )

    @app.middleware("http")
    async def trace_middleware(request: Request, call_next):
        trace_id = request.headers.get("x-trace-id") or tracing.new_trace_id()
        with tracing.request_context(trace_id):
            log_event(log, logging.INFO, "request.received", method=request.method, path=request.url.path)
            start = time.perf_counter()
            idempotency_key = request.headers.get("Idempotency-Key") if request.method == "POST" else None
            cache_key = (request.method, request.url.path, idempotency_key) if idempotency_key else None
            is_replay = bool(cache_key and cache_key in app.state.idempotency_cache)
            if is_replay:
                cached = app.state.idempotency_cache[cache_key]
                response = Response(
                    content=cached["body"],
                    status_code=cached["status_code"],
                    headers=cached["headers"],
                    media_type=cached["media_type"],
                )
            else:
                try:
                    response = await call_next(request)
                except Exception as exc:
                    log.exception("unhandled business exception", exc_info=exc)
                    response = Response(
                        content=json.dumps(_envelope(
                            error=_error_payload("UH-KRN-5001", "内核未捕获异常"),
                        ), ensure_ascii=False),
                        status_code=500,
                        media_type="application/json",
                    )
            latency_ms = round((time.perf_counter() - start) * 1000, 2)
            log_event(log, logging.INFO, "request.finished", method=request.method, path=request.url.path, status=response.status_code, latency_ms=latency_ms)
            content_type = response.headers.get("content-type", "")
            is_ops = request.url.path in {"/health", "/ready", "/metrics"}
            is_stream = "text/event-stream" in content_type or request.url.path.endswith("/stream")
            # This hidden development probe predates the public API envelope and
            # is kept raw for the existing router regression suite.
            is_legacy_dev_probe = request.url.path == "/api/v1/dev/route" and response.status_code < 400
            if not is_replay and not is_ops and not is_stream and not is_legacy_dev_probe and "application/json" in content_type:
                body = getattr(response, "body", None)
                if body is None and hasattr(response, "body_iterator"):
                    chunks = [chunk async for chunk in response.body_iterator]
                    body = b"".join(chunks)
                try:
                    payload = json.loads(body or b"null")
                except (TypeError, json.JSONDecodeError):
                    payload = None
                if isinstance(payload, dict) and {"data", "meta", "error"}.issubset(payload):
                    normalized = dict(payload)
                    normalized["meta"] = dict(payload.get("meta") or {})
                    normalized["meta"]["trace_id"] = tracing.get_trace_id() or trace_id
                    if normalized["error"] is not None:
                        normalized["error"] = _error_payload(
                            normalized["error"].get("code", "UH-KRN-5001"),
                            normalized["error"].get("message", "请求失败"),
                            retryable=bool(normalized["error"].get("retryable", False)),
                            retry_after_ms=int(normalized["error"].get("retry_after_ms") or 0),
                            detail=normalized["error"].get("detail") or {},
                        )
                elif response.status_code >= 400:
                    message = payload.get("detail", "请求失败") if isinstance(payload, dict) else "请求失败"
                    normalized = _envelope(error=_error_for_status(response.status_code, str(message)))
                    normalized["meta"]["trace_id"] = tracing.get_trace_id() or trace_id
                else:
                    normalized = _envelope(payload)
                    normalized["meta"]["trace_id"] = tracing.get_trace_id() or trace_id
                body = json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                headers = {
                    key: value for key, value in response.headers.items()
                    if key.lower() not in {"content-length", "transfer-encoding", "content-type"}
                }
                response = Response(content=body, status_code=response.status_code, headers=headers, media_type="application/json")
                if cache_key:
                    app.state.idempotency_cache[cache_key] = {
                        "body": body,
                        "status_code": response.status_code,
                        "headers": {**dict(response.headers), "X-Trace-Id": trace_id},
                        "media_type": "application/json",
                    }
            if not is_replay:
                response.headers["X-Trace-Id"] = trace_id
            return response

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": __version__}

    @app.get("/metrics", response_class=Response)
    async def metrics():
        short_term_keys = await app.state.memory.key_count()
        body = (
            "# TYPE uh_memory_short_term_keys gauge\n"
            f"uh_memory_short_term_keys {short_term_keys}\n"
            "# TYPE uh_memory_long_term_entries gauge\n"
            f"uh_memory_long_term_entries {app.state.long_term_memory.entry_count}\n"
        )
        return Response(content=body, media_type="text/plain")

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

    @app.get("/memory/short-term")
    @app.get("/api/v1/memory/short-term")
    async def get_short_term_memory(session_id: str):
        entries = await app.state.memory.get_context(session_id)
        return _envelope({"session_id": session_id, "entries": entries})

    @app.get("/api/v1/sessions/{session_id}/messages")
    async def list_messages(
        session_id: str,
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
    ):
        entries = await app.state.memory.get_context(session_id)
        total = len(entries)
        start = (page - 1) * page_size
        messages = [
            {
                "message_id": f"{session_id}_{start + index + 1}",
                "role": entry.get("role", "user"),
                "content": entry.get("content", ""),
                "created_at": datetime.fromtimestamp(float(entry.get("ts", time.time())), timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            }
            for index, entry in enumerate(entries[start:start + page_size])
        ]
        return _envelope({"messages": messages, "total": total, "has_more": start + page_size < total})

    @app.get("/plugins")
    @app.get("/api/v1/plugins")
    async def list_plugins(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
        total = len(app.state.plugins)
        start = (page - 1) * page_size
        plugins = app.state.plugins[start:start + page_size]
        return _envelope({"plugins": plugins, "total": total, "has_more": start + page_size < total})

    @app.delete("/memory/long-term")
    @app.delete("/api/v1/memory/long-term")
    async def purge_long_term_memory(before: str | None = None):
        try:
            count = app.state.long_term_memory.purge(before)
        except (TypeError, ValueError) as exc:
            return Response(content=json.dumps(_envelope(error=_error_payload("UH-USR-1003", str(exc))), ensure_ascii=False), status_code=400, media_type="application/json")
        return _envelope({"purged_count": count, "before": before})

    @app.get("/memory/export")
    @app.get("/api/v1/memory/export")
    async def export_long_term_memory(request: Request, include_sensitive: bool = False):
        authorized = False
        if include_sensitive:
            expected = os.environ.get("UH_MEMORY_EXPORT_TOKEN")
            authorized = bool(expected and request.headers.get("X-UH-Memory-Export-Authorization") == expected)
            if not authorized:
                return Response(content=json.dumps(_envelope(error=_error_payload("UH-USR-1002", "需要长期记忆导出授权")), ensure_ascii=False), status_code=403, media_type="application/json")
        return _envelope({"entries": app.state.long_term_memory.export(authorize_sensitive=authorized)})

    @app.post("/api/v1/dev/route", include_in_schema=False)
    async def dev_route(req: RouteRequest):
        # 开发/测试端点：M0-3 messages 端点落地后移除；不在公开 OpenAPI 契约内
        try:
            d = classify(req.content, req.mode)
        except ValueError:
            return Response(
                content=json.dumps(_envelope(error=_error_payload("UH-USR-1003", "invalid mode")), ensure_ascii=False),
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
        return _envelope({"session_id": sid, "status": "active", "created_at": now})

    @app.post("/api/v1/sessions/{session_id}/messages", status_code=202)
    async def send_message(session_id: str, req: MessageRequest):
        task_id, trace_id = app.state.tasks.create(session_id, req.content, req.mode, req.context)
        return _envelope({"task_id": task_id, "stream_url": "/api/v1/tasks/" + task_id + "/stream", "trace_id": trace_id})

    @app.get("/api/v1/tasks/{task_id}")
    async def get_task(task_id: str):
        st = app.state.tasks.status(task_id)
        if st is None:
            return Response(content=json.dumps(_envelope(error=_error_payload("UH-USR-1003", "task not found")), ensure_ascii=False), status_code=404, media_type="application/json")
        return _envelope(st)

    @app.post("/api/v1/tasks/{task_id}/resume")
    async def resume_task(task_id: str, req: ResumeRequest | None = None):
        result = app.state.tasks.resume(task_id, req.checkpoint_id if req else None)
        if result is None:
            return Response(content=json.dumps(_envelope(error=_error_payload("UH-USR-1003", "task or checkpoint not found")), ensure_ascii=False), status_code=404, media_type="application/json")
        return _envelope(result)

    @app.post("/api/v1/dev/capability/{name}/toggle")
    async def toggle_cap(name: str, available: bool = True):
        app.state.rpc.set_available(name, available)
        return _envelope({"name": name, "available": app.state.rpc.is_available(name)})

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
