"""Process-local metrics for the kernel, in Prometheus text format 0.0.4."""
from __future__ import annotations

from bisect import bisect_left
from collections import Counter
from dataclasses import dataclass, field
from itertools import accumulate
from threading import Lock
from time import perf_counter
from typing import TYPE_CHECKING

from starlette.routing import BaseRoute, Match, Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .router import TASK_TYPES

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .long_term_memory import LongTermMemory
    from .plugins import PluginRecord
    from .protocol import Envelope, EventBus


CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30)
_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "CONNECT", "TRACE"})
_MODULES = ("llm", "retrieval", "orchestration", "langgraph", "mcp", "other")
_TASK_OUTCOMES = {
    "kernel.route.decided": "created",
    "tool.confirmation.requested": "pending_confirmation",
    "kernel.task.resumed": "resumed",
    "kernel.task.finished": "finished",
    "kernel.task.failed": "failed",
    "kernel.task.cancelled": "cancelled",
}


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


@dataclass(frozen=True)
class Metric:
    name: str
    kind: str
    labels: tuple[str, ...] = ()
    description: str = ""

    def metadata(self) -> tuple[str, str]:
        return f"# HELP {self.name} {self.description}", f"# TYPE {self.name} {self.kind}"

    def sample(self, values: tuple[str, ...], number: int | float, *, suffix: str = "", bucket: str | None = None) -> str:
        labels = list(zip(self.labels, values, strict=True))
        if bucket is not None:
            labels.append(("le", bucket))
        encoded = ",".join(f'{key}="{_escape(value)}"' for key, value in labels)
        return f"{self.name}{suffix}" + ("{" + encoded + "}" if encoded else "") + f" {number}"


REQUESTS = Metric(
    "uh_kernel_requests_total", "counter", ("method", "route", "status_code", "task_type"),
    description="HTTP requests handled by the kernel.",
)
REQUEST_ERRORS = Metric(
    "uh_kernel_request_errors_total", "counter", ("method", "route", "status_code"),
    description="Kernel HTTP requests with a 4xx or 5xx response.",
)
REQUEST_DURATION = Metric(
    "uh_kernel_request_duration_seconds", "histogram", ("method", "route"),
    description="Elapsed seconds from HTTP request entry to response headers.",
)
CAPABILITY_INVOCATIONS = Metric(
    "uh_capability_invocations_total", "counter", ("module",),
    description="Kernel capability RPC calls and task step invocations.",
)
CAPABILITY_FAILURES = Metric(
    "uh_capability_failures_total", "counter", ("module",),
    description="Failed or degraded capability RPC calls and failed task steps.",
)
SHORT_TERM_KEYS = Metric("uh_memory_short_term_keys", "gauge", description="Active short-term session keys.")
LONG_TERM_ENTRIES = Metric("uh_memory_long_term_entries", "gauge", description="Stored long-term entries.")
LONG_TERM_VECTORS = Metric("uh_memory_long_term_vectors", "gauge", description="Indexed long-term memory vectors.")
ACTIVE_PLUGINS = Metric("uh_plugins_active", "gauge", description="Currently active plugins.")
PLUGIN_CRASHES = Metric("uh_plugins_crashes_total", "counter", description="Plugin crashes seen by this kernel.")
CRASH_QUARANTINED_PLUGINS = Metric(
    "uh_plugins_crash_quarantined", "gauge",
    description="Currently quarantined plugins that reached the crash limit.",
)
TOKEN_USAGE = Metric(
    "uh_token_usage_total", "counter", ("model",), description="Prompt and completion tokens reported by the provider.",
)
TASK_TRANSITIONS = Metric(
    "uh_tasks_transitions_total", "counter", ("outcome",), description="Task lifecycle events by outcome.",
)


def route_template(scope: Scope, routes: list[BaseRoute]) -> str:
    matched = scope.get("route")
    if isinstance(matched, Route):
        return matched.path_format
    # Auth responses and idempotency replays can bypass the router.
    partial = None
    for route in routes:
        if isinstance(route, Route):
            match, _ = route.matches(scope)
            if match == Match.FULL:
                return route.path_format
            if match == Match.PARTIAL and partial is None:
                partial = route
    if partial is not None:
        return partial.path_format
    return "unmatched"


@dataclass
class _Histogram:
    buckets: list[int] = field(default_factory=lambda: [0] * (len(_BUCKETS) + 1))
    total_seconds: float = 0.0


class KernelMetrics:
    def __init__(self, model: str, bus: EventBus) -> None:
        self.model = model
        self._lock = Lock()
        self._counters: dict[Metric, Counter[tuple[str, ...]]] = {
            REQUESTS: Counter(),
            REQUEST_ERRORS: Counter(),
            CAPABILITY_INVOCATIONS: Counter({(module,): 0 for module in _MODULES}),
            CAPABILITY_FAILURES: Counter({(module,): 0 for module in _MODULES}),
            PLUGIN_CRASHES: Counter({(): 0}),
            TASK_TRANSITIONS: Counter({(outcome,): 0 for outcome in _TASK_OUTCOMES.values()}),
        }
        self._durations: dict[tuple[str, ...], _Histogram] = {}
        for event in (*_TASK_OUTCOMES, "capability.module.invoked", "capability.module.failed"):
            bus.subscribe(event, self._observe_event)

    def observe_http(self, scope: Scope, routes: list[BaseRoute], status: int, seconds: float) -> None:
        method = scope["method"] if scope["method"] in _METHODS else "OTHER"
        route = route_template(scope, routes)
        status_code = str(status) if 100 <= status <= 599 else "other"
        task_type = scope.get("state", {}).get("task_type")
        if task_type not in TASK_TYPES:
            task_type = "none"
        with self._lock:
            self._counters[REQUESTS][method, route, status_code, task_type] += 1
            if status >= 400:
                self._counters[REQUEST_ERRORS][method, route, status_code] += 1
            key = (method, route)
            if key not in self._durations:
                self._durations[key] = _Histogram()
            duration = self._durations[key]
            duration.buckets[bisect_left(_BUCKETS, seconds)] += 1
            duration.total_seconds += seconds

    def observe_capability(self, action: str, failed: bool) -> None:
        module = action.partition(".")[0]
        labels = (module if module in _MODULES else "other",)
        with self._lock:
            self._counters[CAPABILITY_INVOCATIONS][labels] += 1
            if failed:
                self._counters[CAPABILITY_FAILURES][labels] += 1

    async def _observe_event(self, event: Envelope) -> None:
        outcome = _TASK_OUTCOMES.get(event.action)
        with self._lock:
            if outcome:
                self._counters[TASK_TRANSITIONS][(outcome,)] += 1
            elif "module" in event.payload and not event.payload.get("cancelled"):
                module = event.payload["module"]
                labels = (module if module in _MODULES else "other",)
                metric = CAPABILITY_INVOCATIONS if event.action == "capability.module.invoked" else CAPABILITY_FAILURES
                self._counters[metric][labels] += 1

    def observe_plugin_crash(self) -> None:
        with self._lock:
            self._counters[PLUGIN_CRASHES][()] += 1

    def render(
        self, *, short_term_keys: int, long_term_memory: LongTermMemory,
        plugin_records: Iterable[PluginRecord], token_usage: dict[str, dict[str, int]],
    ) -> str:
        tokens = Counter({(self.model,): 0})
        for model, usage in token_usage.items():
            label = model if model == self.model else "other"
            tokens[(label,)] += usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
        gauges = {
            SHORT_TERM_KEYS: short_term_keys,
            LONG_TERM_ENTRIES: long_term_memory.entry_count,
            LONG_TERM_VECTORS: long_term_memory.vector_count,
            ACTIVE_PLUGINS: sum(record.status == "active" for record in plugin_records),
            CRASH_QUARANTINED_PLUGINS: sum(
                record.status == "quarantined" and record.crash_count >= 3 for record in plugin_records
            ),
        }
        with self._lock:
            lines = []
            for metric, samples in {**self._counters, TOKEN_USAGE: tokens}.items():
                lines.extend(metric.metadata())
                lines.extend(metric.sample(labels, value) for labels, value in sorted(samples.items()))
            lines.extend(REQUEST_DURATION.metadata())
            for labels, duration in sorted(self._durations.items()):
                counts = list(accumulate(duration.buckets))
                for bound, count in zip((*map(str, _BUCKETS), "+Inf"), counts, strict=True):
                    lines.append(REQUEST_DURATION.sample(labels, count, suffix="_bucket", bucket=bound))
                lines.append(REQUEST_DURATION.sample(labels, duration.total_seconds, suffix="_sum"))
                lines.append(REQUEST_DURATION.sample(labels, counts[-1], suffix="_count"))
            for metric, value in gauges.items():
                lines.extend(metric.metadata())
                lines.append(metric.sample((), value))
        return "\n".join(lines) + "\n"


class MetricsMiddleware:
    def __init__(self, app: ASGIApp, metrics: KernelMetrics, routes: list[BaseRoute]) -> None:
        self.app = app
        self.metrics = metrics
        self.routes = routes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = perf_counter()
        sent_headers = False

        async def observe_response(message: Message) -> None:
            nonlocal sent_headers
            if message["type"] == "http.response.start":
                self.metrics.observe_http(scope, self.routes, message["status"], perf_counter() - started)
                sent_headers = True
            await send(message)

        try:
            await self.app(scope, receive, observe_response)
        except Exception:
            if not sent_headers:
                self.metrics.observe_http(scope, self.routes, 500, perf_counter() - started)
            raise
