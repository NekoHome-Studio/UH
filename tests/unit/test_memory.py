import asyncio

import fakeredis.aioredis as fake
import pytest

from kernel.memory import ShortTermMemory


def _mem(window=40, ttl_s=3600):
    return ShortTermMemory(fake.FakeRedis(decode_responses=True), window=window, ttl_s=ttl_s)


def test_20_rounds_context_preserved():
    m = _mem(window=40)
    async def run():
        for i in range(20):
            await m.append("s1", "user", f"q{i}")
            await m.append("s1", "assistant", f"a{i}")
        ctx = await m.get_context("s1")
        assert len(ctx) == 40
        assert ctx[0]["content"] == "q0" and ctx[-1]["content"] == "a19"
    asyncio.run(run())


def test_sliding_window_trims():
    m = _mem(window=10)
    async def run():
        for i in range(20):
            await m.append("s1", "user", f"m{i}")
        ctx = await m.get_context("s1")
        assert len(ctx) == 10
        assert ctx[0]["content"] == "m10" and ctx[-1]["content"] == "m19"
    asyncio.run(run())


def test_clear_removes_keys():
    m = _mem()
    async def run():
        await m.append("s1", "user", "hi")
        assert await m.key_count() == 1
        await m.clear("s1")
        assert await m.key_count() == 0
        assert await m.get_context("s1") == []
    asyncio.run(run())


def test_ttl_set():
    m = _mem(ttl_s=120)
    async def run():
        await m.append("s1", "user", "hi")
        ttl = await m.redis.ttl(m._key("s1"))
        assert 0 < ttl <= 120
    asyncio.run(run())


def test_key_count_multi_session():
    m = _mem()
    async def run():
        await m.append("s1", "user", "a")
        await m.append("s2", "user", "b")
        await m.append("s3", "user", "c")
        assert await m.key_count() == 3
        await m.clear("s2")
        assert await m.key_count() == 2
    asyncio.run(run())


def test_get_context_limit():
    m = _mem(window=40)
    async def run():
        for i in range(10):
            await m.append("s1", "user", f"m{i}")
        ctx = await m.get_context("s1", limit=3)
        assert len(ctx) == 3 and ctx[-1]["content"] == "m9"
    asyncio.run(run())
