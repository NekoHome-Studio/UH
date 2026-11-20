"""UH! Python 工具插件 SDK 的最小 MCP 实现。

插件作者只需继承 :class:`ToolPlugin`、注册工具处理函数，再调用
``run_stdio(plugin)``。协议边界保持 MCP JSON-RPC 形状，运行时权限通过
``PluginContext`` 复用 M2-2 的 SandboxPolicy。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from .plugins import PluginManifest
from .sandbox import SandboxPolicy

ToolHandler = Callable[[dict[str, Any], "ToolContext"], Any]


@dataclass
class ToolContext:
    manifest: PluginManifest
    policy: SandboxPolicy

    def read_text(self, path: str | Path) -> str:
        return self.policy.check_path(path, write=False).read_text(encoding="utf-8")

    def write_text(self, path: str | Path, content: str) -> None:
        target = self.policy.check_path(path, write=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def allow_host(self, host_or_url: str) -> str:
        return self.policy.check_host(host_or_url)


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    risk_level: str = "read-only"
    idempotent: bool = True

    def as_mcp(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "risk_level": self.risk_level,
            "annotations": {"readOnlyHint": self.idempotent, "riskLevel": self.risk_level},
        }


class ToolPlugin:
    def __init__(self, manifest: PluginManifest, *, package_dir: str | Path):
        self.manifest = manifest
        self.package_dir = Path(package_dir).resolve()
        self.policy = SandboxPolicy(
            plugin_dir=self.package_dir,
            permissions=set(manifest.permissions),
            allow_hosts=tuple((manifest.network or {}).get("allow_hosts", [])),
        )
        self.context = ToolContext(manifest, self.policy)
        self._tools: dict[str, ToolDefinition] = {}

    def tool(
        self,
        name: str,
        *,
        description: str,
        input_schema: dict[str, Any] | None = None,
        risk_level: str | None = None,
        idempotent: bool | None = None,
    ):
        def decorator(handler: ToolHandler):
            self.register(
                name,
                handler,
                description=description,
                input_schema=input_schema or {"type": "object"},
                risk_level=risk_level or self.manifest.risk_level or "read-only",
                idempotent=self.manifest.risk_level == "read-only" if idempotent is None else idempotent,
            )
            return handler

        return decorator

    def register(
        self,
        name: str,
        handler: ToolHandler,
        *,
        description: str,
        input_schema: dict[str, Any],
        risk_level: str = "read-only",
        idempotent: bool = True,
    ) -> None:
        if name in self._tools:
            raise ValueError(f"duplicate tool: {name}")
        self._tools[name] = ToolDefinition(name, description, dict(input_schema), handler, risk_level, idempotent)

    def list_tools(self) -> list[dict[str, Any]]:
        return [definition.as_mcp() for definition in self._tools.values()]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        definition = self._tools.get(name)
        if definition is None:
            raise KeyError(name)
        value = definition.handler(arguments, self.context)
        if inspect.isawaitable(value):
            value = await value
        if isinstance(value, list) and all(isinstance(item, dict) and isinstance(item.get("type"), str) for item in value):
            return value
        return [{"type": "text", "text": str(value)}]


class MCPPluginServer:
    def __init__(self, plugin: ToolPlugin):
        self.plugin = plugin

    async def handle(self, request: Mapping[str, Any]) -> dict[str, Any] | None:
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}
        if method == "notifications/initialized":
            return None
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": self.plugin.manifest.name, "version": self.plugin.manifest.version}}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": self.plugin.list_tools()}}
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            try:
                content = await self.plugin.call_tool(str(name), dict(arguments))
            except KeyError:
                return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": f"unknown tool: {name}"}}
            except Exception as exc:
                return {"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": str(exc)}], "isError": True}}
            return {"jsonrpc": "2.0", "id": request_id, "result": {"content": content, "isError": False}}
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"method not found: {method}"}}

    async def serve(self, reader: Any = None, writer: Any = None) -> None:
        reader = reader or asyncio.StreamReader()

        async def send(data: bytes) -> None:
            if writer is not None:
                writer.write(data)
                await writer.drain()

        await self._serve(reader.readline, send)

    async def _serve(
        self, readline: Callable[[], Awaitable[bytes]], send: Callable[[bytes], Awaitable[None]],
    ) -> None:
        while True:
            raw = await readline()
            if not raw:
                return
            response = await self.handle(json.loads(raw.decode("utf-8")))
            if response is not None:
                await send((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))


def run_stdio(plugin: ToolPlugin) -> None:
    """Run a plugin over line-delimited JSON-RPC on stdin/stdout."""

    async def readline() -> bytes:
        # Inherited Windows stdio handles need not support overlapped I/O.
        limit = 64 * 1024 + 1
        line = await asyncio.to_thread(sys.stdin.buffer.readline, limit + 1)
        if len(line) > limit:
            raise ValueError("MCP stdio message exceeds 64 KiB")
        return line

    def write_and_flush(data: bytes) -> None:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()

    async def send(data: bytes) -> None:
        await asyncio.to_thread(write_and_flush, data)

    asyncio.run(MCPPluginServer(plugin)._serve(readline, send))


__all__ = ["MCPPluginServer", "ToolContext", "ToolDefinition", "ToolPlugin", "run_stdio"]

