import asyncio

import pytest

from kernel.protocol import Envelope, EventBus, CapabilityRegistry, StorageGuard, validate_event_name


def test_envelope_fields():
    env = Envelope(action="capability.module.invoked", payload={"x": 1}, trace_id="uh_trc_a", span_id="sp_1", from_="kernel", to="capability.llm")
    d = env.to_dict()
    for f in ["envelope_version", "trace_id", "span_id", "parent_span_id", "from", "to", "action", "payload", "deadline_ms", "ts", "event_id"]:
        assert f in d
    assert d["from"] == "kernel"


def test_event_name_three_segment():
    validate_event_name("capability.module.invoked")
    validate_event_name("memory.record.write")
    validate_event_name("kernel.route.decided")


def test_event_name_two_segment_rejected():
    with pytest.raises(ValueError):
        validate_event_name("capability.invoked")
    with pytest.raises(ValueError):
        validate_event_name("memory.write")


def test_bus_publish_seq_and_dispatch():
    bus = EventBus()
    got = []
    async def h(env):
        got.append(env)
    bus.subscribe("capability.module.*", h)
    async def run():
        e1 = await bus.publish("capability.module.invoked", {"a": 1}, trace_id="t1")
        e2 = await bus.publish("capability.module.failed", {"a": 2}, trace_id="t1")
        await bus.publish("memory.record.write", {}, trace_id="t1")  # 不匹配
        return e1, e2
    e1, e2 = asyncio.run(run())
    assert e1.event_id == 1 and e2.event_id == 2 and bus.bus_seq == 3
    assert len(got) == 2 and got[0].trace_id == "t1"


def test_bus_subscriber_exception_isolated():
    bus = EventBus()
    ok = []
    async def bad(env):
        raise RuntimeError("boom")
    async def good(env):
        ok.append(env)
    bus.subscribe("kernel.route.decided", bad)
    bus.subscribe("kernel.route.decided", good)
    asyncio.run(bus.publish("kernel.route.decided", {}))
    assert len(ok) == 1  # bad 不影响 good


def _rpc():
    bus = EventBus()
    guard = StorageGuard()
    return CapabilityRegistry(bus, guard), bus


def test_rpc_call_success():
    rpc, bus = _rpc()
    async def handler(req):
        return Envelope(action=req.action, payload={"content": "hi"}, trace_id=req.trace_id, from_=req.to, to=req.from_)
    rpc.register("llm.generate", handler)
    resp = asyncio.run(rpc.call("llm.generate", {"q": 1}, trace_id="t"))
    assert resp.payload["content"] == "hi" and resp.trace_id == "t"


def test_rpc_unavailable_degraded():
    rpc, bus = _rpc()
    resp = asyncio.run(rpc.call("retrieval.search", {}, trace_id="t"))
    assert resp.payload["degraded"] is True


def test_rpc_disabled_degraded():
    rpc, bus = _rpc()
    async def handler(req):
        return Envelope(action=req.action, payload={"docs": []}, trace_id=req.trace_id)
    rpc.register("retrieval.search", handler)
    rpc.set_available("retrieval.search", False)
    resp = asyncio.run(rpc.call("retrieval.search", {}, trace_id="t"))
    assert resp.payload["degraded"] is True


def test_rpc_timeout_degraded():
    rpc, bus = _rpc()
    async def slow(req):
        await asyncio.sleep(1)
        return Envelope(action=req.action, payload={})
    rpc.register("slow.op", slow)
    resp = asyncio.run(rpc.call("slow.op", {}, deadline_ms=50))
    assert resp.payload["degraded"] is True and resp.payload["error_code"] == "UH-CAP-5021"


def test_rpc_handler_error_degraded():
    rpc, bus = _rpc()
    async def bad(req):
        raise RuntimeError("crash")
    rpc.register("bad.op", bad)
    resp = asyncio.run(rpc.call("bad.op", {}))
    assert resp.payload["degraded"] is True and resp.payload["error_code"] == "UH-CAP-4003"


def test_storage_isolation():
    guard = StorageGuard()
    guard.declare("retrieval", {"retrieval"})
    guard.check("retrieval", "retrieval")  # own ok
    with pytest.raises(PermissionError):
        guard.check("retrieval", "memory")  # foreign denied


def test_search_flow_degrades_when_retrieval_off():
    rpc, bus = _rpc()
    async def llm(req):
        return Envelope(action=req.action, payload={"content": "answer"}, trace_id=req.trace_id)
    async def retrieval(req):
        return Envelope(action=req.action, payload={"docs": ["d1"]}, trace_id=req.trace_id)
    rpc.register("llm.generate", llm)
    rpc.register("retrieval.search", retrieval)
    async def flow():
        r = await rpc.call("retrieval.search", {"q": "x"}, trace_id="t")
        degraded = r.payload.get("degraded", False)
        l = await rpc.call("llm.generate", {"q": "x"}, trace_id="t")
        return degraded, l.payload["content"]
    degraded, content = asyncio.run(flow())
    assert degraded is False and content == "answer"
    rpc.set_available("retrieval.search", False)
    degraded2, content2 = asyncio.run(flow())
    assert degraded2 is True and content2 == "answer"  # 检索关闭，对话仍可用（降级）
