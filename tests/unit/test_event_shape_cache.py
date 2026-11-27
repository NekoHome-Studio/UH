"""v0.2.0 性能：事件 schema 形状缓存不得削弱契约校验。"""
import pytest

from kernel import event_mapping
from kernel.event_mapping import EventValidationError, validate_external_event

TASK = "uh_task_mapping1"
TRACE = "uh_trc_mapping1"


def _event(payload=None, event_id=1, event="delta"):
    return {
        "event_id": event_id, "event": event, "version": 1, "task_id": TASK,
        "trace_id": TRACE, "span_id": "sp_1", "ts": "2026-11-16T00:00:00Z",
        "payload": payload if payload is not None else {"text": "x", "index": 0},
    }


class _CountingValidator:
    """包住真实 validator，统计全量 schema 遍历次数。"""

    def __init__(self, real):
        self.real = real
        self.calls = 0

    def iter_errors(self, value):
        self.calls += 1
        return self.real.iter_errors(value)


def test_shape_cache_skips_repeat_validation(monkeypatch):
    """同形状重复事件只做一次全量 schema 校验（热路径优化）。"""
    event_mapping._SHAPE_PASSED.clear()
    counter = _CountingValidator(event_mapping._VALIDATOR)
    monkeypatch.setattr(event_mapping, "_VALIDATOR", counter)
    for i in range(50):
        validate_external_event(_event(payload={"text": "x", "index": i}, event_id=i + 1))
    assert counter.calls == 1, "同类形状重复校验了 %d 次" % counter.calls


def test_cache_does_not_mask_type_errors():
    """形状缓存按值类型区分：payload 值类型变化必须重新校验并被拒。"""
    event_mapping._SHAPE_PASSED.clear()
    validate_external_event(_event(payload={"text": "ok", "index": 0}, event_id=1))
    with pytest.raises(EventValidationError):
        validate_external_event(_event(payload={"text": 123, "index": 0}, event_id=2))


def test_missing_payload_field_still_rejected():
    """字段缺失属不同形状，必须重新校验并被拒。"""
    event_mapping._SHAPE_PASSED.clear()
    validate_external_event(_event(payload={"text": "ok", "index": 0}, event_id=1))
    with pytest.raises(EventValidationError):
        validate_external_event(_event(payload={"text": "x"}, event_id=2))


def test_invalid_event_rejected_on_first_sight():
    event_mapping._SHAPE_PASSED.clear()
    with pytest.raises(EventValidationError):
        validate_external_event(_event(event="not.a.real.event", payload={"x": 1}, event_id=1))


def test_event_id_monotonic_still_enforced_after_cache():
    """缓存只跳过 schema 遍历，event_id 无空洞约束仍生效。"""
    event_mapping._SHAPE_PASSED.clear()
    validate_external_event(_event(payload={"text": "a", "index": 0}, event_id=1))
    validate_external_event(_event(payload={"text": "b", "index": 1}, event_id=2), previous_event_id=1)
    with pytest.raises(EventValidationError):
        validate_external_event(_event(payload={"text": "c", "index": 2}, event_id=5), previous_event_id=2)
