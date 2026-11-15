import re

from kernel import tracing


def test_trace_id_format():
    tid = tracing.new_trace_id()
    assert re.fullmatch(r"uh_trc_[0-9a-f]{32}", tid)


def test_trace_id_unique():
    ids = {tracing.new_trace_id() for _ in range(1000)}
    assert len(ids) == 1000


def test_span_id_format():
    assert re.fullmatch(r"sp_[0-9a-f]{8}", tracing.new_span_id())


def test_context_bind_get_clear():
    tracing.clear()
    assert tracing.get_trace_id() is None
    with tracing.request_context("uh_trc_" + "ab" * 16) as tid:
        assert tracing.get_trace_id() == tid
        assert tracing.get_span_id() is not None
    assert tracing.get_trace_id() is None
    assert tracing.get_span_id() is None


def test_context_accepts_external_trace():
    with tracing.request_context("uh_trc_" + "cd" * 16) as tid:
        assert tid == "uh_trc_" + "cd" * 16
