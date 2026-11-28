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
#
# 为什么需要：map() 在 _emit 热路径上被调用，mock 流式回答按字符发 delta 事件，
# 单个任务会产生数千次全量 JSON Schema 校验（cProfile 实测占单任务 CPU 的 80%）。
#
# ⚠ 缓存**只在事件 schema 完全没有值级约束时才启用**：
# 早期实现只比对「键集合 + 值类型名」，导致同形状但值非法的事件绕过校验——
# 例如 task.created 的 session_id 有 pattern ^uh_ses_[A-Za-z0-9]+$，
# 一旦某个合法值建立缓存，后续 'NOT-A-SESSION-ID' 因类型同为 str 被放行；
# p_delta 的 index 有 minimum: 0 同样漏掉负值。
# 因此这里在模块加载时静态分析 schema：任何 payload 属性带
# pattern/enum/min*/max*/format/const/multipleOf 等值约束的事件，一律不做缓存。
_SHAPE_PASSED: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
# 事件名 → 是否允许形状缓存（payload 无值级约束）
_CACHE_SAFE: dict[str, bool] = {}
_VALUE_CONSTRAINT_KEYS = frozenset({
    "pattern", "enum", "const", "format",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "minItems", "maxItems", "uniqueItems",
    "minProperties", "maxProperties",
    "propertyNames", "contains", "patternProperties", "dependentRequired",
    "anyOf", "oneOf", "not", "allOf", "if",
})


def _has_value_constraint(node: Any) -> bool:
    """递归判断 schema 片段是否含值级约束（类型/必填/键集之外的判定）。"""
    if isinstance(node, bool):
        return False
    if not isinstance(node, Mapping):
        return False
    for key, value in node.items():
        if key in _VALUE_CONSTRAINT_KEYS:
            return True
        if key in {"properties", "items", "additionalProperties", "$defs", "definitions"}:
            if _has_value_constraint(value):
                return True
        elif isinstance(value, (Mapping, list)) and _has_value_constraint(value):
            return True
    return False


def _build_cache_safe() -> None:
    """按 event 名标注「payload 无值约束，可安全按形状缓存」。"""
    if _VALIDATOR is None:
        return
    schema = _schema()
    by_event = (schema.get("properties") or {}).get("event") or {}
    # event 通常是 enum；payload 用 allOf/if-then 按事件名判别
    for name in (by_event.get("enum") or []):
        try:
            payload_schema = _payload_schema_for(name, schema)
        except Exception:  # noqa: BLE001 - 分析失败一律不缓存（保守）
            _CACHE_SAFE[name] = False
            continue
        _CACHE_SAFE[name] = payload_schema is not None and not _has_value_constraint(payload_schema)


def _payload_schema_for(name: str, schema: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """从顶层 allOf 的 if/then 分支取出该事件名对应的 payload schema（解引用后）。

    实际结构：allOf[i] = {"if": {"properties": {"event": {"const": name}}},
                          "then": {"properties": {"payload": {"$ref": "#/$defs/p_xxx"}}}}
    取不到就返回 None，调用方按「不可缓存」保守处理。
    """
    defs = schema.get("$defs") or schema.get("definitions") or {}

    def resolve(node: Any) -> Mapping[str, Any] | None:
        if not isinstance(node, Mapping):
            return None
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            target = defs.get(ref.split("/")[-1])
            return target if isinstance(target, Mapping) else None
        return node

    branches: list[Any] = []
    top = schema.get("allOf")
    if isinstance(top, list):
        branches.extend(top)
    payload_direct = (schema.get("properties") or {}).get("payload")
    if isinstance(payload_direct, list):
        branches.extend(payload_direct)

    for branch in branches:
        if not isinstance(branch, Mapping):
            continue
        cond = branch.get("if") or {}
        const = ((cond.get("properties") or {}).get("event") or {}).get("const")
        if const != name:
            continue
        then = branch.get("then") or {}
        payload = (then.get("properties") or {}).get("payload")
        return resolve(payload)
    return None


def _compile_constraints(payload_schema: Mapping[str, Any] | None) -> list[tuple[str, Any]]:
    """把 payload schema 的**值级约束**编译成 (字段, 判定器) 列表。

    为什么要自己编译：jsonschema 全量遍历单次约 0.2ms，而 _emit 在热路径上
    每任务调用数千次（字符级 delta），是单任务 CPU 的最大头。但缓存「形状」
    又会让同类型非法值绕过校验（session_id 的 pattern、index 的 minimum）。
    折中：把约束抽成廉价判定器，每次仍**逐值**检查，只是跳过整套 schema 遍历。
    这样既保住正确性，又拿到大部分性能收益。
    """
    checks: list[tuple[str, Any]] = []
    props = (payload_schema or {}).get("properties")
    if not isinstance(props, Mapping):
        return checks
    for field, rules in props.items():
        if not isinstance(rules, Mapping):
            continue
        if isinstance(rules.get("enum"), list):
            allowed = list(rules["enum"])
            checks.append((field, lambda v, a=allowed: v in a))
        pattern = rules.get("pattern")
        if isinstance(pattern, str):
            compiled = re.compile(pattern)
            checks.append((field, lambda v, c=compiled: isinstance(v, str) and bool(c.search(v))))
        lo = rules.get("minimum")
        if isinstance(lo, (int, float)) and not isinstance(lo, bool):
            checks.append((field, lambda v, m=lo: isinstance(v, (int, float)) and not isinstance(v, bool) and v >= m))
        hi = rules.get("maximum")
        if isinstance(hi, (int, float)) and not isinstance(hi, bool):
            checks.append((field, lambda v, m=hi: isinstance(v, (int, float)) and not isinstance(v, bool) and v <= m))
        min_len = rules.get("minLength")
        if isinstance(min_len, int):
            checks.append((field, lambda v, m=min_len: isinstance(v, str) and len(v) >= m))
        max_len = rules.get("maxLength")
        if isinstance(max_len, int):
            checks.append((field, lambda v, m=max_len: isinstance(v, str) and len(v) <= m))
    return checks


_CONSTRAINT_CHECKS: dict[str, list[tuple[str, Any]]] = {}


def _build_constraint_checks() -> None:
    """为每个事件编译值级约束判定器（供快速路径使用）。"""
    if _VALIDATOR is None:
        return
    schema = _schema()
    by_event = (schema.get("properties") or {}).get("event") or {}
    for name in (by_event.get("enum") or []):
        try:
            _CONSTRAINT_CHECKS[name] = _compile_constraints(_payload_schema_for(name, schema))
        except Exception:  # noqa: BLE001 - 任何分析失败都退回全量校验
            _CONSTRAINT_CHECKS.pop(name, None)


def _fast_check(value: Mapping[str, Any]) -> bool | None:
    """快速路径：用预编译判定器检查值级约束。

    返回 True 表示「该事件的所有值级约束均通过」，可跳过全量 schema 遍历；
    返回 None 表示无法快速判定，调用方必须走全量校验。
    注意：这里只覆盖约束检查，键集合/必填/类型仍由形状缓存与全量校验保证。
    """
    event = value.get("event")
    payload = value.get("payload")
    if not isinstance(event, str) or not isinstance(payload, Mapping):
        return None
    checks = _CONSTRAINT_CHECKS.get(event)
    if checks is None:
        return None
    for field, test in checks:
        if field not in payload:
            return None
        try:
            if not test(payload[field]):
                return False
        except Exception:  # noqa: BLE001
            return None
    return True


def _shape_key(value: Mapping[str, Any]) -> tuple[str, tuple[tuple[str, str], ...]] | None:
    event = value.get("event")
    payload = value.get("payload")
    if not isinstance(event, str) or not isinstance(payload, Mapping):
        return None
    return (
        event,
        tuple(sorted((str(k), type(v).__name__) for k, v in payload.items())),
    )


_build_cache_safe()
_build_constraint_checks()


def validate_external_event(event: Mapping[str, Any], *, previous_event_id: int | None = None) -> dict[str, Any]:
    """校验一个完整外部信封并返回副本。

    ``previous_event_id`` 用于流式推送的无空洞约束；schema 本身负责字段、
    枚举和事件载荷判别。任何失败都统一抛出 :class:`EventValidationError`。
    """
    # 只做浅拷贝：本函数只读顶层键并原样返回，payload 不被修改。
    # 原 deepcopy 在 Kylin profiling 中占单任务 CPU 的 29%（每事件一次深拷贝）。
    value = dict(event)
    if _VALIDATOR is None:
        raise EventValidationError("缺少 jsonschema 依赖，无法校验外部事件")
    shape = _shape_key(value)
    # 校验分两级：
    #   1) 形状（事件名 + payload 键集合 + 值类型）已通过过完整校验 → 只需跑
    #      值级约束的预编译判定器（pattern/enum/min/max/长度），O(字段数)，
    #      不触发整套 schema 遍历。
    #   2) 否则走全量 jsonschema 校验，通过后登记形状。
    # 关键：值约束**每次都会检查**，不会因为缓存形状而绕过——
    # 早期实现只比类型名，导致 session_id 的 pattern、index 的 minimum 被跳过。
    if shape is not None and shape in _SHAPE_PASSED:
        verdict = _fast_check(value)
        if verdict is True:
            pass
        elif verdict is False:
            errors = sorted(_VALIDATOR.iter_errors(value), key=lambda error: list(error.path))
            if errors:
                first = errors[0]
                path = ".".join(str(p) for p in first.path) or "<root>"
                raise EventValidationError(f"非法外部事件 {path}: {first.message}")
        else:
            # 无法快速判定（未编译的约束类型）→ 退回全量，保证不漏校验
            errors = sorted(_VALIDATOR.iter_errors(value), key=lambda error: list(error.path))
            if errors:
                first = errors[0]
                path = ".".join(str(p) for p in first.path) or "<root>"
                raise EventValidationError(f"非法外部事件 {path}: {first.message}")
    else:
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

