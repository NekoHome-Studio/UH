"""短期记忆（M0-4，§4.6 / §9.4）：Redis 滑动窗口。

L0 会话级短期记忆：每会话一个 list 键，滑动窗口保留最近 N 条消息，
带 TTL（会话结束/过期清理），删除会话无残留键。
指标 uh_memory_short_term_keys（键数）。Redis 不可用时开发态回退 fakeredis。
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from . import otel
from .logging import get_logger, log_event

log = get_logger("kernel.memory")


class ShortTermMemory:
    def __init__(self, redis: Any, window: int = 40, ttl_s: int = 3600, prefix: str = "uh:mem:st:"):
        self.redis = redis
        self.window = window  # 最大消息数（默认 40 = 20 轮 × 2）
        self.ttl_s = ttl_s
        self.prefix = prefix

    def _key(self, session_id: str) -> str:
        return self.prefix + session_id

    @otel.instrument_async("memory.write")
    async def append(self, session_id: str, role: str, content: str) -> None:
        key = self._key(session_id)
        msg = json.dumps({"role": role, "content": content, "ts": round(time.time(), 3)}, ensure_ascii=False)
        await self.redis.rpush(key, msg)
        await self.redis.ltrim(key, -self.window, -1)  # 滑动窗口
        await self.redis.expire(key, self.ttl_s)

    async def get_context(self, session_id: str, limit: int | None = None) -> list[dict]:
        # 二审 N13：limit=0 显式返回空（而非 lrange(0,-1) 取全部）；None 用窗口默认。
        if limit is not None and limit <= 0:
            return []
        n = self.window if limit is None else limit
        items = await self.redis.lrange(self._key(session_id), -n, -1)
        return [json.loads(x) for x in items]

    async def clear(self, session_id: str) -> None:
        await self.redis.delete(self._key(session_id))
        log_event(log, logging.INFO, "memory.clear", session_id=session_id)

    async def key_count(self) -> int:
        count = 0
        async for _ in self.redis.scan_iter(match=self.prefix + "*"):
            count += 1
        return count

    async def close(self) -> None:
        await self.redis.aclose()


def create_short_term_memory(window: int | None = None, ttl_s: int | None = None) -> ShortTermMemory:
    w = window if window is not None else int(os.environ.get("UH_MEMORY_WINDOW", "40"))
    t = ttl_s if ttl_s is not None else int(os.environ.get("UH_MEMORY_TTL_S", "3600"))
    url = os.environ.get("UH_REDIS_URL")
    if url:
        import redis.asyncio as aioredis
        client = aioredis.from_url(url, decode_responses=True)
        log_event(log, logging.INFO, "memory.backend", backend="redis")
    else:
        import fakeredis.aioredis as fake
        client = fake.FakeRedis(decode_responses=True)
        log_event(log, logging.WARNING, "memory.backend", backend="fakeredis", reason="UH_REDIS_URL unset (dev fallback)")
    return ShortTermMemory(client, window=w, ttl_s=t)
