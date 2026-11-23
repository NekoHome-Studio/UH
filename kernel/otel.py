"""Optional OpenTelemetry bridge for UH trace IDs, spans and W3C propagation."""
from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import re
from contextlib import contextmanager
from functools import wraps
from threading import Lock
from typing import TYPE_CHECKING

from . import __version__, tracing

if TYPE_CHECKING:
    from opentelemetry.context import Context
    from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
    from opentelemetry.sdk.trace.export import SpanProcessor

_CURRENT: contextvars.ContextVar[_Trace | None] = contextvars.ContextVar("uh_otel", default=None)
_ROOT_ID: contextvars.ContextVar[int] = contextvars.ContextVar("uh_otel_root_id", default=0)
_UH_TRACE_ID = re.compile(r"uh_trc_([0-9a-f]{32})")
_LOG = logging.getLogger(__name__)


def trace_id_to_otel(value: str) -> int:
    match = _UH_TRACE_ID.fullmatch(value)
    return int(match[1], 16) if match else 0


def trace_id_from_otel(value: int) -> str:
    return f"uh_trc_{value:032x}"


def span_id_from_otel(value: int) -> str:
    return f"sp_{value & 0xffffffff:08x}"


class _Trace:
    def __init__(self, runtime: Otel, sampled: bool):
        self.runtime = runtime
        self.sampled = sampled
        self._keep = sampled or runtime.collector
        self._lock = Lock()
        self._owners = 1
        self._pending: list[ReadableSpan] = []

    def reserve(self) -> None:
        with self._lock:
            self._owners += 1

    def release(self) -> None:
        with self._lock:
            self._owners -= 1
            if self._owners == 0:
                self._pending.clear()

    def on_end(self, span: ReadableSpan) -> None:
        from opentelemetry.trace import StatusCode

        with self._lock:
            if self._keep:
                self.runtime.export.on_end(span)
                return
            self._pending.append(span)
            if span.status.status_code is StatusCode.ERROR:
                self._keep = True
                for queued in self._pending:
                    self.runtime.export.on_end(queued)
                self._pending.clear()


def _export_processor(endpoint: str, protocol: str) -> SpanProcessor:
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    if protocol == "http/protobuf":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        url = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or endpoint.rstrip("/") + "/v1/traces"
        return BatchSpanProcessor(OTLPSpanExporter(endpoint=url))
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

    return BatchSpanProcessor(OTLPSpanExporter(endpoint=os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or endpoint))


def configure(service_name: str = "uh-kernel") -> Otel | None:
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if os.environ.get("UH_OTEL_ENABLED", "").strip().lower() not in {"1", "true", "yes", "on"} or not endpoint:
        return None
    try:
        from opentelemetry.sdk.trace import TracerProvider  # noqa: F401
    except ImportError:
        _LOG.warning("OpenTelemetry unavailable; install the otel extra to enable tracing")
        return None
    protocol = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL") or os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    if protocol not in {"grpc", "http/protobuf"}:
        raise ValueError("OTLP trace protocol must be grpc or http/protobuf")
    ratio = float(os.environ.get("UH_OTEL_SAMPLE_RATIO", "1.0"))
    if not 0 <= ratio <= 1:
        raise ValueError("UH_OTEL_SAMPLE_RATIO must be between 0 and 1")
    mode = os.environ.get("UH_OTEL_TAIL_SAMPLING", "local")
    if mode not in {"local", "collector"}:
        raise ValueError("UH_OTEL_TAIL_SAMPLING must be local or collector")
    try:
        return Otel(_export_processor(endpoint, protocol), ratio, service_name, collector=mode == "collector")
    except ImportError:
        _LOG.warning("OpenTelemetry unavailable; install the otel extra to enable tracing")
        return None


class Otel:
    def __init__(self, export: SpanProcessor, ratio: float, service_name: str = "uh-kernel", *, collector: bool = False):
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
        from opentelemetry.sdk.trace.id_generator import RandomIdGenerator
        from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased, TraceIdRatioBased
        from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

        self.export = export
        self.collector = collector
        self.sampler = ParentBased(TraceIdRatioBased(ratio))
        self.propagator = TraceContextTextMapPropagator()

        class IdGenerator(RandomIdGenerator):
            def generate_trace_id(self) -> int:
                return _ROOT_ID.get() or super().generate_trace_id()

        class TailProcessor(SpanProcessor):
            def on_end(self, span: ReadableSpan) -> None:
                current = _CURRENT.get()
                if current is not None:
                    current.on_end(span)

            def shutdown(self) -> None:
                export.shutdown()

            def force_flush(self, timeout_millis: int = 30_000) -> bool:
                return export.force_flush(timeout_millis)

        self.provider: TracerProvider = TracerProvider(
            resource=Resource.create({"service.name": service_name, "service.version": __version__}),
            # Keep SDK recording on so a later error can retain earlier spans.
            # The ParentBased decision controls export and W3C propagation.
            sampler=ALWAYS_ON,
            id_generator=IdGenerator(),
            shutdown_on_exit=False,
        )
        self.provider.add_span_processor(TailProcessor())
        self.tracer = self.provider.get_tracer("uh", __version__)

    @contextmanager
    def operation(self, name: str, *, carrier=None, trace_id: str | None = None, kind: str = "SERVER"):
        from opentelemetry.context import Context
        from opentelemetry.trace import get_current_span

        parent = self.propagator.extract(carrier, context=Context()) if carrier else Context()
        remote = get_current_span(parent).get_span_context()
        root_id = remote.trace_id if remote.is_valid else (
            trace_id_to_otel(trace_id or "") or trace_id_to_otel(tracing.new_trace_id())
        )
        state = _Trace(self, self.sampler.should_sample(parent, root_id, name).decision.is_sampled())
        scope_token = _CURRENT.set(state)
        try:
            with self._span(state, name, kind, parent=parent, root_id=root_id) as span:
                yield span
        finally:
            state.release()
            _CURRENT.reset(scope_token)

    @contextmanager
    def _span(self, state: _Trace, name: str, kind: str, *, parent: Context | None = None, root_id: int = 0):
        from opentelemetry.trace import SpanKind, use_span

        attrs = {"uh.sample_candidate": "true" if state.sampled else "false"} if self.collector else None
        root_token = _ROOT_ID.set(root_id)
        try:
            span = self.tracer.start_span(name, context=parent, kind=SpanKind[kind], attributes=attrs)
        finally:
            _ROOT_ID.reset(root_token)
        ids = span.get_span_context()
        with use_span(span, end_on_exit=True, record_exception=False, set_status_on_exception=False):
            with tracing.request_context(trace_id_from_otel(ids.trace_id), span_id=span_id_from_otel(ids.span_id)):
                try:
                    yield span
                except Exception:
                    mark_error()
                    raise

    async def shutdown(self) -> None:
        await asyncio.to_thread(self.provider.shutdown)


def active() -> bool:
    return _CURRENT.get() is not None


def span(name: str, *, kind: str = "INTERNAL"):
    state = _CURRENT.get()
    return state.runtime._span(state, name, kind)


def instrument_async(name: str, *, kind: str = "INTERNAL"):
    def decorate(func):
        @wraps(func)
        async def wrapped(*args, **kwargs):
            if not active():
                return await func(*args, **kwargs)
            with span(name, kind=kind):
                return await func(*args, **kwargs)

        return wrapped

    return decorate


def mark_error() -> None:
    from opentelemetry.trace import Status, StatusCode, get_current_span

    get_current_span().set_status(Status(StatusCode.ERROR))


def envelope_ids() -> tuple[str, str | None]:
    from opentelemetry.trace import get_current_span

    current = get_current_span()
    return span_id_from_otel(current.get_span_context().span_id), (
        span_id_from_otel(current.parent.span_id) if current.parent is not None else None
    )


def outbound_headers() -> dict[str, str]:
    from opentelemetry.context import Context
    from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, get_current_span, set_span_in_context

    state = _CURRENT.get()
    ids = get_current_span().get_span_context()
    flags = (ids.trace_flags & ~TraceFlags.SAMPLED) | (TraceFlags.SAMPLED if state.sampled else 0)
    propagated = SpanContext(ids.trace_id, ids.span_id, False, TraceFlags(flags), ids.trace_state)
    carrier: dict[str, str] = {}
    state.runtime.propagator.inject(carrier, context=set_span_in_context(NonRecordingSpan(propagated), Context()))
    return carrier


def start_task(coro, trace_id: str, carrier=None) -> asyncio.Task:
    state = _CURRENT.get()
    if trace_id != tracing.get_trace_id():
        async def resume():
            with state.runtime.operation("task.resume", trace_id=trace_id, carrier=carrier, kind="INTERNAL"):
                return await coro

        runner = asyncio.create_task(resume())
        runner.add_done_callback(lambda _: coro.close())
        return runner
    state.reserve()
    runner = asyncio.create_task(coro)
    runner.add_done_callback(lambda _: state.release())
    return runner


class TracingMiddleware:
    def __init__(self, app, *, runtime: Otel, routes, name: str = "http.request"):
        self.app = app
        self.runtime = runtime
        self.routes = routes
        self.name = name

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        from starlette.datastructures import Headers

        from .metrics import route_template

        headers = Headers(scope=scope)
        with self.runtime.operation(self.name, carrier=headers, trace_id=headers.get("x-trace-id")) as root:
            async def observe(message):
                if message["type"] == "http.response.start":
                    status = message["status"]
                    root.set_attribute("http.response.status_code", status)
                    if status >= 400:
                        mark_error()
                await send(message)

            try:
                await self.app(scope, receive, observe)
            finally:
                root.set_attribute("http.route", route_template(scope, self.routes))
