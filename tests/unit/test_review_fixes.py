"""M1 审查修复回归测试（先红后绿）。"""
import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from kernel.main import create_app
from kernel.tasks import TaskManager
from kernel.confirmation import ConfirmationManager
from kernel.retrieval import _read_document
from kernel.mcp import MCPError


class _SlowLLM:
    async def stream_chat(self, _messages):
        for i in range(30):
            await asyncio.sleep(0.1)
            yield f"d{i}"


def _chat_task(client, content="你好"):
    sid = client.post("/api/v1/sessions").json()["data"]["session_id"]
    return client.post(f"/api/v1/sessions/{sid}/messages", json={"content": content}).json()["data"]


# --- WS 空 token 绕过修复 ---
def test_ws_rejects_arbitrary_token_when_no_expected(monkeypatch):
    monkeypatch.setenv("UH_WS_AUTH_REQUIRED", "1")
    monkeypatch.delenv("UH_WS_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("UH_WS_TOKEN", raising=False)
    monkeypatch.delenv("UH_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("UH_LOCAL_MODE", raising=False)
    with TestClient(create_app()) as c:
        h = _chat_task(c)
        with c.websocket_connect("/api/v1/stream?task_id=" + h["task_id"] + "&token=attacker") as ws:
            err = ws.receive_json()
            assert err["error"]["code"] == "UH-USR-1001"
            with pytest.raises(WebSocketDisconnect) as ei:
                ws.receive_json()
            assert ei.value.code == 4401


# --- WS auth 后实时事件修复 ---
def test_ws_auth_then_receives_live_events(monkeypatch):
    monkeypatch.setenv("UH_WS_AUTH_REQUIRED", "1")
    monkeypatch.setenv("UH_WS_AUTH_TOKEN", "tok")
    monkeypatch.delenv("UH_LOCAL_MODE", raising=False)
    app = create_app()
    app.state.tasks.llm = _SlowLLM()
    with TestClient(app) as c:
        h = _chat_task(c)
        with c.websocket_connect("/api/v1/stream?task_id=" + h["task_id"]) as ws:
            ws.send_json({"op": "auth", "token": "tok"})
            got_delta = False
            for _ in range(60):
                ev = ws.receive_json()
                if ev.get("event") == "delta":
                    got_delta = True
                    break
                if ev.get("event") in {"task.finished", "task.failed", "task.cancelled"}:
                    break
            assert got_delta  # 鉴权后必须收到实时增量


# --- 确认 TTL 自动超时 ---
def test_confirmation_auto_timeout_cancels():
    async def run():
        tm = TaskManager(None, None, confirmations=ConfirmationManager(ttl_s=0.1))
        class _Host:
            async def call_tool(self, name, args, *, risk_level=None, confirm=None, **k):
                if confirm is not None and risk_level in {"write-host", "irreversible"}:
                    d = confirm(name, risk_level, "summary", "call_1")
                    if asyncio.iscoroutine(d):
                        d = await d
                    if d != "approved":
                        raise MCPError("UH-USR-1002", "rejected")
                return {"content": "executed"}
        tid, _ = tm.create_tool_task("uh_ses_t1", "danger", {}, "irreversible", _Host())
        for _ in range(300):
            if tm.tasks[tid]["status"] in {"cancelled", "finished", "failed"}:
                break
            await asyncio.sleep(0.01)
        evs = [e["event"] for e in tm.tasks[tid]["event_buffer"]]
        return tm.tasks[tid]["status"], evs
    status, evs = asyncio.run(run())
    assert status == "cancelled"
    resolved = [e for e in evs if e["event"] == "task.confirmation_resolved"][0]
    assert resolved["payload"]["resolved_by"] == "timeout"
    cancel = [e for e in evs if e["event"] == "task.cancelled"][0]
    assert cancel["payload"]["reason"] == "timeout"


# --- 非 MCPError 异常送达终态 ---
def test_non_mcp_error_reaches_terminal():
    async def run():
        tm = TaskManager(None, None)
        class _Host:
            async def call_tool(self, *a, **k):
                raise RuntimeError("boom")
        tid, _ = tm.create_tool_task("uh_ses_t2", "tool", {}, "read-only", _Host())
        for _ in range(300):
            if tm.tasks[tid]["status"] in {"finished", "failed", "cancelled"}:
                break
            await asyncio.sleep(0.01)
        return tm.tasks[tid]["status"]
    assert asyncio.run(run()) == "failed"


# --- steps 端点（C8 seq） ---
def test_steps_endpoint_seq():
    with TestClient(create_app()) as c:
        h = _chat_task(c, "按步骤制定一个计划")
        h2 = c.post("/api/v1/sessions/" + c.post("/api/v1/sessions").json()["data"]["session_id"] + "/messages", json={"content": "计划", "mode": "workflow"}).json()["data"]
        with c.stream("GET", h2["sse_url"]) as r:
            "".join(r.iter_text())
        steps = c.get("/api/v1/tasks/" + h2["task_id"] + "/steps").json()["data"]["steps"]
        assert len(steps) == 4 and all("seq" in s for s in steps)
        assert c.get("/api/v1/tasks/uh_task_nonexistent/steps").status_code == 404


# --- cancel 端点 ---
def test_cancel_endpoint_and_terminal_conflict():
    app = create_app()
    app.state.tasks.llm = _SlowLLM()
    with TestClient(app) as c:
        h = _chat_task(c)
        r = c.post("/api/v1/tasks/" + h["task_id"] + "/cancel")
        assert r.status_code == 200 and r.json()["data"]["reason"] == "user_request"
        for _ in range(200):
            st = c.get("/api/v1/tasks/" + h["task_id"]).json()["data"]["status"]
            if st == "cancelled":
                break
            time.sleep(0.01)
        assert st == "cancelled"
        assert c.post("/api/v1/tasks/" + h["task_id"] + "/cancel").status_code == 409


# --- 幂等绑 body ---
def test_idempotency_key_bound_to_body():
    with TestClient(create_app()) as c:
        sid = c.post("/api/v1/sessions").json()["data"]["session_id"]
        url = f"/api/v1/sessions/{sid}/messages"
        a = c.post(url, json={"content": "甲"}, headers={"Idempotency-Key": "k1"}).json()["data"]
        b = c.post(url, json={"content": "乙"}, headers={"Idempotency-Key": "k1"}).json()["data"]
        assert a["task_id"] != b["task_id"]  # 同键不同 body 不得复用
        a2 = c.post(url, json={"content": "甲"}, headers={"Idempotency-Key": "k1"}).json()["data"]
        assert a2["task_id"] == a["task_id"]  # 同键同 body 回放


# --- bus→task 桥：tool.call/result 真实发射 ---
def test_tool_events_emitted_via_bus_bridge():
    from kernel.protocol import EventBus
    from kernel.mcp import MCPHost, InProcessTransport
    async def run():
        bus = EventBus()
        tm = TaskManager(None, None, bus=bus)
        executed = []
        def handler(req):
            m, rid = req.get("method"), req.get("id")
            if m == "initialize":
                return {"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2024-11-05", "capabilities": {}}}
            if m == "tools/call":
                executed.append(req["params"]["name"])
                return {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": "ok"}]}}
            return {"jsonrpc": "2.0", "id": rid, "result": {}}
        host = MCPHost(InProcessTransport(handler), bus=bus)
        tid, _ = tm.create_tool_task("uh_ses_b1", "safe-tool", {}, "read-only", host)
        for _ in range(300):
            if tm.tasks[tid]["status"] in {"finished", "failed", "cancelled"}:
                break
            await asyncio.sleep(0.01)
        return [e["event"]["event"] for e in tm.tasks[tid]["event_buffer"]], executed
    evs, executed = asyncio.run(run())
    assert executed == ["safe-tool"]
    assert "tool.call" in evs and "tool.result" in evs


# --- 路径白名单 ---
def test_index_path_whitelist(tmp_path, monkeypatch):
    monkeypatch.setenv("UH_RETRIEVAL_ROOT", str(tmp_path))
    inside = tmp_path / "ok.md"
    inside.write_text("合法文档", encoding="utf-8")
    text, _, _ = _read_document(inside)
    assert "合法文档" in text
    outside = tmp_path.parent / "secret.md"
    with pytest.raises(ValueError):
        _read_document(outside)


# --- REST bearerAuth（审查 B5，本地单用户豁免） ---
def _auth_env(monkeypatch, *, local="0", required="1", token="secret"):
    monkeypatch.setenv("UH_LOCAL_MODE", local)
    monkeypatch.setenv("UH_AUTH_REQUIRED", required)
    if token is None:
        for k in ("UH_AUTH_TOKEN", "UH_WS_AUTH_TOKEN", "UH_WS_TOKEN"):
            monkeypatch.delenv(k, raising=False)
    else:
        monkeypatch.setenv("UH_AUTH_TOKEN", token)


def test_rest_auth_rejects_missing_token(monkeypatch):
    _auth_env(monkeypatch)
    with TestClient(create_app()) as c:
        r = c.post("/api/v1/sessions")
        assert r.status_code == 401 and r.json()["error"]["code"] == "UH-USR-1001"


def test_rest_auth_valid_token_passes(monkeypatch):
    _auth_env(monkeypatch)
    with TestClient(create_app()) as c:
        # 经 ?token= 查询参数（与 WS 一致）；TestClient 传输层会掩码 Authorization 头
        r = c.post("/api/v1/sessions?token=secret")
        assert r.status_code == 201


def test_rest_auth_wrong_token_rejected(monkeypatch):
    _auth_env(monkeypatch)
    with TestClient(create_app()) as c:
        r = c.post("/api/v1/sessions", headers={"Authorization": "***"})
        assert r.status_code == 401


def test_rest_auth_no_expected_token_rejects_all(monkeypatch):
    _auth_env(monkeypatch, token=None)
    with TestClient(create_app()) as c:
        r = c.post("/api/v1/sessions", headers={"Authorization": "***"})
        assert r.status_code == 401  # 未配期望 token 不得放行任意 token


def test_rest_local_mode_exempt(monkeypatch):
    monkeypatch.setenv("UH_LOCAL_MODE", "1")
    monkeypatch.setenv("UH_AUTH_REQUIRED", "1")
    monkeypatch.setenv("UH_AUTH_TOKEN", "secret")
    with TestClient(create_app()) as c:
        assert c.post("/api/v1/sessions").status_code == 201  # 本地单用户豁免


def test_rest_ops_exempt_when_auth_required(monkeypatch):
    _auth_env(monkeypatch)
    with TestClient(create_app()) as c:
        assert c.get("/health").status_code == 200  # ops 豁免
        assert c.get("/ready").status_code == 200
