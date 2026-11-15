import json
import logging

from kernel import tracing
from kernel.logging import JsonFormatter, log_event


def _record(level=logging.INFO, msg="route.decided", **extra):
    rec = logging.LogRecord("kernel.router", level, __file__, 1, msg, None, None)
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


def test_required_fields():
    tracing.clear()
    out = JsonFormatter().format(_record())
    data = json.loads(out)
    for f in ("timestamp", "level", "trace_id", "span_id", "module", "event"):
        assert f in data
    assert data["level"] == "info"
    assert data["module"] == "kernel.router"
    assert data["event"] == "route.decided"
    assert data["timestamp"].endswith("Z")


def test_trace_id_from_context():
    with tracing.request_context("uh_trc_" + "ef" * 16):
        data = json.loads(JsonFormatter().format(_record()))
        assert data["trace_id"] == "uh_trc_" + "ef" * 16
        assert data["span_id"].startswith("sp_")


def test_extra_fields():
    rec = _record(event="route.decided", fields={"task_type": "search", "latency_ms": 45})
    data = json.loads(JsonFormatter().format(rec))
    assert data["task_type"] == "search"
    assert data["latency_ms"] == 45
    assert data["event"] == "route.decided"


def test_levels():
    for lvl, name in [(logging.DEBUG, "debug"), (logging.WARNING, "warning"), (logging.ERROR, "error")]:
        data = json.loads(JsonFormatter().format(_record(level=lvl)))
        assert data["level"] == name


def test_log_event_helper(caplog):
    log = logging.getLogger("kernel.test")
    with caplog.at_level(logging.INFO):
        log_event(log, logging.INFO, "capability.invoked", module_name="llama_index")
    rec = caplog.records[-1]
    assert rec.event == "capability.invoked"
    assert rec.fields == {"module_name": "llama_index"}
