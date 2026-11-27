"""统一结构化 JSON 日志（§7.6.1）。

必含字段：timestamp / level / trace_id / span_id / module / event；
可选扩展字段经 log_event(..., **fields) 注入（如 task_type / latency_ms）。
分级：DEBUG / INFO / WARN / ERROR。
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

from . import tracing

_REQUIRED = ("timestamp", "level", "trace_id", "span_id", "module", "event")


def _redact_text(value: str) -> str:
    try:
        from .redaction import redact_text

        return redact_text(value)
    except Exception:
        return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds")
        if ts.endswith("+00:00"):
            ts = ts[:-6] + "Z"
        payload: dict[str, Any] = {
            "timestamp": ts,
            "level": record.levelname.lower(),
            "trace_id": getattr(record, "trace_id", None) or tracing.get_trace_id() or "-",
            "span_id": getattr(record, "span_id", None) or tracing.get_span_id() or "-",
            "module": record.name,
            "event": _redact_text(getattr(record, "event", None) or record.getMessage()),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            # M3-8 §9.9：日志脱敏（凭据→*REDACTED*，正文裁剪）；trace_id/span_id 保留供排查
            from .redaction import redact_fields
            payload.update(redact_fields(fields))
        if record.exc_info:
            payload["exc_info"] = _redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    if not any(isinstance(h, logging.StreamHandler) and isinstance(h.formatter, JsonFormatter) for h in root.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)


def get_logger(module: str) -> logging.Logger:
    return logging.getLogger(module)


def log_event(logger: logging.Logger, level: int, event: str, **fields: Any) -> None:
    # 发射时即绑定 trace_id/span_id，记录自包含（异步/队列 handler 下不丢链路）
    logger.log(level, event, extra={
        "event": event,
        "fields": fields,
        "trace_id": tracing.get_trace_id(),
        "span_id": tracing.get_span_id(),
    })
