"""M1-9 事件字典运行时实现。

内部总线事件是审计事实，外部事件是 UI/WS 契约。这个模块把两者连接起来，
并在推送前用冻结的 ``events.schema.json`` 做最后一道拒绝校验。映射器同时
保留 span_id、trace_id 和 task_id 索引，供审计从内部事件反查外部事件。
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from . import tracing
from .protocol import Envelope, validate_event_name, module_error_code

try:  # jsonschema 是项目开发/运行时依赖；导入失败时给出明确错误。
    from jsonschema import Draft7Validator
except ImportError:  # pragma: no cover - packaging error, not a normal code path
    Draft7Validator = None  # type: ignore[assignment,misc]


ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "docs" / "contracts" / "events.schema.json"

EXTERNAL_EVENTS = frozenset(
    {
        "task.created",
        "task.finished",
        "task.failed",
        "task.cancelled",
        "task.resumed",
        "step.started",
        "step.finished",
        "step.failed",
        "delta",
        "tool.call",
        "tool.result",
        "task.confirmation_required",
        "task.confirmation_resolved",
        "task.yielded",
        "task.degraded",
        "safety.check_started",
        "safety.check_finished",
        "safety.denied",
    }
)

# Deprecated two-segment names remain accepted for the compatibility window in
# events.md; new internal producers should use the three-segment aliases.
INTERNAL_TO_EXTERNAL: dict[str, str | None] = {
    "kernel.request.received": None,
    "kernel.route.decided": "task.created",
    "capability.invoked": "step.started",
    "capability.module.invoked": "step.started",
    "capability.finished": "step.finished",
    "capability.module.finished": "step.finished",
    "capability.failed": "step.failed",
    "capability.module.failed": "step.failed",
    "memory.write": None,
    "memory.record.write": None,
    "llm.delta": "delta",
    "llm.stream.delta": "delta",
    "mcp.tool.call": "tool.call",
    "mcp.tool.result": "tool.result",
    "tool.confirm": "task.confirmation_required",
    "tool.confirmation.requested": "task.confirmation_required",
    "tool.confirm.resolved": "task.confirmation_resolved",
    "tool.confirmation.resolved": "task.confirmation_resolved",
    "kernel.task.cancelled": "task.cancelled",
    "kernel.task.resumed": "task.resumed",
    "kernel.task.degraded": "task.degraded",
    "kernel.task.finished": "task.finished",
    "kernel.task.failed": "task.failed",
    "safety.check.started": "safety.check_started",
    "safety.check.finished": "safety.check_finished",
    "safety.action.denied": "safety.denied",
    # Historical spelling seen in early P0-3 adapters; kept read-only for the
    # compatibility window while the bus itself remains three-segment strict.
    "safety.check_started": "safety.check_started",
    "safety.check_finished": "safety.check_finished",
    "safety.denied": "safety.denied",
    "cua.yield": "task.yielded",
    "cua.session.yielded": "task.yielded",
    "capability.module.degraded": "task.degraded",
}

_MODULE_ALIASES = {
    "retrieval": "llama_index",
    "retrieval.search": "llama_index",
    "orchestration": "langgraph",
    "tool": "mcp_tool",
    "mcp": "mcp_tool",
}


class EventMappingError(ValueError):
    """拒绝未知事件、非法载荷或不连续的外部 event_id。"""


class UnknownInternalEventError(EventMappingError):
    pass


class EventValidationError(EventMappingError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


_VALIDATOR = Draft7Validator(_schema()) if Draft7Validator is not None else None

# 形状缓存：(事件名, payload 键, 值类型名元组) → 该形状已通过完整校验。
# 为什么需要：map() 在 _emit 热路径上被调用，mock 流式回答按字符发 delta 事件，
# 单个任务会产生数千次全量 JSON Schema 校验（cProfile 实测占单任务 CPU 的 80%）。
# 同一事件名 + 同一 payload 键集合 + 同一值类型，其 schema 判定结果必然相同，
# 故首次校验通过后缓存形状即可，语义不变（值仍受类型约束，仅跳过重复遍历）。
_SHAPE_PASSED: set[tuple[str, tuple[tuple[str, str], ...]]] = set()


def _shape_key(value: Mapping[str, Any]) -> tuple[str, tuple[tuple[str, str], ...]] | None:
    event = value.get("event")
    payload = value.get("payload")
    if not isinstance(event, str) or not isinstance(payload, Mapping):
        return None
    return (
        event,
        tuple(sorted((str(k), type(v).__name__) for k, v in payload.items())),
    )


def validate_external_event(event: Mapping[str, Any], *, previous_event_id: int | None = None) -> dict[str, Any]:
    """校验一个完整外部信封并返回副本。

    ``previous_event_id`` 用于流式推送的无空洞约束；schema 本身负责字段、
    枚举和事件载荷判别。任何失败都统一抛出 :class:`EventValidationError`。
    """
    value = copy.deepcopy(dict(event))
    if _VALIDATOR is None:
        raise EventValidationError("缺少 jsonschema 依赖，无法校验外部事件")
    shape = _shape_key(value)
    if shape is None or shape not in _SHAPE_PASSED:
        errors = sorted(_VALIDATOR.iter_errors(value), key=lambda error: list(error.path))
        if errors:
            first = errors[0]
            path = ".".join(str(p) for p in first.path) or "<root>"
            raise EventValidationError(f"非法外部事件 {path}: {first.message}")
        if shape is not None:
            _SHAPE_PASSED.add(shape)
    if previous_event_id is not None and value["event_id"] != previous_event_id + 1:
        raise EventValidationError(
            f"event_id 必须单调且无空洞: previous={previous_event_id}, current={value['event_id']}"
        )
    return value


def _module(payload: Mapping[str, Any], source: Envelope) -> str:
    raw = payload.get("module") or payload.get("action") or source.from_ or "llm"
    raw_s = str(raw)
    if raw_s.startswith("mcp.tool") or raw_s.startswith("mcp_"):
        return "mcp_tool"
    if raw_s.startswith("retrieval"):
        return "llama_index"
    if raw_s.startswith("orchestration"):
        return "langgraph"
    return _MODULE_ALIASES.get(raw_s, raw_s if raw_s in {"router", "llama_index", "langgraph", "crewai", "mcp_tool", "memory", "llm", "safety"} else "llm")


def _mapped_payload(external: str, source: Envelope) -> dict[str, Any]:
    payload = copy.deepcopy(dict(source.payload or {}))
    if external in {"step.started", "step.finished", "step.failed"}:
        payload["module"] = _module(payload, source)
        payload.setdefault("step_id", str(payload.get("action") or source.span_id or "step"))
        if external == "step.started":
            payload.setdefault("name", str(payload.get("step_id", "step")))
            payload = {key: payload[key] for key in ("step_id", "module", "name", "index") if key in payload}
        elif external == "step.finished":
            payload.setdefault("status", "done")
            payload.setdefault("duration_ms", 0)
            payload = {key: payload[key] for key in ("step_id", "module", "status", "duration_ms") if key in payload}
        elif not isinstance(payload.get("error"), dict):
            message = str(payload.get("error") or "能力模块失败")
            payload["error"] = {"code": module_error_code(), "message": message, "retryable": False}
            payload = {key: payload[key] for key in ("step_id", "module", "error") if key in payload}
        else:
            payload = {key: payload[key] for key in ("step_id", "module", "error") if key in payload}
    elif external == "task.degraded":
        payload = {
            "reason_code": payload.get("reason_code") or payload.get("error_code") or "UH-CAP-5021",
            "module": _module(payload, source),
            "description": payload.get("description") or payload.get("reason") or "能力模块降级",
        }
    elif external == "task.finished":
        result = payload.get("final_result")
        if not isinstance(result, dict):
            payload["final_result"] = {"content": str(payload.get("content", ""))}
        payload.setdefault("duration_ms", 0)
    elif external == "task.failed" and "error" not in payload:
        payload["error"] = {"code": "UH-KRN-5001", "message": "任务失败", "retryable": False}
    elif external == "tool.result":
        # MCP 内部响应可能保留结构化 content；外部契约只允许脱敏字符串，
        # 且内部 error 详情不能穿透事件流。
        preview = payload.get("content_preview")
        if preview is None:
            payload["content_preview"] = ""
        elif not isinstance(preview, str):
            payload["content_preview"] = json.dumps(preview, ensure_ascii=False, separators=(",", ":"))
        encoded = payload["content_preview"].encode("utf-8")
        if len(encoded) > 2048:
            payload["content_preview"] = encoded[:2045].decode("utf-8", "ignore") + "..."
        payload.pop("error", None)
    return payload


def map_internal_event(
    source: Envelope,
    *,
    task_id: str,
    event_id: int,
    ts: str | None = None,
) -> dict[str, Any] | None:
    """将一个内部 Envelope 映射为经 schema 校验的外部事件。

    返回 ``None`` 表示该内部事件是审计专用（例如 memory.write）。未知内部
    事件直接拒绝，避免未登记事件意外进入 UI/WS。
    """
    if source.action not in INTERNAL_TO_EXTERNAL:
        try:
            validate_event_name(source.action)
        except ValueError as exc:
            raise UnknownInternalEventError(str(exc)) from exc
        raise UnknownInternalEventError(f"内部事件未登记: {source.action}")
    external = INTERNAL_TO_EXTERNAL[source.action]
    if external is None:
        return None
    if external not in EXTERNAL_EVENTS:
        raise UnknownInternalEventError(f"外部事件未登记: {external}")
    span_id = source.span_id or tracing.new_span_id()
    event = {
        "event_id": event_id,
        "event": external,
        "version": 1,
        "task_id": task_id,
        "trace_id": source.trace_id,
        "span_id": span_id,
        "ts": ts or source.ts or _now(),
        "payload": _mapped_payload(external, source),
    }
    return validate_external_event(event)


@dataclass(frozen=True)
class AuditLink:
    """内部来源与外部事件的可序列化审计链。"""

    task_id: str
    event_id: int
    trace_id: str
    span_id: str
    internal_event: str
    external_event: str

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class EventMapper:
    """有状态映射器：生成 event_id，并支持 span/event 双向反查。"""

    def __init__(self) -> None:
        self._next_ids: dict[str, int] = {}
        self._external: dict[tuple[str, int], dict[str, Any]] = {}
        self._by_span: dict[str, list[dict[str, Any]]] = {}
        self._links: dict[tuple[str, int], AuditLink] = {}

    def map(self, source: Envelope, *, task_id: str, event_id: int | None = None, ts: str | None = None) -> dict[str, Any] | None:
        # 审计专用内部事件没有外部 event_id，不得消耗任务外部流的序号。
        if INTERNAL_TO_EXTERNAL.get(source.action, "__unknown__") is None:
            return map_internal_event(source, task_id=task_id, event_id=event_id or 1, ts=ts)
        next_id = self._next_ids.get(task_id, 0) + 1
        actual_id = next_id if event_id is None else event_id
        if actual_id != next_id:
            raise EventValidationError(f"event_id 必须从 1 连续递增: expected={next_id}, current={actual_id}")
        result = map_internal_event(source, task_id=task_id, event_id=actual_id, ts=ts)
        self._next_ids[task_id] = actual_id
        if result is None:
            return None
        self._external[(task_id, actual_id)] = result
        span = result.get("span_id", "")
        self._by_span.setdefault(span, []).append(result)
        self._links[(task_id, actual_id)] = AuditLink(
            task_id=task_id,
            event_id=actual_id,
            trace_id=result["trace_id"],
            span_id=span,
            internal_event=source.action,
            external_event=result["event"],
        )
        return result

    # Friendly aliases used by bus adapters and tests.
    map_event = map

    def lookup_by_span(self, span_id: str) -> list[dict[str, Any]]:
        return copy.deepcopy(self._by_span.get(span_id, []))

    def lookup_external(self, event_id: int, *, task_id: str | None = None) -> dict[str, Any] | None:
        if task_id is not None:
            value = self._external.get((task_id, event_id))
            return copy.deepcopy(value) if value else None
        matches = [value for (tid, eid), value in self._external.items() if eid == event_id]
        return copy.deepcopy(matches[0]) if len(matches) == 1 else None

    def audit_link(self, *, task_id: str, event_id: int) -> dict[str, Any] | None:
        link = self._links.get((task_id, event_id))
        return link.to_dict() if link else None

    reverse_lookup = lookup_by_span
    to_external = map

    def trace_chain(self, span_id: str) -> list[dict[str, Any]]:
        return [
            {"internal_span_id": span_id, "external": event, "trace_id": event["trace_id"]}
            for event in self.lookup_by_span(span_id)
        ]

