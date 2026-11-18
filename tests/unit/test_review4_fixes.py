"""第四轮三审修复——绕过路径回归。"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from kernel.main import create_app
from kernel.protocol import EventBus, CapabilityRegistry, StorageGuard
from kernel.mcp import MCPHost, InProcessTransport
from kernel.retrieval import ChromaVectorStore, Document
from kernel.tasks import TaskManager


# --- 三审 P1-6：不存在 session → 404 ---
def test_unknown_session_rejected():
    with TestClient(create_app()) as c:
        r = c.post("/api/v1/sessions/uh_ses_nonexistent/messages", json={"content": "hi"})
        assert r.status_code == 404 and r.json()["error"]["code"] == "UH-USR-1003"


# --- 三审 P1-1：UH_PUBLIC_BASE_URL https→wss ---
def test_stream_url_public_base_wss(monkeypatch):
    monkeypatch.setenv("UH_PUBLIC_BASE_URL", "https://uh.example")
    with TestClient(create_app()) as c:
        sid = c.post("/api/v1/sessions").json()["data"]["session_id"]
        h = c.post("/api/v1/sessions/" + sid + "/messages", json={"content": "hi"}).json()["data"]
        assert h["stream_url"].startswith("wss://uh.example/api/v1/stream")


# --- 三审 N1：Chroma 后端 guard 始终存在且强制 ---
def test_chroma_store_guard_enforced():
    g = StorageGuard()  # 未声明任何命名空间
    store = ChromaVectorStore(guard=g, module="retrieval.search", namespace="retrieval")
    assert hasattr(store, "_guard")  # 构造不再跳过 super，_guard 必存在
    with pytest.raises(PermissionError):
        store.add(Document("d1", "text"))  # 未声明 → 拒绝


# --- 三审 P1-3：fail-closed 补 tool.result(denied) 配对 ---
def test_fail_closed_emits_tool_result():
    bus = EventBus()
    seen = []
    async def cap(env):
        seen.append(env.action)
    bus.subscribe("mcp.tool.*", cap)
    def handler(req):
        m, rid = req.get("method"), req.get("id")
        if m == "initialize":
            return {"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2024-11-05", "capabilities": {}}}
        if m == "tools/list":
            return {"jsonrpc": "2.0", "id": rid, "result": {"tools": [{"name": "danger", "inputSchema": {}}]}}
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    host = MCPHost(InProcessTransport(handler), bus=bus)
    rpc = CapabilityRegistry(bus, StorageGuard())
    asyncio.run(host.register_capabilities(rpc, manifest={"danger": {"risk_level": "irreversible"}}))
    resp = asyncio.run(rpc.call("mcp.tool.danger", {}))
    assert resp.payload.get("degraded")
    assert "mcp.tool.call" in seen and "mcp.tool.result" in seen  # 配对闭合


# --- 三审 P1-5：steps 永久日志超窗不丢 + duration_ms integer ---
def test_steps_persist_beyond_window():
    app = create_app()
    with TestClient(app) as c:
        sid = c.post("/api/v1/sessions").json()["data"]["session_id"]
        h = c.post("/api/v1/sessions/" + sid + "/messages", json={"content": "按步骤制定一个计划", "mode": "workflow"}).json()["data"]
        with c.stream("GET", h["sse_url"]) as r:
            "".join(r.iter_text())
        tid = h["task_id"]
        app.state.tasks.tasks[tid]["event_buffer"].clear()  # 模拟超窗清热缓冲
        steps = app.state.tasks.steps(tid)
        assert steps and all(isinstance(s["duration_ms"], int) for s in steps)  # step_log 永久，超窗仍全量


# --- 三审 N3：终态 chat 任务（无 checkpoint）resume → 409 ---
def test_terminal_chat_resume_409():
    with TestClient(create_app()) as c:
        sid = c.post("/api/v1/sessions").json()["data"]["session_id"]
        h = c.post("/api/v1/sessions/" + sid + "/messages", json={"content": "你好"}).json()["data"]
        with c.stream("GET", h["sse_url"]) as r:
            "".join(r.iter_text())
        rr = c.post("/api/v1/tasks/" + h["task_id"] + "/resume", json={})
        assert rr.status_code == 409
