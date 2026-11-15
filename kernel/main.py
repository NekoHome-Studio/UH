"""内核 HTTP 服务骨架（M0-1）：请求接收 + trace_id + 统一日志 + /health /ready。

路由（M0-2）、LLM 封装（M0-3）等后续里程碑在此骨架上扩展。
"""
from __future__ import annotations

import logging
import time

from fastapi import FastAPI, Request, Response

from . import __version__, tracing
from .logging import get_logger, log_event, setup_logging

setup_logging()
log = get_logger("kernel.http")


def create_app() -> FastAPI:
    app = FastAPI(title="UH! Kernel", version=__version__)

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
        # M0 无外部依赖；M0-4 起在此注册 Redis 等就绪检查
        return {"status": "ok", "version": __version__, "checks": {}}

    return app


app = create_app()
