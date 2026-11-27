"""M3 独立复审 P1：非结构化消息与异常栈绕过日志脱敏。"""
import json
import logging
import sys

import pytest

from kernel.logging import JsonFormatter

SECRET = "Zx9Kq2Lm7Pw4Rt8Yv3Nb6Hc1Jd5Fs0Ga"


def _record(level=logging.INFO, msg="probe", event=None, fields=None, exc_info=None):
    r = logging.LogRecord("kernel.probe", level, __file__, 1, msg, None, exc_info)
    r.event = event if event is not None else msg
    if fields is not None:
        r.fields = fields
    return r


def test_structured_fields_are_redacted():
    out = json.loads(JsonFormatter().format(_record(fields={"password": SECRET})))
    assert out["password"] == "*REDACTED*"


def test_plain_event_text_is_redacted():
    """log.warning("token=...") 走 event，原实现只脱敏 fields。"""
    out = json.loads(JsonFormatter().format(_record(event=f"token={SECRET}")))
    assert SECRET not in json.dumps(out, ensure_ascii=False)


def test_exception_traceback_is_redacted():
    """log.exception 在内核未处理异常分支被调用，栈文本可能含凭据。"""
    try:
        raise ValueError(f"password={SECRET}")
    except ValueError:
        info = sys.exc_info()
    out = json.loads(JsonFormatter().format(_record(level=logging.ERROR, exc_info=info)))
    assert SECRET not in json.dumps(out, ensure_ascii=False)
    # 异常类型与位置仍需可读，便于排查
    assert "ValueError" in out["exc_info"]


def test_trace_and_span_survive_redaction():
    """脱敏不得破坏链路排查能力。"""
    r = _record(event="kernel.request.finished")
    r.trace_id = "uh_trc_" + "a" * 32
    r.span_id = "sp_" + "b" * 8
    out = json.loads(JsonFormatter().format(r))
    assert out["trace_id"] == "uh_trc_" + "a" * 32
    assert out["span_id"] == "sp_" + "b" * 8


def test_contract_ids_in_fields_survive():
    """契约 ID 是排查刚需，整 token 匹配应放行。"""
    task = "uh_task_" + "c" * 32
    out = json.loads(JsonFormatter().format(_record(fields={"path": f"/api/v1/tasks/{task}", "task_id": task})))
    assert task in json.dumps(out, ensure_ascii=False)


def test_natural_language_event_is_untouched():
    out = json.loads(JsonFormatter().format(_record(event="任务已完成处理")))
    assert out["event"] == "任务已完成处理"


def test_bench_uses_authorization_header_not_query():
    """压测不得把 token 放查询串（access log 会记录完整 URL）。"""
    import inspect
    from kernel import bench

    src = inspect.getsource(bench._one_request)
    assert "Authorization" in src
    assert "params=params" not in src, "token 仍在查询串"


def test_dockerfile_disables_access_log():
    from pathlib import Path

    dockerfile = (Path(__file__).resolve().parents[2] / "Dockerfile").read_text(encoding="utf-8")
    cmd = [l for l in dockerfile.splitlines() if l.startswith("CMD")]
    assert cmd and "--no-access-log" in cmd[0], "默认镜像仍在记录 access log"
