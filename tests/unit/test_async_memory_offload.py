"""R2 审查 P0-2：awrite/arecall 曾是协程壳，使 to_thread 卸载失效。"""
import asyncio
import base64
import time

import pytest


@pytest.fixture
def ltm(monkeypatch, tmp_path):
    monkeypatch.setenv("UH_LONG_TERM_STORE", str(tmp_path / "ltm.json"))
    monkeypatch.setenv("UH_MEMORY_MASTER_KEY", base64.urlsafe_b64encode(b"k" * 32).decode())
    from kernel.long_term_memory import create_long_term_memory
    return create_long_term_memory()


def test_awrite_does_not_block_event_loop(ltm):
    """同步体（embedding+AES+写盘 ~84ms）不得在事件循环内执行。

    原实现 `return self.write(...)` 只是协程壳，调用方 iscoroutinefunction
    判定为真后直接 await，to_thread 分支永不触发（R2 实测 100 并发 P95 33s）。
    """
    async def scenario():
        gaps = []
        stop = False

        async def heartbeat():
            last = time.perf_counter()
            while not stop:
                await asyncio.sleep(0.005)
                now = time.perf_counter()
                gaps.append((now - last) * 1000)
                last = now

        hb = asyncio.create_task(heartbeat())
        await asyncio.sleep(0.03)
        gaps.clear()
        for i in range(3):
            await ltm.awrite("probe " + str(i) + "x" * 200, session_id="uh_ses_probe0001")
        stop = True
        await asyncio.sleep(0.02)
        hb.cancel()
        return max(gaps) if gaps else 0.0

    worst = asyncio.run(scenario())
    # 线程池 + GIL 下仍有调度抖动，但不得出现「整段同步 I/O」级别的停顿
    assert worst < 60, f"事件循环最大停顿 {worst:.1f}ms，疑似同步体仍在循环内"


def test_arecall_does_not_block_event_loop(ltm):
    async def scenario():
        for i in range(5):
            await ltm.awrite("content " + str(i), session_id="uh_ses_probe0002")
        gaps = []
        stop = False

        async def heartbeat():
            last = time.perf_counter()
            while not stop:
                await asyncio.sleep(0.005)
                now = time.perf_counter()
                gaps.append((now - last) * 1000)
                last = now

        hb = asyncio.create_task(heartbeat())
        await asyncio.sleep(0.03)
        gaps.clear()
        for _ in range(3):
            await ltm.arecall("content", top_k=5)
        stop = True
        await asyncio.sleep(0.02)
        hb.cancel()
        return max(gaps) if gaps else 0.0

    worst = asyncio.run(scenario())
    assert worst < 60, f"arecall 事件循环停顿 {worst:.1f}ms"


def test_awrite_is_real_async_not_coroutine_shell(ltm):
    """结构断言：awrite 必须经 to_thread，而非直接调同步方法。"""
    import inspect
    src = inspect.getsource(type(ltm).awrite)
    assert "to_thread" in src, "awrite 仍是协程壳"
    src2 = inspect.getsource(type(ltm).arecall)
    assert "to_thread" in src2, "arecall 仍是协程壳"
