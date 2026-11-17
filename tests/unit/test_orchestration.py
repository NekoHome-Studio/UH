import asyncio

import pytest

from kernel.main import create_app
from kernel.orchestration import JSONCheckpointStore, SQLiteCheckpointStore, OrchestrationError, OrchestrationService
from fastapi.testclient import TestClient


def run(coro):
    return asyncio.run(coro)


def test_workflow_nodes_run_in_order_and_checkpoint(tmp_path):
    seen = []

    def make(name):
        async def node(state):
            seen.append(name)
            return {name: True}
        return node

    service = OrchestrationService(nodes={n: make(n) for n in OrchestrationService.NODE_ORDER}, store=JSONCheckpointStore(tmp_path / "checkpoints.json"))
    result = run(service.run("task-1", "hello"))
    checkpoint = service.checkpoint("task-1")
    assert seen == ["plan", "retrieve", "generate", "verify"]
    assert result["completed_nodes"] == list(OrchestrationService.NODE_ORDER)
    assert checkpoint and checkpoint.status == "completed"


def test_resume_skips_completed_nodes_and_survives_restart(tmp_path):
    counts = {n: 0 for n in OrchestrationService.NODE_ORDER}
    fail_once = {"retrieve": True}

    def make(name):
        async def node(state):
            counts[name] += 1
            if name == "retrieve" and fail_once.pop(name, False):
                raise RuntimeError("interrupted")
            return {name: True}
        return node

    store = SQLiteCheckpointStore(tmp_path / "checkpoints.sqlite3")
    first = OrchestrationService(nodes={n: make(n) for n in counts}, store=store)
    with pytest.raises(OrchestrationError):
        run(first.run("task-2", "hello"))
    # 新实例读取同一个 checkpoint，plan 不会重复执行。
    second = OrchestrationService(nodes={n: make(n) for n in counts}, store=SQLiteCheckpointStore(tmp_path / "checkpoints.sqlite3"))
    result = run(second.run("task-2", "hello", resume=True))
    assert result["completed_nodes"] == list(OrchestrationService.NODE_ORDER)
    assert counts["plan"] == 1 and counts["retrieve"] == 2


def test_deadline_timeout_maps_cap_5021(tmp_path):
    async def slow(state):
        await asyncio.sleep(0.05)

    nodes = {n: slow for n in OrchestrationService.NODE_ORDER}
    service = OrchestrationService(nodes=nodes, store=JSONCheckpointStore(tmp_path / "checkpoints.json"))
    with pytest.raises(OrchestrationError) as caught:
        run(service.run("task-3", "hello", deadline_ms=5))
    assert caught.value.code == "UH-CAP-5021"
    assert caught.value.detail["module"] == "orchestration"


def test_workflow_resume_endpoint_and_events():
    app = create_app()
    with TestClient(app) as client:
        sid = client.post("/api/v1/sessions").json()["data"]["session_id"]
        handle = client.post("/api/v1/sessions/" + sid + "/messages", json={"content": "按步骤制定一个计划", "mode": "workflow"}).json()["data"]
        with client.stream("GET", handle["sse_url"]) as response:
            body = "".join(response.iter_text())
        events = [line for line in body.split("\n\n") if line.startswith("data: ")]
        names = [__import__("json").loads(line[6:])["event"] for line in events]
        assert names.count("step.started") == 4 and "task.finished" in names
        # 终态唯一性（审查 P0-2）：已完成任务 resume 须 409，不得重发终态事件
        resumed = client.post("/api/v1/tasks/" + handle["task_id"] + "/resume", json={})
        assert resumed.status_code == 409
        assert resumed.json()["error"]["code"] == "UH-USR-1003"
        buffer = app.state.tasks.tasks[handle["task_id"]]["event_buffer"]
        terminals = [e["event"]["event"] for e in buffer if e["event"]["event"] in {"task.finished", "task.failed", "task.cancelled"}]
        assert len(terminals) == 1  # 终态仅一次


def test_disabling_orchestration_degrades_workflow_but_chat_completes():
    app = create_app()
    with TestClient(app) as client:
        client.post("/api/v1/dev/capability/orchestration.run/toggle?available=false")
        sid = client.post("/api/v1/sessions").json()["data"]["session_id"]
        handle = client.post("/api/v1/sessions/" + sid + "/messages", json={"content": "按步骤制定一个计划", "mode": "workflow"}).json()["data"]
        with client.stream("GET", handle["sse_url"]) as response:
            body = "".join(response.iter_text())
        names = [__import__("json").loads(line[6:])["event"] for line in body.split("\n\n") if line.startswith("data: ")]
        assert "task.degraded" in names and "task.finished" in names
