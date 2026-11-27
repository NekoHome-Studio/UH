"""跨进程会话注册表（M3-9 多进程支持）。

多 worker 下 app.state.sessions / session_owners 是进程内集合，
worker A 建的会话在 worker B 校验会 USR-1003/1002。本模块把会话归属外置到 Redis，
无 Redis 时回退进程内 dict（单进程语义不变）。

任务本身仍粘在创建它的进程（含 asyncio.Queue 不可跨进程）；
WS/SSE 订阅经 task_id 路由，需粘性会话或独立任务路由层。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

_PREFIX = "uh:session:"
_TTL_S = int(os.environ.get("UH_SESSION_REGISTRY_TTL_S", "86400"))


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class SessionRegistry:
    """会话存在性 + 归属的共享视图。async 接口，Redis 或本地回退。"""

    def __init__(self, redis: Any = None) -> None:
        self._redis = redis
        self._local: dict[str, tuple[str, float]] = {}

    @property
    def shared(self) -> bool:
        return self._redis is not None

    async def register(self, session_id: str, owner_client_id: str, *,
                       created_at: str = "", status: str = "active") -> None:
        """写入会话完整状态。

        M3 独立复审 P1：原来只存 owner，导致 (a) 关闭状态不跨副本传播——
        副本 B 关闭后副本 A 仍返回 active 并接受发消息；(b) 缺 created_at，
        GET 只能用查询时间填充。现在 owner/created_at/status 作为一份状态存。
        """
        doc = json.dumps({
            "owner": owner_client_id,
            "created_at": created_at or _now(),
            "status": status,
        }, ensure_ascii=False)
        if self._redis is not None:
            await self._redis.set(_PREFIX + session_id, doc, ex=_TTL_S)
            return
        self._local[session_id] = (doc, time.time())

    async def _raw(self, session_id: str) -> dict[str, Any] | None:
        doc: str | None
        if self._redis is not None:
            value = await self._redis.get(_PREFIX + session_id)
            if value is None:
                return None
            doc = value.decode() if isinstance(value, bytes) else str(value)
        else:
            entry = self._local.get(session_id)
            if entry is None:
                return None
            doc, created = entry
            if time.time() - created > _TTL_S:
                self._local.pop(session_id, None)
                return None
        try:
            parsed = json.loads(doc)
        except ValueError:
            # 兼容旧格式（纯 owner 字符串）
            return {"owner": doc, "created_at": "", "status": "active"}
        return parsed if isinstance(parsed, dict) else None

    async def state(self, session_id: str) -> dict[str, Any] | None:
        """会话完整状态：{owner, created_at, status}。不存在返回 None。"""
        return await self._raw(session_id)

    async def owner(self, session_id: str) -> str | None:
        doc = await self._raw(session_id)
        if doc is None:
            return None
        owner = doc.get("owner")
        return str(owner) if owner is not None else None

    async def status(self, session_id: str) -> str | None:
        doc = await self._raw(session_id)
        return None if doc is None else str(doc.get("status") or "active")

    async def close(self, session_id: str) -> bool:
        """标记会话关闭。跨副本生效（状态在同一份文档里）。返回是否命中。"""
        doc = await self._raw(session_id)
        if doc is None:
            return False
        doc["status"] = "closed"
        payload = json.dumps(doc, ensure_ascii=False)
        if self._redis is not None:
            await self._redis.set(_PREFIX + session_id, payload, ex=_TTL_S)
        else:
            self._local[session_id] = (payload, time.time())
        return True

    async def exists(self, session_id: str) -> bool:
        return await self._raw(session_id) is not None

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
