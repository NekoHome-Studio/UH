"""M3-9 执行闸：有界并发 + 排队 + 提交优先（#36 解耦）。"""
import asyncio

import pytest

from kernel.execution_gate import ExecutionGate, execution_limit


def test_execution_limit_default_is_positive():
    assert execution_limit() >= 4


def test_execution_limit_env_override(monkeypatch):
    monkeypatch.setenv("UH_TASK_CONCURRENCY", "7")
    assert execution_limit() == 7
    monkeypatch.setenv("UH_TASK_CONCURRENCY", "not-a-number")
    assert execution_limit() >= 4
    monkeypatch.setenv("UH_TASK_CONCURRENCY", "-1")
    assert execution_limit() >= 4


def test_gate_bounds_concurrent_execution():
    async def scenario():
        gate = ExecutionGate(limit=2)
        peak = 0
        active = 0

        async def work():
            nonlocal peak, active
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1

        await asyncio.gather(*(gate.run(work) for _ in range(10)))
        return peak, gate.stats()

    peak, stats = asyncio.run(scenario())
    assert peak <= 2, f"concurrency exceeded limit: {peak}"
    assert stats["completed"] == 10
    assert stats["running"] == 0
    assert stats["queued"] == 0


def test_gate_yields_before_executing():
    """入口让出一次循环，使调用方（HTTP handler）先完成响应。"""
    async def scenario():
        gate = ExecutionGate(limit=4)
        order: list[str] = []

        async def work():
            order.append("exec")

        task = asyncio.create_task(gate.run(work))
        order.append("submit")
        await task
        return order

    order = asyncio.run(scenario())
    assert order[0] == "submit", order


def test_gate_releases_on_exception():
    async def scenario():
        gate = ExecutionGate(limit=1)

        async def boom():
            raise ValueError("probe")

        with pytest.raises(ValueError):
            await gate.run(boom)
        # 闸必须释放，否则后续任务永久阻塞
        async def ok():
            return 42

        return await asyncio.wait_for(gate.run(ok), timeout=1.0)

    assert asyncio.run(scenario()) == 42


def test_task_manager_uses_gate():
    from kernel.tasks import TaskManager

    class _Mem:
        async def append(self, *a, **k):
            return None

    tm = TaskManager(llm=None, memory=_Mem())
    assert tm._gate is not None
    assert tm._gate.limit >= 4
