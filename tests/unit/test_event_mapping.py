import subprocess

import pytest

from kernel.event_mapping import EventMapper, EventValidationError, map_internal_event, validate_external_event
from kernel.protocol import Envelope


TASK = "uh_task_mapping1"
TRACE = "uh_trc_mapping1"


def source(action, payload, span="sp_mapping1"):
    return Envelope(action=action, payload=payload, trace_id=TRACE, span_id=span)


def test_internal_mapping_and_audit_reverse_lookup():
    mapper = EventMapper()
    event = mapper.map(source("mcp.tool.call", {
        "call_id": "call-1", "tool_name": "search", "args_summary": "{}",
        "risk_level": "read-only", "requires_confirmation": False,
    }), task_id=TASK)
    assert event["event"] == "tool.call"
    assert event["span_id"] == "sp_mapping1"
    assert mapper.lookup_by_span("sp_mapping1")[0]["event_id"] == event["event_id"]
    assert mapper.lookup_external(event["event_id"], task_id=TASK)["trace_id"] == TRACE
    assert mapper.audit_link(task_id=TASK, event_id=event["event_id"])["internal_event"] == "mcp.tool.call"


def test_capability_and_degraded_mappings():
    mapper = EventMapper()
    started = mapper.map(source("capability.module.invoked", {"step_id": "s1", "module": "llm", "name": "generate"}), task_id=TASK)
    degraded = mapper.map(source("kernel.task.degraded", {"reason_code": "UH-CAP-3001", "module": "llama_index", "description": "offline"}), task_id=TASK)
    failed = mapper.map(source("capability.module.failed", {"action": "orchestration.run", "error": "boom"}), task_id=TASK)
    assert started["event"] == "step.started"
    assert degraded["event"] == "task.degraded"
    assert failed["event"] == "step.failed"
    assert failed["payload"]["module"] == "langgraph"
    assert failed["payload"]["error"]["code"] == "UH-CAP-4004"  # ADR-003 strict 默认


def test_audit_only_event_does_not_create_external_id_gap():
    mapper = EventMapper()
    assert mapper.map(source("memory.record.write", {"content": "internal"}), task_id=TASK) is None
    event = mapper.map(source("mcp.tool.result", {
        "call_id": "call-1", "tool_name": "search", "status": "error",
        "truncated": False, "content_preview": [{"type": "text", "text": "x"}],
        "duration_ms": 1, "error": {"code": "UH-CAP-4003"},
    }), task_id=TASK)
    assert event["event_id"] == 1
    assert isinstance(event["payload"]["content_preview"], str)
    assert "error" not in event["payload"]


def test_schema_and_unknown_or_non_contiguous_events_are_rejected():
    valid = {
        "event_id": 1, "event": "delta", "version": 1, "task_id": TASK,
        "trace_id": TRACE, "span_id": "sp_1", "ts": "2026-11-16T00:00:00Z",
        "payload": {"text": "x", "index": 0},
    }
    assert validate_external_event(valid)["event"] == "delta"
    with pytest.raises(EventValidationError):
        validate_external_event({**valid, "event": "future.event"})
    with pytest.raises(EventValidationError):
        validate_external_event({**valid, "payload": {"text": "x"}})
    with pytest.raises(EventValidationError):
        EventMapper().map(source("mcp.tool.call", {}), task_id=TASK, event_id=2)


def test_audit_only_internal_event_does_not_push():
    assert map_internal_event(source("memory.record.write", {"content": "secret"}), task_id=TASK, event_id=1) is None


def test_ui_state_machine_main_paths():
    script = r'''const sm=require('./ui/state-machine.js'); let s=sm.initialState();
    s=sm.transition(s,{event:'task.created',payload:{}}); if(s.state!=='routing')process.exit(1);
    s=sm.transition(s,{event:'step.started',payload:{module:'llm'}}); if(s.state!=='thinking')process.exit(2);
    s=sm.transition(s,{event:'delta',payload:{}}); if(s.state!=='streaming')process.exit(3);
    s=sm.transition(s,{event:'task.degraded',payload:{}}); if(!s.degraded||s.state!=='streaming')process.exit(4);
    s=sm.transition(s,{event:'task.finished',payload:{}}); if(s.state!=='idle'||s.degraded||!s.terminal)process.exit(5);
    s=sm.transition(s,{event:'task.yielded',payload:{}}); if(s.state!=='idle'||!s.terminal)process.exit(6);
    s=sm.transition(s,{event:'task.created',payload:{}}); if(s.state!=='routing'||s.terminal)process.exit(7);
    s=sm.transition(s,{event:'future.event',payload:{}}); if(s.state!=='routing')process.exit(8);'''
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
