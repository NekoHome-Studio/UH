"""M2-3 Python SDK 与 MCP 最小协议。"""
import asyncio

import pytest

from kernel.mcp import InProcessTransport, MCPHost
from kernel.plugin_sdk import MCPPluginServer, ToolPlugin
from kernel.plugins import PluginManifest


def _manifest(tmp_path):
    return PluginManifest(
        name="sdk-tool",
        version="1.0.0",
        type="tool",
        description="SDK test",
        author="test",
        license="MIT",
        permissions=("fs.sandbox.read",),
        entry="main.py",
        config={"timeout_ms": 1000, "memory_limit_mb": 32},
        risk_level="read-only",
        raw={},
    )


def test_sdk_mcp_list_and_call(tmp_path):
    plugin = ToolPlugin(_manifest(tmp_path), package_dir=tmp_path)

    @plugin.tool("echo", description="Echo text", input_schema={"type": "object"})
    def echo(arguments, context):
        return arguments["text"]

    server = MCPPluginServer(plugin)

    async def exercise():
        initialized = await server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert initialized["result"]["serverInfo"]["name"] == "sdk-tool"
        listed = await server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        assert listed["result"]["tools"][0]["name"] == "echo"
        called = await server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "echo", "arguments": {"text": "hello"}}})
        assert called["result"]["content"] == [{"type": "text", "text": "hello"}]

    asyncio.run(exercise())


def test_sdk_works_through_mcp_host(tmp_path):
    plugin = ToolPlugin(_manifest(tmp_path), package_dir=tmp_path)
    plugin.register("echo", lambda args, ctx: args["text"], description="Echo", input_schema={"type": "object"})
    server = MCPPluginServer(plugin)
    host = MCPHost(InProcessTransport(server.handle), backoff_base_s=0)

    async def exercise():
        tools = await host.list_tools()
        assert tools[0]["name"] == "echo"
        result = await host.call_tool("echo", {"text": "through host"})
        assert result["content"][0]["text"] == "through host"
        await host.close()

    asyncio.run(exercise())


def test_sdk_context_uses_sandbox_policy(tmp_path):
    plugin = ToolPlugin(_manifest(tmp_path), package_dir=tmp_path)
    outside = tmp_path.parent / "outside.txt"
    with pytest.raises(Exception):
        plugin.context.write_text(outside, "blocked")

