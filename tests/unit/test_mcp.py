import asyncio

import pytest

from kernel.mcp import InProcessTransport, MCPError, MCPHost, MAX_RESPONSE_BYTES
from kernel.protocol import CapabilityRegistry, Envelope, EventBus, StorageGuard


class MockMCPServer:
    def __init__(self):
        self.calls = 0
        self.cancelled = []
        self.failures_before_success = 0
        self.large = False
        self.slow = False

    async def handle(self, request):
        method = request.get("method")
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": request["id"], "result": {"protocolVersion": "2024-11-05", "capabilities": {}}}
        if method == "notifications/initialized":
            return None
        if method == "notifications/cancelled":
            self.cancelled.append(request.get("params"))
            return None
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": request["id"], "result": {"tools": [{"name": "web-search", "description": "search", "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": True}}, {"name": "write-tool", "description": "write", "inputSchema": {"type": "object"}}]}}
        if method == "tools/call":
            self.calls += 1
            name = request["params"]["name"]
            if name == "slow":
                self.slow = True
                await asyncio.sleep(1)
            if self.failures_before_success:
                self.failures_before_success -= 1
                return {"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32000, "message": "temporary"}}
            text = "x" * (MAX_RESPONSE_BYTES + 1000) if self.large else "result"
            return {"jsonrpc": "2.0", "id": request["id"], "result": {"content": [{"type": "text", "text": text}]}}
        return {"jsonrpc": "2.0", "id": request.get("id"), "error": {"code": -32601, "message": "unknown"}}


def run(coro):
    return asyncio.run(coro)


def test_tools_list_and_call_end_to_end():
    server = MockMCPServer()
    host = MCPHost(InProcessTransport(server.handle), backoff_base_s=0)

    async def scenario():
        tools = await host.list_tools()
        result = await host.call_tool("web-search", {"query": "UH"})
        await host.close()
        return tools, result

    tools, result = run(scenario())
    assert [tool["name"] for tool in tools] == ["web-search", "write-tool"]
    assert result["content"][0]["text"] == "result"
    assert result["truncated"] is False


def test_timeout_sends_cancellation_and_structured_error():
    server = MockMCPServer()
    host = MCPHost(InProcessTransport(server.handle), backoff_base_s=0)

    async def scenario():
        await host.list_tools()
        with pytest.raises(MCPError) as caught:
            await host.call_tool("slow", timeout_ms=10, idempotent=False)
        await asyncio.sleep(0)
        return caught.value

    error = run(scenario())
    assert error.code == "UH-CAP-5021"
    assert error.detail == {"module": "tool", "timeout_ms": 10, "tool_name": "slow"}
    assert server.cancelled and server.cancelled[-1]["requestId"]


def test_large_response_is_truncated_under_limit():
    server = MockMCPServer()
    server.large = True
    host = MCPHost(InProcessTransport(server.handle), backoff_base_s=0)
    result = run(host.call_tool("web-search", idempotent=True))
    assert result["truncated"] is True
    assert len(__import__("json").dumps(result, ensure_ascii=False, separators=(",", ":")).encode()) <= MAX_RESPONSE_BYTES


def test_idempotent_retries_but_non_idempotent_does_not():
    server = MockMCPServer()
    server.failures_before_success = 1
    host = MCPHost(InProcessTransport(server.handle), max_retries=2, backoff_base_s=0)

    async def idempotent_call():
        await host.list_tools()
        return await host.call_tool("web-search")

    result = run(idempotent_call())
    assert result["content"][0]["text"] == "result"
    assert server.calls == 2

    server.failures_before_success = 1
    with pytest.raises(MCPError):
        run(host.call_tool("write-tool", risk_level="write-host"))
    assert server.calls == 3


def test_registry_rpc_registration_and_events():
    server = MockMCPServer()
    bus = EventBus()
    events = []

    async def collect(event):
        events.append(event)

    bus.subscribe("mcp.tool.*", collect)
    host = MCPHost(InProcessTransport(server.handle), bus=bus, backoff_base_s=0)
    registry = CapabilityRegistry(bus, StorageGuard())

    async def scenario():
        actions = await host.register_capabilities(registry)
        response = await registry.call("mcp.tool.web-search", {"arguments": {"query": "x"}}, trace_id="t-1")
        return actions, response

    actions, response = run(scenario())
    assert "mcp.tool.web-search" in actions
    assert response.payload["content"][0]["text"] == "result"
    assert [event.action for event in events] == ["mcp.tool.call", "mcp.tool.result"]
    assert events[0].trace_id == "t-1" and events[1].payload["status"] == "ok"


