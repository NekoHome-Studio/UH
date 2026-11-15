"""内核 HTTP 服务骨架（M0-1）：请求接收 + trace_id + 统一日志 + /health /ready。

路由（M0-2）、LLM 封装（M0-3）等后续里程碑在此骨架上扩展。
"""
from __future__ import annotations

import logging
import time

from fastapi import FastAPI, Request, Response
from pydantic import BaseModel

from . import __version__, tracing
from .logging import get_logger, log_event, setup_logging
from .router import classify
from .memory import create_short_term_memory

setup_logging()
log = get_logger("kernel.http")


class RouteRequest(BaseModel):
    """Request body for the development-only routing endpoint."""

    content: str
    mode: str = "auto"


def create_app() -> FastAPI:
    app = FastAPI(title="UH! Kernel", version=__version__)
    app.state.memory = create_short_term_memory()

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

    return app


app = create_app()
