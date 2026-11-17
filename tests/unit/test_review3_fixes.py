"""第三轮审查修复——绕过路径回归（每条测攻击路径，非仅修复函数）。"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from kernel.main import create_app
from kernel.protocol import EventBus, CapabilityRegistry, StorageGuard
from kernel.mcp import MCPHost, InProcessTransport, MCPError


def _auth(monkeypatch, *, local="0", required="1", token="tok"):
    monkeypatch.setenv("UH_LOCAL_MODE", local)
    monkeypatch.setenv("UH_AUTH_REQUIRED", required)
    monkeypatch.setenv("UH_AUTH_TOKEN", token)


# --- 二审 N2：鉴权覆盖遗留别名（匿名不可清库/导出） ---
def test_auth_covers_legacy_aliases(monkeypatch, tmp_path):
    _auth(monkeypatch)
    monkeypatch.setenv("UH_LONG_TERM_STORE", str(tmp_path / "m.json"))
    with TestClient(create_app()) as c:
        assert c.get("/memory/export").status_code == 401
        assert c.delete("/memory/long-term").status_code == 401
        assert c.get("/memory/short-term?session_id=x").status_code == 401
        assert c.get("/plugins").status_code == 401
        assert c.get("/api/v1/memory/export").status_code == 401
        assert c.get("/health").status_code == 200  # ops 豁免


# --- 二审 N1：单设 UH_AUTH_REQUIRED=1 即生效（无 UH_LOCAL_MODE 默认豁免） ---
def test_auth_required_alone_enables(monkeypatch):
    monkeypatch.setenv("UH_AUTH_REQUIRED", "1")
    monkeypatch.setenv("UH_AUTH_TOKEN", "tok")
    monkeypatch.delenv("UH_LOCAL_MODE", raising=False)
    with TestClient(create_app()) as c:
        assert c.post("/api/v1/sessions").status_code == 401


# --- 二审 R2：401 必带 meta.trace_id ---
def test_401_has_meta_trace_id(monkeypatch):
    _auth(monkeypatch)
    with TestClient(create_app()) as c:
        r = c.post("/api/v1/sessions")
        assert r.status_code == 401 and r.json()["meta"].get("trace_id")


# --- 二审 N3：RPC 通道高危工具 fail-closed（无 confirm 不执行） ---
def test_rpc_high_risk_fail_closed():
    executed = []
    def handler(req):
        m, rid = req.get("method"), req.get("id")
        if m == "initialize":
            return {"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2024-11-05", "capabilities": {}}}
        if m == "tools/list":
            return {"jsonrpc": "2.0", "id": rid, "result": {"tools": [{"name": "danger", "inputSchema": {}}]}}
        if m == "tools/call":
            executed.append(req["params"]["name"])
            return {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": "done"}]}}
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    bus = EventBus()
    host = MCPHost(InProcessTransport(handler), bus=bus)
    rpc = CapabilityRegistry(bus, StorageGuard())
    asyncio.run(host.register_capabilities(rpc, manifest={"danger": {"risk_level": "irreversible"}}))
    resp = asyncio.run(rpc.call("mcp.tool.danger", {}))
    assert resp.payload.get("degraded") and resp.payload.get("error_code") == "UH-USR-1002"
    assert executed == []  # 高危工具未经确认绝不执行


# --- 二审 GPT-6 P0：调用方不可降级风险级绕过确认 ---
def test_risk_downgrade_blocked():
    def handler(req):
        m, rid = req.get("method"), req.get("id")
        if m == "initialize":
            return {"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2024-11-05", "capabilities": {}}}
        if m == "tools/call":
            return {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": "done"}]}}
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    host = MCPHost(InProcessTransport(handler))
    seen = []
    async def confirm(name, risk, summary, call_id):
        seen.append(risk)
        return "rejected"
    async def run():
        try:
            await host.call_tool("danger", {}, manifest={"risk_level": "irreversible"}, risk_level="read-only", confirm=confirm)
        except MCPError:
            pass
    asyncio.run(run())
    assert seen == ["irreversible"]  # 确认门以工具声明为准，未被降级


# --- 二审 N4：stream_url 不信任 Host 头 ---
def test_stream_url_no_host_injection():
    with TestClient(create_app()) as c:
        sid = c.post("/api/v1/sessions").json()["data"]["session_id"]
        h = c.post("/api/v1/sessions/" + sid + "/messages", json={"content": "hi"}, headers={"Host": "evil.example"}).json()["data"]
        assert "evil.example" not in h["stream_url"]


# --- 二审 N5：幂等回放不泄漏 Set-Cookie ---
def test_idempotency_replay_no_set_cookie():
    with TestClient(create_app()) as c:
        url = "/api/v1/sessions/demo/messages"
        hdr = {"Idempotency-Key": "k1"}
        c.post(url, json={"content": "x"}, headers=hdr)
        r2 = c.post(url, json={"content": "x"}, headers=hdr)
        assert "set-cookie" not in {k.lower() for k in r2.headers.keys()}


# --- 二审 N6：dev_route 受 dev 门禁 ---
def test_dev_route_gated(monkeypatch):
    monkeypatch.setenv("UH_DEV_MODE", "0")
    with TestClient(create_app()) as c:
        assert c.post("/api/v1/dev/route", json={"content": "hi", "mode": "auto"}).status_code == 404


# --- 二审 GPT-6：取消后确认令牌失效 ---
def test_cancel_invalidates_confirmation_token():
    from kernel.tasks import TaskManager
    from kernel.confirmation import ConfirmationManager, ConfirmationError
    async def run():
        tm = TaskManager(None, None, confirmations=ConfirmationManager(ttl_s=30))
        class _Host:
            async def call_tool(self, name, args, *, risk_level=None, confirm=None, **k):
                if confirm and risk_level in {"write-host", "irreversible"}:
                    d = confirm(name, risk_level, "s", "c1")
                    if asyncio.iscoroutine(d):
                        d = await d
                    if d != "approved":
                        raise MCPError("UH-USR-1002", "rej")
                return {"content": "x"}
        tid, _ = tm.create_tool_task("uh_ses_c1", "danger", {}, "irreversible", _Host())
        for _ in range(300):
            if tm.tasks[tid]["status"] == "pending_confirmation":
                break
            await asyncio.sleep(0.01)
        token = tm.confirmations.pending_for_task(tid)[0].token
        await tm.cancel(tid)
        try:
            tm.resolve_confirmation(tid, token, "approved")
            return False
        except ConfirmationError:
            return True
    assert asyncio.run(run())  # 取消后令牌不可再批准


# --- 二审 GPT-6 P2：L3 override 不可绕过 ---
def test_l3_override_cannot_bypass(tmp_path):
    from kernel.long_term_memory import LongTermMemory
    from kernel.retrieval import LocalVectorStore
    ltm = LongTermMemory(path=tmp_path / "m.json", vector_store=LocalVectorStore(tmp_path / "v.json"))
    assert ltm.write("身份证号 11010519491231002X", privacy_level="L1") is None
    assert ltm.entry_count == 0  # L3 强制拒入，override 无效


# --- 二审 N7：StorageGuard 真实隔离 ---
def test_storage_guard_cross_module_denied():
    from kernel.protocol import StorageGuard
    g = StorageGuard()
    g.declare("mod_a", {"ns_a"})
    g.access("mod_a", "ns_a")  # 自身命名空间放行
    with pytest.raises(PermissionError):
        g.access("mod_a", "ns_b")  # 越权他模块命名空间
    with pytest.raises(PermissionError):
        g.access("mod_b", "ns_a")


def test_storage_guard_namespace_collision():
    from kernel.protocol import StorageGuard
    g = StorageGuard()
    g.declare("mod_a", {"shared"})
    with pytest.raises(PermissionError):
        g.declare("mod_b", {"shared"})  # 命名空间不可重复声明


# --- 二审 R11：ADR-003 错误码模式上下文覆盖 ---
def test_error_mode_context_override():
    from kernel.protocol import module_error_code
    assert module_error_code("legacy") == "UH-CAP-4003"
    assert module_error_code("strict") == "UH-CAP-4004"
