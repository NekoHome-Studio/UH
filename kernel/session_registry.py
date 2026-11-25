"""跨进程会话注册表（M3-9 多进程支持）。

多 worker 下 app.state.sessions / session_owners 是进程内集合，
worker A 建的会话在 worker B 校验会 USR-1003/1002。本模块把会话归属外置到 Redis，
无 Redis 时回退进程内 dict（单进程语义不变）。

任务本身仍粘在创建它的进程（含 asyncio.Queue 不可跨进程）；
WS/SSE 订阅经 task_id 路由，需粘性会话或独立任务路由层。
"""
from __future__ import annotations

import os
import time
from typing import Any

_PREFIX = "uh:session:"
_TTL_S = int(os.environ.get("UH_SESSION_REGISTRY_TTL_S", "86400"))


class SessionRegistry:
    """会话存在性 + 归属的共享视图。async 接口，Redis 或本地回退。"""

    def __init__(self, redis: Any = None) -> None:
        self._redis = redis
        self._local: dict[str, tuple[str, float]] = {}

    @property
    def shared(self) -> bool:
        return self._redis is not None

    async def register(self, session_id: str, owner_client_id: str) -> None:
        if self._redis is not None:
            await self._redis.set(_PREFIX + session_id, owner_client_id, ex=_TTL_S)
            return
        self._local[session_id] = (owner_client_id, time.time())

    async def owner(self, session_id: str) -> str | None:
        if self._redis is not None:
            value = await self._redis.get(_PREFIX + session_id)
            if value is None:
                return None
            return value.decode() if isinstance(value, bytes) else str(value)
        entry = self._local.get(session_id)
        if entry is None:
            return None
        owner, created = entry
        if time.time() - created > _TTL_S:
            self._local.pop(session_id, None)
            return None
        return owner

    async def exists(self, session_id: str) -> bool:
        return await self.owner(session_id) is not None

    async def forget(self, session_id: str) -> None:
        if self._redis is not None:
            await self._redis.delete(_PREFIX + session_id)
            return
        self._local.pop(session_id, None)


def create_session_registry(memory_backend: Any = None) -> SessionRegistry:
    """复用短期记忆的 Redis 连接；fakeredis 是进程内的，多进程不共享故排除。"""
    client = getattr(memory_backend, "redis", None) or getattr(memory_backend, "client", None)
    if client is not None and type(client).__module__.startswith("fakeredis"):
        client = None
    return SessionRegistry(client)
