"""trace_id / span_id 生成与请求级上下文（§6.2 ID 规范 / §7.6.2 链路追踪）。

trace_id 格式：uh_trc_<32位十六进制随机>，内核在请求入口生成，全链路透传。
"""
from __future__ import annotations

import contextvars
import secrets
from contextlib import contextmanager
from typing import Iterator

_TRACE_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar("uh_trace_id", default=None)
_SPAN_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar("uh_span_id", default=None)


def new_trace_id() -> str:
    return "uh_trc_" + secrets.token_hex(16)


def new_span_id() -> str:
    return "sp_" + secrets.token_hex(4)


def get_trace_id() -> str | None:
    return _TRACE_ID.get()


def get_span_id() -> str | None:
    return _SPAN_ID.get()


def bind(trace_id: str, span_id: str | None = None) -> None:
    _TRACE_ID.set(trace_id)
    _SPAN_ID.set(span_id or new_span_id())


def clear() -> None:
    _TRACE_ID.set(None)
    _SPAN_ID.set(None)


@contextmanager
def request_context(trace_id: str | None = None) -> Iterator[str]:
    tid = trace_id or new_trace_id()
    bind(tid)
    try:
        yield tid
    finally:
        clear()
