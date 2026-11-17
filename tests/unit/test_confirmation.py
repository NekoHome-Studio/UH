import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from kernel.confirmation import ConfirmationManager, ConfirmationError
from kernel.mcp import MCPHost, InProcessTransport
from kernel.tasks import TaskManager
from kernel.main import create_app


# ---------- ConfirmationManager 单测 ----------

def test_request_requires_confirm_level():
    cm = ConfirmationManager()
    with pytest.raises(ConfirmationError):
        cm.request("t", "tool", "read-only", "summary")
    req = cm.request("t", "tool", "irreversible", "删除文件")
    assert req.token and req.status == "pending" and req.task_id == "t"


def test_token_one_time_anti_replay():
    cm = ConfirmationManager()
    req = cm.request("t", "tool", "write-host", "写主机")
    cm.resolve(req.token, "approved", operator="alice")
    with pytest.raises(ConfirmationError) as ei:
        cm.resolve(req.token, "approved", operator="alice")
    assert "防重放" in ei.value.message


def test_token_task_bound():
    cm = ConfirmationManager()
    req = cm.request("task-a", "tool", "irreversible", "x")
    with pytest.raises(ConfirmationError) as ei:
        cm.resolve(req.token, "approved", task_id="task-b")
    assert ei.value.code == "UH-USR-1002"


def test_token_expiry():
    cm = ConfirmationManager(ttl_s=-1)
    req = cm.request("t", "tool", "irreversible", "x")
    with pytest.raises(ConfirmationError):
        cm.resolve(req.token, "approved")


def test_audit_records_operator_and_decision():
    cm = ConfirmationManager()
    req = cm.request("t", "danger", "irreversible", "删除")
    cm.resolve(req.token, "rejected", operator="bob")
    assert cm.audit[-1]["operator"] == "bob"
    assert cm.audit[-1]["decision"] == "rejected"
    assert cm.audit[-1]["event"] == "tool.confirm"
    assert cm.audit[-1]["tool_name"] == "danger"


# ---------- TaskManager 确认集成（mock 高危工具，追踪执行） ----------

def _mock_host(executed):
    def handler(request):
        method = request.get("method")
        rid = request.get("id")
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2024-11-05", "capabilities": {}}}
        if method == "tools/call":
            executed.append(request["params"]["name"])
            return {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": "executed"}]}}
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    return MCPHost(InProcessTransport(handler))


async def _wait_status(tm, task_id, targets):
    for _ in range(300):
        if tm.tasks[task_id]["status"] in targets:
            return
        await asyncio.sleep(0.01)


async def _drive(decision):
    executed = []
    tm = TaskManager(None, None)
    task_id, _ = tm.create_tool_task("uh_ses_test1", "danger-tool", {"x": 1}, "irreversible", _mock_host(executed))
    await _wait_status(tm, task_id, {"pending_confirmation"})
    assert tm.tasks[task_id]["status"] == "pending_confirmation"
    req = tm.confirmations.pending_for_task(task_id)[0]
    tm.resolve_confirmation(task_id, req.token, decision, operator="tester")
    await _wait_status(tm, task_id, {"finished", "failed", "cancelled"})
    events = [e["event"]["event"] for e in tm.tasks[task_id]["event_buffer"]]
    return executed, tm.tasks[task_id]["status"], events, tm.confirmations.audit


def test_approve_executes_tool_and_emits_events():
    executed, status, events, audit = asyncio.run(_drive("approved"))
    assert executed == ["danger-tool"] and status == "finished"
    assert "task.confirmation_required" in events and "task.confirmation_resolved" in events
    assert audit[-1]["decision"] == "approved" and audit[-1]["operator"] == "tester"


def test_reject_does_not_execute_and_cancels():
    executed, status, events, audit = asyncio.run(_drive("rejected"))
    assert executed == [] and status == "cancelled"
    assert "task.confirmation_required" in events and "task.cancelled" in events
    assert audit[-1]["decision"] == "rejected"


def test_reject_cancelled_reason_and_error():
    async def run():
        executed = []
        tm = TaskManager(None, None)
        task_id, _ = tm.create_tool_task("uh_ses_test1", "danger-tool", {}, "irreversible", _mock_host(executed))
        await _wait_status(tm, task_id, {"pending_confirmation"})
        req = tm.confirmations.pending_for_task(task_id)[0]
        tm.resolve_confirmation(task_id, req.token, "rejected")
        await _wait_status(tm, task_id, {"cancelled"})
        cancel = [e["event"] for e in tm.tasks[task_id]["event_buffer"] if e["event"]["event"] == "task.cancelled"][0]
        return cancel, executed
    cancel, executed = asyncio.run(run())
    assert executed == []
    assert cancel["payload"]["reason"] == "confirmation_rejected"
    assert cancel["payload"]["error"]["code"] == "UH-USR-1002"


# ---------- REST 确认端点（拒绝 + 防重放 + 审计） ----------

def test_rest_confirm_reject_and_replay():
    app = create_app()
    with TestClient(app) as c:
        sid = c.post("/api/v1/sessions").json()["data"]["session_id"]
        handle = c.post("/api/v1/dev/tool-task", json={"session_id": sid, "tool_name": "search", "arguments": {"query": "x"}, "risk_level": "irreversible"}).json()["data"]
        task_id = handle["task_id"]
        # 测试直接读 manager 取令牌（审查建议：不依赖 HTTP 泄漏端点）；
        # 生产中令牌经任务自有事件流送达归属者。
        token = None
        for _ in range(300):
            pend = app.state.confirmations.pending_for_task(task_id)
            if pend:
                token = pend[0].token
                break
            time.sleep(0.01)
        assert token
        # dev 端点已脱敏，不再返回完整令牌
        confs = c.get("/api/v1/dev/confirmations?task_id=" + task_id).json()["data"]["confirmations"]
        assert confs and "redacted" in confs[0]["confirmation_token"]
        r = c.post("/api/v1/tasks/" + task_id + "/confirm", json={"confirmation_token": token, "decision": "rejected", "operator": "rest-user"})
        assert r.status_code == 200 and r.json()["data"]["decision"] == "rejected"
        st = None
        for _ in range(300):
            st = c.get("/api/v1/tasks/" + task_id).json()["data"]["status"]
            if st == "cancelled":
                break
            time.sleep(0.01)
        assert st == "cancelled"
        r2 = c.post("/api/v1/tasks/" + task_id + "/confirm", json={"confirmation_token": token, "decision": "approved"})
        assert r2.status_code == 400  # 一次性防重放
        audit = c.get("/api/v1/dev/confirmations?task_id=" + task_id).json()["data"]["audit"]
        assert any(a["operator"] == "rest-user" and a["decision"] == "rejected" for a in audit)
