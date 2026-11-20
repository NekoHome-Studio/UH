"""M2-5 HookManager 的异步隔离。"""
import asyncio

from kernel.hooks import HookManager
from kernel.protocol import EventBus


def test_slow_hook_does_not_block_event_publish():
    bus = EventBus()
    manager = HookManager(bus)
    seen = []

    async def slow(envelope):
        await asyncio.sleep(0.05)
        seen.append(envelope.action)

    async def exercise():
        manager.register("slow", ["kernel.route.decided"], slow, timeout_ms=200)
        started = asyncio.get_running_loop().time()
        await bus.publish("kernel.route.decided", {"task_type": "chat"})
        elapsed = asyncio.get_running_loop().time() - started
        assert elapsed < 0.04
        await asyncio.sleep(0.08)
        assert seen == ["kernel.route.decided"]
        await manager.shutdown()

    asyncio.run(exercise())


def test_hook_failure_and_timeout_are_isolated(caplog):
    bus = EventBus()
    manager = HookManager(bus)
    seen = []
    cancelled = []

    async def failing(_):
        raise RuntimeError("hook failure")

    async def timing_out(_):
        try:
            await asyncio.Future()
        finally:
            cancelled.append(True)

    async def healthy(envelope):
        seen.append(envelope.action)

    async def exercise():
        manager.register("failing", ["kernel.route.decided"], failing)
        manager.register("timeout", ["kernel.route.decided"], timing_out, timeout_ms=5)
        manager.register("healthy", ["kernel.route.decided"], healthy)
        await bus.publish("kernel.route.decided", {})
        await asyncio.sleep(0.08)
        assert seen == ["kernel.route.decided"]
        assert cancelled == [True]
        await manager.shutdown()

    asyncio.run(exercise())
    assert {"hook.failed", "hook.timeout"} <= {record.getMessage() for record in caplog.records}


def test_hook_receives_one_metadata_snapshot_and_can_be_removed():
    async def exercise():
        bus = EventBus()
        manager = HookManager(bus)
        received = []
        delivered = asyncio.Event()

        async def hook(envelope):
            received.append(envelope)
            delivered.set()

        manager.register("audit", ["tool.*", "tool.confirmation.requested"], hook)
        original = await bus.publish("tool.confirmation.requested", {
            "call_id": "call-1",
            "confirmation_token": "fixture-only",
            "summary": "private prompt",
        })
        await asyncio.wait_for(delivered.wait(), timeout=1)
        assert [envelope.payload for envelope in received] == [{"call_id": "call-1"}]
        assert "confirmation_token" in original.payload
        await manager.shutdown()
        assert bus._subs == []

    asyncio.run(exercise())

