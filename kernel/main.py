"""内核 HTTP 服务骨架（M0-1）：请求接收 + trace_id + 统一日志 + /health /ready。

路由（M0-2）、LLM 封装（M0-3）等后续里程碑在此骨架上扩展。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from . import __version__, otel, tracing
from .logging import get_logger, log_event, setup_logging
from .router import classify
from .memory import create_short_term_memory
from .long_term_memory import create_long_term_memory
from .llm import create_llm_client
from .metrics import CONTENT_TYPE, KernelMetrics, MetricsMiddleware
from .tasks import TaskManager
from .protocol import EventBus, CapabilityRegistry, StorageGuard, Envelope
from .mcp import MCPHost, InProcessTransport, MCPError
from .retrieval import RetrievalMCPServer
from .orchestration import OrchestrationService, OrchestrationError
from .confirmation import ConfirmationManager, ConfirmationError
from .plugins import PluginDriver, PluginError
from .sandbox import SandboxRunner
from .scaler import CrewAIScaler
from .collaboration import CollaborationManager, CrewAIWorker
from .hooks import HookManager, HookPluginLoader
from .themes import clear_uninstalled_theme, installed_theme, select_theme, theme_asset, theme_catalog

_UI = Path(__file__).resolve().parent.parent / "ui" / "index.html"
_UI_STATE_MACHINE = Path(__file__).resolve().parent.parent / "ui" / "state-machine.js"
_UI_THEMES = _UI.with_name("themes.js")
_UI_APP = _UI.with_name("app.js")
_DESKTOP_UI_HEADERS = {
    "X-UH-Desktop": "1",
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "connect-src 'self' http://127.0.0.1:8080 ws://127.0.0.1:8080 ipc: http://ipc.localhost; "
        "object-src 'none'; base-uri 'none'; frame-src 'none'; frame-ancestors 'none'"
    ),
}

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


class ConfirmRequest(BaseModel):
    confirmation_token: str
    decision: str
    operator: str = "user"


class ToolTaskRequest(BaseModel):
    session_id: str
    tool_name: str
    arguments: dict = {}
    risk_level: str = "irreversible"


class InstallRequest(BaseModel):
    source: str
    no_verify: bool = False


class PluginConfigRequest(BaseModel):
    config: dict = {}


class ThemeSelection(BaseModel):
    name: str | None


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


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _dev_enabled() -> bool:
    # 二审 N6/R5：dev 默认关闭，跟随 UH_LOCAL_MODE（本地开/生产关）；UH_DEV_MODE 显式覆盖。
    explicit = os.environ.get("UH_DEV_MODE")
    if explicit is not None:
        return _truthy(explicit)
    return _truthy(os.environ.get("UH_LOCAL_MODE"))


def _read_auth_token() -> str | None:
    """四审 P0-1：密钥经 env 或 token 文件引用，禁硬编码进仓库/命令行。
    优先 UH_AUTH_TOKEN env；否则读 UH_AUTH_TOKEN_FILE 指向的文件（服务器侧，不入仓库）。"""
    tok = os.environ.get("UH_AUTH_TOKEN") or os.environ.get("UH_WS_AUTH_TOKEN") or os.environ.get("UH_WS_TOKEN")
    if tok:
        return tok
    path = os.environ.get("UH_AUTH_TOKEN_FILE")
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip() or None
        except OSError:
            return None
    return None


def _read_client_identity_secret() -> bytes:
    """Return a server-only key used to bind client identities.

    ``client_id`` is transported by browsers in an HttpOnly cookie and by SDKs
    in an explicit header/query parameter.  The value is signed so a caller
    cannot replace its identity with another task owner's raw session ID.
    """
    raw = os.environ.get("UH_CLIENT_ID_SECRET")
    if raw:
        return hashlib.sha256(raw.encode("utf-8")).digest()
    path = os.environ.get("UH_CLIENT_ID_SECRET_FILE")
    if path:
        try:
            value = Path(path).read_bytes().strip()
            if value:
                return hashlib.sha256(value).digest()
        except OSError:
            pass
    # M3-9 多进程：进程本地随机密钥会让 worker A 签发的 client_id 在 worker B
    # 验签失败（403）。多 worker 部署必须提供稳定密钥；此处从共享 Redis 派生，
    # 使同一集群的 worker 自动一致（首个 worker 写入，其余读取）。
    if int(os.environ.get("UH_WORKERS", "1")) > 1:
        derived = _derive_shared_identity_secret()
        if derived is not None:
            return derived
        raise RuntimeError(
            "多 worker 部署需要稳定的 UH_CLIENT_ID_SECRET（或 UH_CLIENT_ID_SECRET_FILE / 可用 UH_REDIS_URL）"
        )
    return secrets.token_bytes(32)


def _derive_shared_identity_secret() -> bytes | None:
    """从 Redis 取/建集群共享身份密钥（SETNX 保证只有一个 worker 生成）。"""
    url = os.environ.get("UH_REDIS_URL")
    if not url:
        return None
    try:
        import redis

        client = redis.Redis.from_url(url, socket_connect_timeout=3, socket_timeout=3)
        key = "uh:cluster:client_identity_secret"
        client.setnx(key, secrets.token_hex(32))
        raw = client.get(key)
        if raw:
            value = raw.decode() if isinstance(raw, bytes) else str(raw)
            return hashlib.sha256(value.encode("utf-8")).digest()
    except Exception:
        return None
    return None


def _issue_client_identity(client_id: str, secret: bytes) -> str:
    payload = client_id.encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    signature = hmac.new(secret, payload, hashlib.sha256).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return f"uhcid1.{encoded}.{encoded_signature}"


def _verify_client_identity(value: str | None, secret: bytes, *, strict: bool) -> str | None:
    if not value:
        return None
    raw = str(value).strip()
    if not raw.startswith("uhcid1."):
        return None if strict else raw
    parts = raw.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        supplied = base64.urlsafe_b64decode(parts[2] + "=" * (-len(parts[2]) % 4))
    except (ValueError, UnicodeEncodeError):
        return None
    expected = hmac.new(secret, payload, hashlib.sha256).digest()
    if not hmac.compare_digest(supplied, expected):
        return None
    try:
        client_id = payload.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return client_id or None


def _strict_client_identity() -> bool:
    return _truthy(os.environ.get("UH_AUTH_REQUIRED")) and not _truthy(os.environ.get("UH_LOCAL_MODE"))


def _bearer_token(value: str | None) -> str | None:
    if not value:
        return None
    scheme, _, token = value.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else value.strip() or None


def create_app() -> FastAPI:
    app = FastAPI(title="UH! Kernel", version=__version__)
    trace_runtime = otel.configure()
    app.state.scaler = CrewAIScaler()
    app.state.idempotency_cache = {}
    # M3-9 多进程：会话归属外置 Redis（无 Redis 回退进程内，单进程语义不变）
    from .session_registry import create_session_registry

    # registry 在 memory 创建后绑定（见下方），此处占位保证属性存在
    app.state.sessions = set()  # 三审 P1-6：本地快取（共享视图以 registry 为准）
    app.state.session_owners = {}
    app.state.session_meta = {}  # 独立审查 P2-4：getSession 需 created_at/status
    app.state.idempotency_locks = {}
    app.state.bus = EventBus()
    app.state.hooks = HookManager(app.state.bus)
    app.state.client_identity_secret = _read_client_identity_secret()
    app.state.memory = create_short_term_memory()
    # M3-9 多进程：会话归属外置（复用 memory 的 Redis 连接；fakeredis/无 Redis 回退进程内）
    app.state.session_registry = create_session_registry(app.state.memory)
    app.state.guard = StorageGuard()
    app.state.guard.declare("retrieval.search", {"retrieval"})  # 提前声明，供检索存储运行时归属校验
    app.state.guard.declare("memory.long_term", {"long_term"})  # 三审 N9：长期记忆命名空间
    app.state.long_term_memory = create_long_term_memory(guard=app.state.guard)
    app.state.llm = create_llm_client()
    telemetry = KernelMetrics(app.state.llm.model, app.state.bus)
    app.state.rpc = CapabilityRegistry(app.state.bus, app.state.guard, observe_call=telemetry.observe_capability)
    sandbox_runner = SandboxRunner()
    app.state.plugin_driver = PluginDriver(
        sandbox_runner=sandbox_runner,
        loaders={"hook": HookPluginLoader(app.state.hooks, sandbox_runner)},
        capability_registry=app.state.rpc,
        observe_crash=telemetry.observe_plugin_crash,
    )
    # Compatibility snapshot for callers that inspected the M1 placeholder.
    app.state.plugins = app.state.plugin_driver.list()
    app.state.retrieval_server = RetrievalMCPServer(guard=app.state.guard)
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

    async def _workflow_tool(name, arguments, *, trace_id, task_id):
        host = app.state.plugin_driver.tool_host(name)
        if host is None:
            raise OrchestrationError("UH-USR-1003", "插件工具不可用", detail={"module": "tool", "tool_name": name})
        response = await app.state.rpc.call(
            f"mcp.tool.{name}", {"arguments": arguments, "task_id": task_id}, trace_id=trace_id,
            deadline_ms=host.tool_timeout_ms(name) + 1000,
        )
        return response.payload

    app.state.orchestration = OrchestrationService(retrieval=app.state.retrieval_server.service, llm=app.state.llm, tool_call=_workflow_tool)

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
                tool=req.payload.get("tool"),
            )
            return Envelope(action=req.action, payload=result, trace_id=req.trace_id, from_=req.to, to=req.from_)
        except OrchestrationError as exc:
            return Envelope(action=req.action, payload={"degraded": True, "reason": "orchestration error", "error_code": exc.code, "error": exc.to_dict()}, trace_id=req.trace_id, from_=req.to, to=req.from_)

    app.state.rpc.register("llm.generate", _llm_cap)
    app.state.rpc.register("retrieval.search", _retrieval_cap, storage={"retrieval"})
    app.state.rpc.register("orchestration.run", _orchestration_cap, storage={"orchestration"})
    app.state.collaboration = CollaborationManager(
        scaler=None if os.getenv("UH_COLLABORATION_MODE") == "compose" else app.state.scaler,
        bus=app.state.bus,
        redis=app.state.memory.redis,
        worker=CrewAIWorker(os.getenv("UH_CREWAI_URL", "http://uh-crewai:8080")),
    )
    app.state.rpc.register("collaboration.run", app.state.collaboration.run, storage={"crewai"})
    app.state.confirmations = ConfirmationManager()
    app.state.tasks = TaskManager(app.state.llm, app.state.memory, app.state.rpc, app.state.orchestration, app.state.long_term_memory, app.state.confirmations, bus=app.state.bus, plugin_host=lambda name: app.state.plugin_driver.tool_host(name))

    def _request_client_id(request: Request) -> str | None:
        strict = _strict_client_identity()
        # A signed explicit header wins over a stale browser cookie. Invalid
        # candidates are skipped so a bad SDK header cannot hide a valid cookie.
        candidates = (
            request.headers.get("X-UH-Client-ID"),
            request.cookies.get("uh_client_id"),
            request.query_params.get("client_id"),
        )
        for candidate in candidates:
            resolved = _verify_client_identity(candidate, app.state.client_identity_secret, strict=strict)
            if resolved:
                return resolved
        return None

    def _websocket_client_id(websocket: WebSocket, *, message: dict | None = None) -> str | None:
        strict = _strict_client_identity()
        candidates = (
            (message or {}).get("client_id"),
            websocket.headers.get("X-UH-Client-ID"),
            websocket.cookies.get("uh_client_id"),
            websocket.query_params.get("client_id"),
        )
        for candidate in candidates:
            resolved = _verify_client_identity(candidate, app.state.client_identity_secret, strict=strict)
            if resolved:
                return resolved
        return None

    def _client_cookie_value(client_id: str) -> str:
        return _issue_client_identity(client_id, app.state.client_identity_secret)

    @app.on_event("startup")
    async def configure_executor() -> None:
        """M3-9：同步 CPU handler 走 asyncio.to_thread，默认 executor 上限
        min(32, cpu+4) 在 100 并发下成为排队瓶颈。按 UH_THREAD_POOL_SIZE 放大。
        """
        import concurrent.futures

        default = max(32, (os.cpu_count() or 4) * 4)
        size = int(os.environ.get("UH_THREAD_POOL_SIZE", str(default)))
        if size > 0:
            loop = asyncio.get_running_loop()
            loop.set_default_executor(
                concurrent.futures.ThreadPoolExecutor(max_workers=size, thread_name_prefix="uh-cap")
            )
            log_event(log, logging.INFO, "kernel.executor.configured", workers=size)

    @app.on_event("startup")
    async def start_capabilities() -> None:
        # MCP tools 通过 Host 注册到能力目录；主链路的兼容别名由上面的
        # retrieval.search handler 提供，以保持 M1-1 TaskManager 契约不变。
        try:
            await app.state.retrieval_host.register_capabilities(app.state.rpc)
            app.state.retrieval_ready = True
        except Exception as exc:
            app.state.retrieval_ready = False
            log_event(log, logging.WARNING, "capability.module.degraded", module="retrieval", reason=str(exc))
        await app.state.plugin_driver.restore_active()
        app.state.plugins = app.state.plugin_driver.list()
        app.state.collaboration.start()

    @app.on_event("shutdown")
    async def stop_capabilities() -> None:
        try:
            await app.state.collaboration.close()
            await app.state.plugin_driver.shutdown()
            await app.state.hooks.shutdown()
            await app.state.retrieval_host.close()
        finally:
            if trace_runtime is not None:
                await trace_runtime.shutdown()

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
        if trace_runtime is None:
            trace_id = request.headers.get("x-trace-id") or tracing.new_trace_id()
            scope = tracing.request_context(trace_id)
        else:
            trace_id = tracing.get_trace_id()
            scope = tracing.request_context(trace_id, span_id=tracing.get_span_id())
        with scope:
            log_event(log, logging.INFO, "request.received", method=request.method, path=request.url.path)
            start = time.perf_counter()
            # REST bearerAuth（审查 B5，对齐 openapi bearerAuth）：本地单用户模式（UH_LOCAL_MODE 默认 1）豁免；
            # 生产设 UH_LOCAL_MODE=0 + UH_AUTH_REQUIRED=1 + UH_AUTH_TOKEN 启用。ops 端点不在 /api/v1 下，天然豁免。
            # 二审 N1/N2 修复：默认值与 WS 对齐（UH_LOCAL_MODE 无默认，单设 UH_AUTH_REQUIRED=1 即生效）；
            # 鉴权覆盖所有业务端点含遗留别名（/memory/*、/plugins），仅 ops 与 UI 静态豁免。
            auth_required = _truthy(os.environ.get("UH_AUTH_REQUIRED")) and not _truthy(os.environ.get("UH_LOCAL_MODE"))
            _exempt = request.url.path in {
                "/health", "/ready", "/metrics", "/",
                "/static/state-machine.js", "/static/themes.js", "/static/app.js",
                "/docs", "/openapi.json", "/redoc",
            }
            if auth_required and not _exempt:
                expected = _read_auth_token()
                # 与 WS 一致：Bearer 头优先，亦接受 ?token= 查询参数（本地/测试便利）。
                supplied = _bearer_token(request.headers.get("Authorization")) or request.query_params.get("token")
                if not supplied or expected is None or not secrets.compare_digest(supplied, expected):
                    _resp = _envelope(error=_error_payload("UH-USR-1001", "未认证"))
                    _resp["meta"]["trace_id"] = trace_id  # 二审 R2：401 也必带 meta.trace_id
                    return Response(
                        content=json.dumps(_resp, ensure_ascii=False),
                        status_code=401, media_type="application/json", headers={"X-Trace-Id": trace_id},
                    )
            idempotency_key = request.headers.get("Idempotency-Key") if request.method == "POST" else None
            cache_key = None
            if idempotency_key:
                # 审查修复：幂等键绑定请求体哈希——同键不同 body 不得错误复用首次结果
                req_body = await request.body()
                body_hash = hashlib.sha256(req_body).hexdigest()[:16]
                # 二审 N5：多用户（鉴权）模式绑调用者身份防跨客户端复用；本地单用户模式不绑（cookie 中途设置会破坏同客户端回放）
                caller = (_request_client_id(request) or request.query_params.get("token") or "") if auth_required else ""  # 五审 N4：统一身份解析，避免 header/cookie 优先级漂移
                cache_key = (request.method, request.url.path, idempotency_key, body_hash, caller)
            _idem_lock = app.state.idempotency_locks.setdefault(cache_key, asyncio.Lock()) if cache_key else None
            if _idem_lock is not None:
                await _idem_lock.acquire()
            try:
                is_replay = bool(cache_key and cache_key in app.state.idempotency_cache)
                if is_replay:
                    cached = app.state.idempotency_cache[cache_key]
                    request.state.task_type = cached.get("task_type")
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
                        # 二审 N5：缓存头剔除 Set-Cookie，防回放泄漏首次调用者 client_id
                        cached_headers = {k: v for k, v in {**dict(response.headers), "X-Trace-Id": trace_id}.items() if k.lower() != "set-cookie"}
                        app.state.idempotency_cache[cache_key] = {
                            "body": body,
                            "status_code": response.status_code,
                            "headers": cached_headers,
                            "media_type": "application/json",
                            "task_type": getattr(request.state, "task_type", None),
                            "stored_at": time.time(),
                        }
                        # 审查修复：TTL + 容量上限，防进程内 dict 单调增长
                        now = time.time()
                        for k in [k for k, v in app.state.idempotency_cache.items() if now - v.get("stored_at", now) > 300]:
                            app.state.idempotency_cache.pop(k, None)
                        while len(app.state.idempotency_cache) > 1000:
                            app.state.idempotency_cache.pop(next(iter(app.state.idempotency_cache)), None)
                        # 三审 P2-1：锁表随缓存回收，防单调增长
                        for k in list(app.state.idempotency_locks):
                            if k not in app.state.idempotency_cache:
                                app.state.idempotency_locks.pop(k, None)
                if not is_replay:
                    response.headers["X-Trace-Id"] = trace_id
                return response
            finally:
                if _idem_lock is not None:
                    _idem_lock.release()

    if trace_runtime is not None:
        app.add_middleware(otel.TracingMiddleware, runtime=trace_runtime, routes=app.routes)

    # Wrap the tracing middleware as it can respond before routing.
    app.add_middleware(MetricsMiddleware, metrics=telemetry, routes=app.routes)

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": __version__}

    @app.get("/api/v1/bench/baseline", include_in_schema=False)
    async def bench_baseline():
        """独立审查 P1-2：压测需**服务端**硬件基线才能声称数字可比。

        原 fetch_server_baseline 只取 /health 与 /metrics，从不采集 CPU/内存/平台，
        导致 RELEASE 的「双端硬件基线」承诺不成立。此端点补齐服务端侧数据。
        非契约端点（include_in_schema=False），仅供 uh bench 采集。
        """
        from .bench import hardware_baseline

        return _envelope(hardware_baseline())

    @app.get("/metrics", response_class=Response)
    async def metrics():
        body = telemetry.render(
            short_term_keys=await app.state.memory.key_count(),
            long_term_memory=app.state.long_term_memory,
            plugin_records=app.state.plugin_driver.records.values(),
            token_usage=app.state.llm.uh_token_usage_total,
        )
        return Response(content=body, headers={"Content-Type": CONTENT_TYPE})

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
        all_plugins = app.state.plugin_driver.list()
        total = len(all_plugins)
        start = (page - 1) * page_size
        plugins = all_plugins[start:start + page_size]
        return _envelope({"plugins": plugins, "total": total, "has_more": start + page_size < total})

    def _plugin_error(exc: PluginError) -> Response:
        return Response(
            content=json.dumps(_envelope(error=exc.to_dict()), ensure_ascii=False),
            status_code=exc.status_code,
            media_type="application/json",
        )

    @app.post("/plugins", status_code=201)
    @app.post("/api/v1/plugins", status_code=201)
    async def install_plugin(req: InstallRequest):
        try:
            result = await app.state.plugin_driver.install(req.source, no_verify=req.no_verify)
        except PluginError as exc:
            return _plugin_error(exc)
        app.state.plugins = app.state.plugin_driver.list()
        return _envelope(result)

    @app.delete("/plugins/{name}")
    @app.delete("/api/v1/plugins/{name}")
    async def uninstall_plugin(name: str):
        driver = app.state.plugin_driver
        try:
            result = await driver.uninstall(name)
        except PluginError as exc:
            return _plugin_error(exc)
        app.state.plugins = driver.list()
        clear_uninstalled_theme(driver, name)
        return _envelope(result)

    @app.patch("/plugins/{name}/config")
    @app.patch("/api/v1/plugins/{name}/config")
    async def update_plugin_config(name: str, req: PluginConfigRequest | None = None):
        try:
            result = await app.state.plugin_driver.update_config(name, req.config if req else {})
        except PluginError as exc:
            return _plugin_error(exc)
        app.state.plugins = app.state.plugin_driver.list()
        return _envelope(result)

    @app.get("/api/v1/themes")
    async def list_themes():
        return _envelope(theme_catalog(app.state.plugin_driver))

    @app.put("/api/v1/themes/active")
    async def apply_theme(req: ThemeSelection):
        driver = app.state.plugin_driver
        try:
            select_theme(driver, req.name)
        except (KeyError, ValueError):
            return _plugin_error(PluginError("UH-USR-1003", "主题不存在或不可用", status_code=404))
        return _envelope({"active": req.name})

    @app.get("/api/v1/themes/{name}/assets/{asset:path}")
    async def get_theme_asset(name: str, asset: str):
        try:
            record = installed_theme(app.state.plugin_driver, name)
            content, mime = theme_asset(record, "assets/" + asset)
        except (KeyError, ValueError, OSError):
            return _plugin_error(PluginError("UH-USR-1003", "主题资源不存在或不可用", status_code=404))
        return Response(content, media_type=mime, headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})

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
    async def dev_route(req: RouteRequest, request: Request):
        if not _dev_enabled():  # 二审 N6：dev_route 补门禁
            return Response(content=json.dumps(_envelope(error=_error_payload("UH-USR-1003", "not found")), ensure_ascii=False), status_code=404, media_type="application/json")
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
        request.state.task_type = d.task_type
        return {"task_type": d.task_type, "mode": d.mode, "rule": d.rule, "confidence": d.confidence, "fallback": d.fallback}

    @app.get("/")
    async def index(request: Request):
        headers = _DESKTOP_UI_HEADERS if request.query_params.get("desktop") == "1" else None
        return FileResponse(_UI, headers=headers)

    @app.get("/static/state-machine.js", include_in_schema=False)
    async def state_machine_asset():
        return FileResponse(_UI_STATE_MACHINE, media_type="application/javascript")

    @app.get("/static/themes.js", include_in_schema=False)
    async def themes_asset():
        return FileResponse(_UI_THEMES, media_type="application/javascript")

    @app.get("/static/app.js", include_in_schema=False)
    async def app_asset():
        return FileResponse(_UI_APP, media_type="application/javascript")

    @app.post("/api/v1/sessions", status_code=201)
    async def create_session(response: Response, request: Request):
        sid = "uh_ses_" + secrets.token_hex(16)
        now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        # Local single-user mode keeps the historical session-scoped identity.
        # Production mode may reuse an existing signed principal across sessions.
        client_id = (_request_client_id(request) if _strict_client_identity() else None) or sid
        app.state.sessions.add(sid)
        app.state.session_owners[sid] = client_id
        # 独立审查 P2-4：契约声明 getSession/closeSession 但实现缺失。
        # 记录会话元数据供 GET /sessions/{id} 返回（Session schema 要求 created_at/status）。
        app.state.session_meta[sid] = {"created_at": now, "status": "active"}
        await app.state.session_registry.register(sid, client_id)
        response.set_cookie("uh_client_id", _client_cookie_value(client_id), httponly=True, samesite="lax")
        return _envelope({"session_id": sid, "client_id": _client_cookie_value(client_id), "status": "active", "created_at": now})

    def _session_guard(request: Request, session_id: str):
        """会话归属校验，与 send_message 同口径（双因子：token + client_id）。"""
        if not (_truthy(os.environ.get("UH_AUTH_REQUIRED")) and not _truthy(os.environ.get("UH_LOCAL_MODE"))):
            return None
        owner = app.state.session_owners.get(session_id)
        if owner is None:
            return None
        client_id = _request_client_id(request)
        if client_id is not None and client_id != owner:
            return Response(content=json.dumps(_envelope(error=_error_payload(
                "UH-USR-1002", "无权访问会话", detail={"resource": "session", "session_id": session_id})),
                ensure_ascii=False), status_code=403, media_type="application/json")
        return None

    @app.get("/api/v1/sessions/{session_id}")
    async def get_session(session_id: str, request: Request):
        """契约 openapi.yaml getSession：获取会话详情。"""
        meta = app.state.session_meta.get(session_id)
        if meta is None and not await app.state.session_registry.exists(session_id):
            return Response(content=json.dumps(_envelope(error=_error_payload(
                "UH-USR-1003", "会话不存在", detail={"resource": "session", "session_id": session_id})),
                ensure_ascii=False), status_code=404, media_type="application/json")
        denied = _session_guard(request, session_id)
        if denied is not None:
            return denied
        meta = meta or {}
        return _envelope({
            "session_id": session_id,
            "status": meta.get("status", "active"),
            "created_at": meta.get("created_at", datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")),
        })

    @app.delete("/api/v1/sessions/{session_id}")
    async def close_session(session_id: str, request: Request):
        """契约 openapi.yaml closeSession：关闭会话并清理 L0 短期记忆。"""
        meta = app.state.session_meta.get(session_id)
        if meta is None and not await app.state.session_registry.exists(session_id):
            return Response(content=json.dumps(_envelope(error=_error_payload(
                "UH-USR-1003", "会话不存在", detail={"resource": "session", "session_id": session_id})),
                ensure_ascii=False), status_code=404, media_type="application/json")
        denied = _session_guard(request, session_id)
        if denied is not None:
            return denied
        await app.state.memory.clear(session_id)
        if meta is not None:
            meta["status"] = "closed"
        log_event(log, logging.INFO, "kernel.session.closed", session_id=session_id)
        return _envelope({"session_id": session_id, "status": "closed"})

    @app.post("/api/v1/sessions/{session_id}/messages", status_code=202)
    async def send_message(session_id: str, req: MessageRequest, request: Request, response: Response):
        # 三审 P1-6：会话存在性校验（契约要求不存在会话 USR-1003）
        # M3-9 多进程：本地快取未命中时查共享注册表（别的 worker 建的会话）
        if session_id not in app.state.sessions and not await app.state.session_registry.exists(session_id):
            return Response(content=json.dumps(_envelope(error=_error_payload("UH-USR-1003", "会话不存在", detail={"resource": "session", "session_id": session_id})), ensure_ascii=False), status_code=404, media_type="application/json")
        strict_identity = _strict_client_identity()
        client_id = _request_client_id(request)
        if client_id is None and strict_identity:
            return Response(content=json.dumps(_envelope(error=_error_payload("UH-USR-1002", "需要有效的 client_id 身份凭据")), ensure_ascii=False), status_code=403, media_type="application/json")
        client_id = client_id or session_id
        session_owner = app.state.session_owners.get(session_id) or await app.state.session_registry.owner(session_id)
        if strict_identity and session_owner and client_id != session_owner:
            return Response(content=json.dumps(_envelope(error=_error_payload("UH-USR-1002", "无权访问会话", detail={"resource": "session", "session_id": session_id})), ensure_ascii=False), status_code=403, media_type="application/json")
        owner_token = _bearer_token(request.headers.get("Authorization")) or request.query_params.get("token")  # query token 也设归属，与 WS 一致
        task_id, trace_id = app.state.tasks.create(
            session_id,
            req.content,
            req.mode,
            req.context,
            owner_client_id=client_id,
            owner_token=owner_token,
        )
        request.state.task_type = app.state.tasks.tasks.get(task_id, {}).get("task_type")
        # Keep the local client identity available to the subsequent browser
        # WebSocket handshake when the session was supplied directly.
        response.set_cookie("uh_client_id", _client_cookie_value(client_id), httponly=True, samesite="lax")
        # 审查修复：stream_url 按契约 task-flow §1 返回 WS 句柄（F2 按 scheme 区分）；
        # sse_url 保留 M0 SSE 过渡通道。
        # 二审 N4 修复：不信任请求 Host 头（防注入）；用可信 UH_PUBLIC_BASE_URL，否则相对路径。
        public_base = os.environ.get("UH_PUBLIC_BASE_URL", "").rstrip("/")
        # 三审 P1-1：可信 base 的 http(s) 转 ws(s)，符 task-flow §1 wss 句柄
        if public_base.startswith("https://"):
            public_base = "wss://" + public_base[len("https://"):]
        elif public_base.startswith("http://"):
            public_base = "ws://" + public_base[len("http://"):]
        stream_url = (public_base + "/api/v1/stream?task_id=" + task_id) if public_base else ("/api/v1/stream?task_id=" + task_id)
        sse_url = "/api/v1/tasks/" + task_id + "/stream"
        return _envelope({"task_id": task_id, "stream_url": stream_url, "sse_url": sse_url, "trace_id": trace_id})

    def _task_guard(request: Request, task_id: str):
        # 二审 GPT-6 P1：多用户模式校验任务归属；本地单用户豁免。
        if not (_truthy(os.environ.get("UH_AUTH_REQUIRED")) and not _truthy(os.environ.get("UH_LOCAL_MODE"))):
            return None
        if app.state.tasks.status(task_id) is None:
            return None
        token = _bearer_token(request.headers.get("Authorization")) or request.query_params.get("token")
        client_id = _request_client_id(request)
        if not app.state.tasks.owner_matches(task_id, client_id=client_id, token=token):
            return Response(content=json.dumps(_envelope(error=_error_payload("UH-USR-1002", "无权访问任务", detail={"resource": "task", "task_id": task_id})), ensure_ascii=False), status_code=403, media_type="application/json")
        return None

    @app.get("/api/v1/tasks/{task_id}")
    async def get_task(task_id: str, request: Request):
        g = _task_guard(request, task_id)
        if g is not None:
            return g
        st = app.state.tasks.status(task_id)
        if st is None:
            return Response(content=json.dumps(_envelope(error=_error_payload("UH-USR-1003", "task not found")), ensure_ascii=False), status_code=404, media_type="application/json")
        return _envelope(st)

    @app.post("/api/v1/tasks/{task_id}/resume")
    async def resume_task(task_id: str, req: ResumeRequest | None = None, request: Request = None):
        g = _task_guard(request, task_id)
        if g is not None:
            return g
        from .tasks import ResumeConflictError
        try:
            result = app.state.tasks.resume(task_id, req.checkpoint_id if req else None)
        except ResumeConflictError as exc:
            return Response(content=json.dumps(_envelope(error=_error_payload("UH-USR-1003", str(exc))), ensure_ascii=False), status_code=409, media_type="application/json")
        if result is None:
            return Response(content=json.dumps(_envelope(error=_error_payload("UH-USR-1003", "task or checkpoint not found")), ensure_ascii=False), status_code=404, media_type="application/json")
        return _envelope(result)

    @app.post("/api/v1/tasks/{task_id}/confirm")
    async def confirm_task(task_id: str, req: ConfirmRequest, request: Request):
        g = _task_guard(request, task_id)
        if g is not None:
            return g
        if app.state.tasks.status(task_id) is None:
            return Response(content=json.dumps(_envelope(error=_error_payload("UH-USR-1003", "task not found")), ensure_ascii=False), status_code=404, media_type="application/json")
        try:
            resolved = app.state.tasks.resolve_confirmation(task_id, req.confirmation_token, req.decision, operator=req.operator)
        except ConfirmationError as exc:
            status = 403 if exc.code == "UH-USR-1002" else 400
            return Response(content=json.dumps(_envelope(error=_error_payload(exc.code, exc.message, retryable=exc.retryable, detail=exc.detail)), ensure_ascii=False), status_code=status, media_type="application/json")
        return _envelope({"call_id": resolved.call_id, "decision": resolved.decision, "operator": resolved.operator})

    @app.get("/api/v1/tasks/{task_id}/steps")
    async def task_steps(task_id: str, request: Request):
        g = _task_guard(request, task_id)
        if g is not None:
            return g
        steps = app.state.tasks.steps(task_id)
        if steps is None:
            return Response(content=json.dumps(_envelope(error=_error_payload("UH-USR-1003", "task not found")), ensure_ascii=False), status_code=404, media_type="application/json")
        return _envelope({"task_id": task_id, "steps": steps})  # 二审 R3：补 task_id 符 openapi

    @app.post("/api/v1/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str, request: Request):
        g = _task_guard(request, task_id)
        if g is not None:
            return g
        from .tasks import ResumeConflictError
        try:
            result = await app.state.tasks.cancel(task_id, reason="user_request")
        except ResumeConflictError as exc:
            return Response(content=json.dumps(_envelope(error=_error_payload("UH-USR-1003", str(exc))), ensure_ascii=False), status_code=409, media_type="application/json")
        if result is None:
            return Response(content=json.dumps(_envelope(error=_error_payload("UH-USR-1003", "task not found")), ensure_ascii=False), status_code=404, media_type="application/json")
        return _envelope({**result, "status": "cancelling"})  # 二审 R3：补 status 符 openapi CancelResult

    @app.post("/api/v1/dev/tool-task", status_code=202)
    async def dev_tool_task(req: ToolTaskRequest, request: Request):
        if not _dev_enabled():
            return Response(content=json.dumps(_envelope(error=_error_payload("UH-USR-1003", "not found")), ensure_ascii=False), status_code=404, media_type="application/json")
        # 与 send_message 一致：设归属（query token 也认），供 confirm/WS 归属校验
        strict_identity = _strict_client_identity()
        client_id = _request_client_id(request)
        if client_id is None and strict_identity:
            return Response(content=json.dumps(_envelope(error=_error_payload("UH-USR-1002", "需要有效的 client_id 身份凭据")), ensure_ascii=False), status_code=403, media_type="application/json")
        client_id = client_id or req.session_id
        session_owner = app.state.session_owners.get(req.session_id) or await app.state.session_registry.owner(req.session_id)
        if strict_identity and session_owner and client_id != session_owner:
            return Response(content=json.dumps(_envelope(error=_error_payload("UH-USR-1002", "无权访问会话", detail={"resource": "session", "session_id": req.session_id})), ensure_ascii=False), status_code=403, media_type="application/json")
        owner_token = _bearer_token(request.headers.get("Authorization")) or request.query_params.get("token")
        task_id, trace_id = app.state.tasks.create_tool_task(req.session_id, req.tool_name, req.arguments, req.risk_level, app.state.retrieval_host, owner_client_id=client_id, owner_token=owner_token)
        request.state.task_type = "tool"
        return _envelope({"task_id": task_id, "trace_id": trace_id, "sse_url": "/api/v1/tasks/" + task_id + "/stream"})

    @app.get("/api/v1/dev/confirmations")
    async def dev_confirmations(task_id: str | None = None):
        if not _dev_enabled():
            return Response(content=json.dumps(_envelope(error=_error_payload("UH-USR-1003", "not found")), ensure_ascii=False), status_code=404, media_type="application/json")
        reqs = app.state.confirmations.pending_for_task(task_id) if task_id else list(app.state.confirmations._requests.values())
        # 安全修复：不再向 HTTP 泄漏完整确认令牌（令牌仅经任务自有事件流送达归属者）
        items = []
        for r in reqs:
            d = r.to_dict()
            tok = d.get("confirmation_token") or ""
            d["confirmation_token"] = "[redacted]" if tok else None  # 三审：全脱敏，不留前缀
            items.append(d)
        return _envelope({"confirmations": items, "audit": app.state.confirmations.audit})

    @app.post("/api/v1/dev/capability/{name}/toggle")
    async def toggle_cap(name: str, available: bool = True):
        if not _dev_enabled():
            return Response(content=json.dumps(_envelope(error=_error_payload("UH-USR-1003", "not found")), ensure_ascii=False), status_code=404, media_type="application/json")
        app.state.rpc.set_available(name, available)
        return _envelope({"name": name, "available": app.state.rpc.is_available(name)})

    @app.get("/api/v1/tasks/{task_id}/stream")
    async def task_stream(task_id: str, request: Request):
        g = _task_guard(request, task_id)  # 四审 P0-2：SSE 补归属守卫（此前唯一漏的任务端点）
        if g is not None:
            return g
        if app.state.tasks.status(task_id) is None:
            return Response(content=json.dumps(_envelope(error=_error_payload("UH-USR-1003", "task not found")), ensure_ascii=False), status_code=404, media_type="application/json")
        async def gen():
            async for ev in app.state.tasks.stream(task_id):
                yield "data: " + json.dumps(ev, ensure_ascii=False) + "\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.websocket("/api/v1/stream")
    async def websocket_stream(websocket: WebSocket):
        """Unified task event channel with replay and terminal delivery."""
        await websocket.accept()
        params = websocket.query_params
        query_task_id = params.get("task_id")
        if not query_task_id:
            await websocket.send_json({"op": "error", "error": _error_payload("UH-USR-1003", "task_id required")})
            await websocket.close(code=4404)
            return

        after_raw = params.get("after_event_id", params.get("last_event_id", "0"))
        try:
            query_after = int(after_raw or 0)
            if query_after < 0:
                raise ValueError
        except (TypeError, ValueError):
            await websocket.send_json({"op": "error", "error": _error_payload("UH-USR-1003", "invalid after_event_id")})
            await websocket.close(code=4404)
            return

        expected_token = _read_auth_token()
        auth_required = _truthy(os.environ.get("UH_WS_AUTH_REQUIRED") or os.environ.get("UH_AUTH_REQUIRED")) and not _truthy(os.environ.get("UH_LOCAL_MODE"))
        token = _bearer_token(params.get("token")) or _bearer_token(websocket.headers.get("Authorization"))
        authenticated = not auth_required
        # 安全修复：token 仅在配置了期望值且匹配时才有效；未配期望 token 时
        # 任意 token 不得放行（修复空 token 绕过）。
        if token and expected_token is not None and secrets.compare_digest(token, expected_token):
            authenticated = True
        elif token and auth_required:
            await websocket.send_json({"op": "error", "error": _error_payload("UH-USR-1001", "未认证")})
            await websocket.close(code=4401)
            return

        client_id = _websocket_client_id(websocket)
        subscriber = None
        active_task_id = None

        async def close_error(code: str, message: str, close_code: int, *, task_id: str | None = None) -> None:
            detail = {"resource": "task", "task_id": task_id} if code == "UH-USR-1002" and task_id else None
            await websocket.send_json({"op": "error", "error": _error_payload(code, message, detail=detail)})
            await websocket.close(code=close_code)

        async def attach(task_id: str, after_event_id: int):
            nonlocal subscriber, active_task_id
            if app.state.tasks.status(task_id) is None:
                await close_error("UH-USR-1003", "task not found", 4404, task_id=task_id)
                return None, True
            if not app.state.tasks.owner_matches(task_id, client_id=client_id, token=token):
                await close_error("UH-USR-1002", "无权订阅任务", 4403, task_id=task_id)
                return None, True
            if subscriber is not None and active_task_id:
                app.state.tasks.unsubscribe(active_task_id, subscriber)
            result = app.state.tasks.subscribe(task_id, after_event_id=after_event_id)
            if result is None:
                await close_error("UH-USR-1003", "task not found", 4404, task_id=task_id)
                return None, True
            new_subscriber, replay, terminal = result
            if not app.state.tasks.replay_available(task_id, after_event_id=after_event_id):
                app.state.tasks.unsubscribe(task_id, new_subscriber)
                await websocket.send_json({"op": "replay_unavailable", "reason": "window_expired"})
                await websocket.close(code=4408)
                return None, True
            subscriber = new_subscriber
            active_task_id = task_id
            for event in replay:
                await websocket.send_json(event)
            if terminal:
                app.state.tasks.unsubscribe(task_id, subscriber)
                await websocket.close(code=1000)
                return None, True
            return subscriber, False

        if authenticated:
            subscriber, closed = await attach(query_task_id, query_after)
            if closed:
                return

        receive_task = asyncio.create_task(websocket.receive_json())
        queue_task = asyncio.create_task(subscriber.get()) if subscriber is not None else None
        try:
            while True:
                pending = {receive_task}
                if queue_task is not None:
                    pending.add(queue_task)
                done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                if receive_task in done:
                    try:
                        message = receive_task.result()
                    except WebSocketDisconnect:
                        break
                    if not isinstance(message, dict):
                        receive_task = asyncio.create_task(websocket.receive_json())
                        continue
                    op = message.get("op")
                    if op == "ping":
                        await websocket.send_json({"op": "pong", "ts": message.get("ts", int(time.time()))})
                    elif op == "auth":
                        supplied = _bearer_token(message.get("token"))
                        if not supplied or expected_token is None or not secrets.compare_digest(supplied, expected_token):
                            await close_error("UH-USR-1001", "未认证", 4401)
                            return
                        token = supplied
                        authenticated = True
                        client_id = _websocket_client_id(websocket, message=message) or client_id
                        # 二审 N8：换 token 后重校已订阅任务归属，防 token B 续收任务 A 事件
                        if subscriber is not None and active_task_id and not app.state.tasks.owner_matches(active_task_id, client_id=client_id, token=token):
                            await close_error("UH-USR-1002", "无权订阅任务", 4403, task_id=active_task_id)
                            return
                        if subscriber is None:
                            subscriber, closed = await attach(query_task_id, query_after)
                            if closed:
                                return
                            # 修复：鉴权后重建队列读取任务，否则只回放不推实时事件
                            if subscriber is not None:
                                queue_task = asyncio.create_task(subscriber.get())
                    elif op == "subscribe":
                        if auth_required and not authenticated:
                            await close_error("UH-USR-1001", "未认证", 4401)
                            return
                        target = message.get("task_id") or query_task_id
                        client_id = _websocket_client_id(websocket, message=message) or client_id
                        try:
                            cursor = int(message.get("after_event_id", message.get("last_event_id", 0)))
                            if cursor < 0:
                                raise ValueError
                        except (TypeError, ValueError):
                            await close_error("UH-USR-1003", "invalid after_event_id", 4404, task_id=target)
                            return
                        # Query-based subscriptions are attached immediately
                        # in local mode.  Treat the matching explicit frame as
                        # an acknowledgement so the replay is not duplicated.
                        if not (subscriber is not None and target == active_task_id and cursor == query_after):
                            if queue_task is not None:
                                queue_task.cancel()
                                queue_task = None
                            subscriber, closed = await attach(target, cursor)
                            if closed:
                                return
                            # 修复：重订阅后重建队列读取任务，否则实时事件永久丢失
                            if subscriber is not None:
                                queue_task = asyncio.create_task(subscriber.get())
                    receive_task = asyncio.create_task(websocket.receive_json())
                if queue_task is not None and queue_task in done:
                    event = queue_task.result()
                    if event.get("event") in TaskManager.TERMINAL_EVENTS:
                        await websocket.send_json(event)
                        app.state.tasks.unsubscribe(active_task_id, subscriber)
                        await websocket.close(code=1000)
                        return
                    await websocket.send_json(event)
                    queue_task = asyncio.create_task(subscriber.get())
        finally:
            if not receive_task.done():
                receive_task.cancel()
            if queue_task is not None:
                queue_task.cancel()
            if subscriber is not None and active_task_id:
                app.state.tasks.unsubscribe(active_task_id, subscriber)

    return app


app = create_app()
