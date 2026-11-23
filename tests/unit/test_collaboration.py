import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import fakeredis
import fakeredis.aioredis
import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import BaseModel

from kernel.collaboration import CollaborationManager, CrewAIWorker
from kernel.main import create_app
from kernel.protocol import CapabilityRegistry, EventBus, StorageGuard
from kernel.scaler import CrewAIScaler, ScaleResult


def _subscribe(bus):
    events = []

    async def collect(event):
        events.append(event)

    bus.subscribe("collaboration.worker.*", collect)
    return events


def _configure(*, scaler=None, server=None, worker=None, timeout=30):
    bus = EventBus()
    guard = StorageGuard()
    registry = CapabilityRegistry(bus, guard)
    manager = CollaborationManager(
        scaler=scaler, bus=bus, redis=fakeredis.aioredis.FakeRedis(server=server, decode_responses=True),
        worker=worker or CrewAIWorker("http://uh-crewai:8080"), startup_timeout_s=timeout,
    )
    if scaler is not None and worker is None:
        manager.worker.wait_ready = AsyncMock()
    registry.register("collaboration.run", manager.run, storage={"crewai"})
    return manager, registry, _subscribe(bus)


def _call(registry, task_id="one", trace_id="trace-one"):
    return registry.call("collaboration.run", {"task_id": task_id, "content": "团队协作"}, trace_id=trace_id)


def test_real_task_entry_routes_collaboration_and_returns_worker_answer(monkeypatch):
    async def scenario():
        app = create_app()
        manager = app.state.collaboration
        events = _subscribe(app.state.bus)
        scale = AsyncMock(return_value=ScaleResult("ok"))
        run = AsyncMock(return_value="来自 CrewAI 的答案")
        monkeypatch.setattr(app.state.scaler, "scale", scale)
        monkeypatch.setattr(manager.worker, "wait_ready", AsyncMock())
        monkeypatch.setattr(manager.worker, "run", run)

        assert await manager.redis.zcard("uh:collaboration:active") == 0
        chat_id, _ = app.state.tasks.create("uh_ses_collaboration", "hello")
        await app.state.tasks.tasks[chat_id]["_runner"]
        scale.assert_not_called()

        task_id, trace_id = app.state.tasks.create("uh_ses_collaboration", "请团队协作完成")
        task = app.state.tasks.tasks[task_id]
        await task["_runner"]
        await manager.close()

        assert task["task_type"] == "collaborate"
        assert task["events"][-1]["event"]["payload"]["final_result"]["content"] == "来自 CrewAI 的答案"
        assert scale.await_args_list == [call("uh-crewai", 1, trace_id=trace_id), call("uh-crewai", 0, trace_id=trace_id)]
        assert run.await_args.args[0].action == "collaboration.run"
        assert run.await_args.args[0].trace_id == trace_id
        assert await manager.redis.zcard("uh:collaboration:active") == 0
        assert [event.action for event in events] == [
            "collaboration.worker.scaled_up", "collaboration.worker.ready", "collaboration.worker.scaled_down",
        ]
        assert all(event.trace_id == trace_id and event.payload["task_id"] == task_id for event in events)

    asyncio.run(scenario())


def test_two_kernel_replicas_share_startup_and_protect_last_task(monkeypatch):
    async def scenario():
        scaler = CrewAIScaler()
        scale = AsyncMock(return_value=ScaleResult("ok"))
        monkeypatch.setattr(scaler, "scale", scale)
        server = fakeredis.FakeServer()
        first, first_rpc, _ = _configure(scaler=scaler, server=server)
        second, second_rpc, second_events = _configure(scaler=scaler, server=server)
        started = [asyncio.Event(), asyncio.Event()]
        finish = [asyncio.Event(), asyncio.Event()]

        async def work(req):
            index = {"one": 0, "two": 1}[req.payload["task_id"]]
            started[index].set()
            await finish[index].wait()
            return "done"

        monkeypatch.setattr(first.worker, "run", work)
        monkeypatch.setattr(second.worker, "run", work)
        one = asyncio.create_task(_call(first_rpc))
        two = asyncio.create_task(_call(second_rpc, "two", "trace-two"))
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started)), 3)
        assert scale.await_count == 1
        assert scale.await_args.args == ("uh-crewai", 1)

        finish[0].set()
        assert (await one).payload == {"answer": "done"}
        await first.close()
        assert scale.await_count == 1

        finish[1].set()
        assert (await two).payload == {"answer": "done"}
        await second.close()
        assert scale.await_args_list[-1] == call("uh-crewai", 0, trace_id="trace-two")
        assert second_events[-1].action == "collaboration.worker.scaled_down"

    asyncio.run(scenario())


def test_readiness_timeout_is_audited_and_chat_fallback_finishes(monkeypatch, caplog):
    async def scenario():
        app = create_app()
        manager = app.state.collaboration
        assert manager.startup_timeout_s == 30
        manager.startup_timeout_s = 0.3
        app.state.scaler.timeout_s = 0.03
        app.state.scaler.poll_interval_s = 0.005
        events = _subscribe(app.state.bus)
        patches = []

        def kube_api(request):
            if request.method == "PATCH":
                patches.append(int(b'"replicas":1' in request.content))
                return httpx.Response(200, json={})
            return httpx.Response(200, json={
                "spec": {"replicas": patches[-1]}, "metadata": {"generation": 1},
                "status": {"observedGeneration": 1, "replicas": patches[-1], "readyReplicas": 0},
            })

        monkeypatch.setattr(app.state.scaler, "_client", lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(kube_api), base_url="https://kube.invalid",
        ))
        worker = AsyncMock()
        monkeypatch.setattr(manager.worker, "run", worker)
        task_id, trace_id = app.state.tasks.create("uh_ses_collaboration", "团队讨论")
        task = app.state.tasks.tasks[task_id]
        await task["_runner"]
        await manager.close()

        assert patches == [1, 0]
        worker.assert_not_called()
        emitted = [entry["event"] for entry in task["events"]]
        assert emitted[-1]["event"] == "task.finished"
        assert next(e for e in emitted if e["event"] == "task.degraded")["payload"]["reason_code"] == "UH-CAP-5021"
        assert [event.action for event in events] == ["collaboration.worker.timeout", "collaboration.worker.scaled_down"]
        assert all(event.trace_id == trace_id for event in events)
        assert any(r.event == "audit.collaboration.result" and r.trace_id == trace_id and r.fields["reason"] == "timeout"
                   for r in caplog.records if r.name == "kernel.collaboration")

    asyncio.run(scenario())


def test_slow_scaler_is_bounded_without_waiting_for_cleanup(monkeypatch):
    async def scenario():
        scaler = CrewAIScaler()
        cleanup_started = asyncio.Event()
        finish_cleanup = asyncio.Event()

        async def scale(target, replicas, *, trace_id):
            if replicas:
                await asyncio.Event().wait()
            cleanup_started.set()
            await finish_cleanup.wait()
            return ScaleResult("ok")

        monkeypatch.setattr(scaler, "scale", scale)
        manager, registry, events = _configure(scaler=scaler, timeout=0.15)
        result = await asyncio.wait_for(_call(registry), 1)
        assert result.payload["degraded"] and result.payload["reason"] == "timeout"
        await asyncio.wait_for(cleanup_started.wait(), 1)
        assert events[0].action == "collaboration.worker.timeout"
        finish_cleanup.set()
        await manager.close()

    asyncio.run(scenario())


def test_worker_rpc_runs_two_agents_without_shared_storage(monkeypatch):
    class BaseLLM(BaseModel):
        model: str
        temperature: float

    class Crew:
        def __init__(self, *, agents, tasks, memory, cache, tracing):
            assert [agent.role for agent in agents] == ["Analyst", "Writer"]
            assert tasks[1].context == [tasks[0]]
            assert not any((memory, cache, tracing))
            self.agents = agents

        async def akickoff(self, *, inputs):
            assert inputs == {"content": "团队协作"}
            answer = await self.agents[-1].llm.acall([])
            return SimpleNamespace(raw=answer.removeprefix("Final Answer: "))

    monkeypatch.setenv("UH_LLM_PROVIDER", "mock")
    monkeypatch.setitem(sys.modules, "crewai", SimpleNamespace(
        Agent=SimpleNamespace, Task=SimpleNamespace, Crew=Crew, BaseLLM=BaseLLM, LLM=None,
    ))
    path = Path(__file__).resolve().parents[2] / "kernel" / "crewai_worker.py"
    spec = importlib.util.spec_from_file_location("test_crewai_worker", path)
    worker = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, worker)
    spec.loader.exec_module(worker)

    with TestClient(worker.app) as client:
        assert client.get("/ready").status_code == 200
        reply = client.post("/rpc/collaboration.run", json={
            "trace_id": "trace-one", "payload": {"task_id": "one", "content": "团队协作"},
        })
    assert reply.status_code == 200 and reply.json() == {"answer": "协作结果（mock）：团队协作"}


@pytest.mark.parametrize("outcome", ["failed", "cancelled"])
def test_last_task_releases_worker_on_failure_or_cancellation(monkeypatch, outcome):
    async def scenario():
        scaler = CrewAIScaler()
        scale = AsyncMock(return_value=ScaleResult("ok"))
        monkeypatch.setattr(scaler, "scale", scale)
        manager, registry, events = _configure(scaler=scaler)
        started = asyncio.Event()

        async def run(req):
            started.set()
            if outcome == "failed":
                raise httpx.ConnectError("worker offline")
            await asyncio.Event().wait()

        monkeypatch.setattr(manager.worker, "run", run)
        task = asyncio.create_task(_call(registry))
        await asyncio.wait_for(started.wait(), 2)
        if outcome == "cancelled":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert (await task).payload["reason"] == "worker_unavailable"
        await manager.close()
        assert [args.args[1] for args in scale.await_args_list] == [1, 0]
        assert events[-1].action == "collaboration.worker.scaled_down"

    asyncio.run(scenario())


def test_no_cluster_degrades_without_executing_worker(monkeypatch):
    async def scenario():
        manager, registry, events = _configure(scaler=CrewAIScaler())
        worker = AsyncMock()
        monkeypatch.setattr(manager.worker, "run", worker)
        result = await _call(registry)
        await manager.close()
        assert result.payload["degraded"] and result.payload["reason"] == "cluster_unavailable"
        assert events[0].action == "collaboration.worker.degraded"
        worker.assert_not_called()

    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.delenv("UH_K8S_API_HOST", raising=False)
    asyncio.run(scenario())


def test_reconciler_recovers_expired_lease_after_kernel_process_loss(monkeypatch):
    async def scenario():
        scaler = CrewAIScaler()
        scale = AsyncMock(return_value=ScaleResult("ok"))
        monkeypatch.setattr(scaler, "scale", scale)
        manager, _, events = _configure(scaler=scaler)
        await manager.redis.zadd("uh:collaboration:active", {"expired-lease": 1})
        await manager.redis.hset("uh:collaboration:worker", mapping={
            "ready": "1", "trace_id": "lost-trace", "task_id": "lost-task",
        })
        reconciled = asyncio.Event()

        async def signal(event):
            reconciled.set()

        manager.bus.subscribe("collaboration.worker.scaled_down", signal)
        manager.start()
        await asyncio.wait_for(reconciled.wait(), 2)
        await manager.close()
        scale.assert_awaited_once_with("uh-crewai", 0, trace_id="lost-trace")
        assert events[0].payload["task_id"] == "lost-task"
        assert not await manager.redis.exists("uh:collaboration:worker", "uh:collaboration:active")

    asyncio.run(scenario())


def test_storage_guard_and_compose_worker_network():
    app = create_app()
    guard = app.state.guard
    guard.access("collaboration.run", "crewai")
    for namespace in ("retrieval", "memory", "long_term", "orchestration", "checkpoint"):
        with pytest.raises(PermissionError, match="UH-KRN-2010"):
            guard.access("collaboration.run", namespace)

    root = Path(__file__).resolve().parents[2]
    services = yaml.safe_load((root / "docker-compose.yaml").read_text(encoding="utf-8"))["services"]
    worker = services["uh-crewai"]
    assert worker["profiles"] == ["collaborate"]
    assert "/ready" in worker["healthcheck"]["test"][-1]
    assert not {"volumes", "secrets", "ports", "depends_on"}.intersection(worker)
    assert {name for name, service in services.items() if "collaboration" in service.get("networks", [])} == {
        "uh-kernel", "uh-crewai",
    }


def test_compose_readiness_poll_and_rpc_share_private_worker(monkeypatch):
    client = httpx.AsyncClient
    requests = []

    def worker_api(request):
        requests.append(request.url.path)
        if request.url.path == "/ready":
            return httpx.Response(503 if len(requests) == 1 else 200)
        assert b'"trace_id":"trace-one"' in request.content
        assert b'"action":"collaboration.run"' in request.content
        return httpx.Response(200, json={"answer": "done"})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **options: client(
        transport=httpx.MockTransport(worker_api), **options,
    ))

    async def scenario():
        manager, registry, events = _configure()
        assert (await _call(registry)).payload == {"answer": "done"}
        await manager.close()
        assert requests == ["/ready", "/ready", "/rpc/collaboration.run"]
        assert events[0].action == "collaboration.worker.ready"

    asyncio.run(scenario())
