import asyncio
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from kernel.main import create_app


class _SlowLLM:
    async def stream_chat(self, _messages):
        for index in range(30):
            await asyncio.sleep(0.01)
            yield f"d{index}"


def _task(client: TestClient, content: str = "hello") -> tuple[str, str]:
    session_id = client.post("/api/v1/sessions").json()["data"]["session_id"]
    data = client.post(f"/api/v1/sessions/{session_id}/messages", json={"content": content}).json()["data"]
    return data["task_id"], session_id


def test_websocket_events_replay_without_gaps():
    app = create_app()
    app.state.tasks.llm = _SlowLLM()
    with TestClient(app) as client:
        task_id, _ = _task(client)
        with client.websocket_connect(f"/api/v1/stream?task_id={task_id}") as ws:
            first = ws.receive_json()
            assert first["event"] == "task.created"
            assert first["event_id"] == 1
        with client.websocket_connect(f"/api/v1/stream?task_id={task_id}&after_event_id=1") as ws:
            replayed = []
            while True:
                event = ws.receive_json()
                replayed.append(event)
                if event["event"] in {"task.finished", "task.failed", "task.cancelled"}:
                    break
        assert replayed[0]["event_id"] == 2
        assert [event["event_id"] for event in replayed] == list(range(2, replayed[-1]["event_id"] + 1))


def test_websocket_terminal_reconnect_sends_terminal_and_closes_1000():
    with TestClient(create_app()) as client:
        task_id, _ = _task(client)
        # The SSE-compatible subscriber drains the task to its terminal state.
        with client.stream("GET", f"/api/v1/tasks/{task_id}/stream") as response:
            list(response.iter_text())
        with client.websocket_connect(f"/api/v1/stream?task_id={task_id}&after_event_id=1") as ws:
            terminal = ws.receive_json()
            assert terminal["event"] in {"task.finished", "task.failed", "task.cancelled"}
            with pytest.raises(WebSocketDisconnect) as raised:
                ws.receive_json()
            assert raised.value.code == 1000


def test_websocket_over_window_returns_replay_unavailable_4408():
    app = create_app()
    app.state.tasks.llm = _SlowLLM()
    with TestClient(app) as client:
        task_id, _ = _task(client)
        # Keep the task live while forcing its earliest retained event outside
        # the five-minute replay window.
        time.sleep(0.02)
        app.state.tasks.tasks[task_id]["event_buffer"][0]["recorded_at"] = time.time() - 301
        try:
            with client.websocket_connect(f"/api/v1/stream?task_id={task_id}&after_event_id=0") as ws:
                assert ws.receive_json() == {"op": "replay_unavailable", "reason": "window_expired"}
                with pytest.raises(WebSocketDisconnect) as raised:
                    ws.receive_json()
                assert raised.value.code == 4408
        finally:
            # The task is intentionally allowed to finish before TestClient
            # tears down its event loop.
            with client.stream("GET", f"/api/v1/tasks/{task_id}/stream") as response:
                list(response.iter_text())


def test_websocket_sec04_rejects_other_owner_and_missing_task():
    with TestClient(create_app()) as client:
        first_task, _ = _task(client, "first")
        _second_task, _ = _task(client, "second")
        with client.websocket_connect(f"/api/v1/stream?task_id={first_task}") as ws:
            error = ws.receive_json()
            assert error["op"] == "error"
            assert error["error"]["code"] == "UH-USR-1002"
            with pytest.raises(WebSocketDisconnect) as raised:
                ws.receive_json()
            assert raised.value.code == 4403
        with client.websocket_connect("/api/v1/stream?task_id=uh_task_missing") as ws:
            assert ws.receive_json()["error"]["code"] == "UH-USR-1003"
            with pytest.raises(WebSocketDisconnect) as raised:
                ws.receive_json()
        assert raised.value.code == 4404


def test_websocket_ping_and_auth_close_code(monkeypatch):
    monkeypatch.setenv("UH_WS_AUTH_REQUIRED", "true")
    monkeypatch.setenv("UH_WS_AUTH_TOKEN", "test-token")
    with TestClient(create_app()) as client:
        task_id, _ = _task(client)
        with client.websocket_connect(f"/api/v1/stream?task_id={task_id}") as ws:
            ws.send_json({"op": "ping", "ts": 123})
            assert ws.receive_json() == {"op": "pong", "ts": 123}
            ws.send_json({"op": "subscribe", "task_id": task_id})
            assert ws.receive_json()["error"]["code"] == "UH-USR-1001"
            with pytest.raises(WebSocketDisconnect) as raised:
                ws.receive_json()
            assert raised.value.code == 4401


def test_websocket_bearer_token_is_sufficient_owner_identity(monkeypatch):
    monkeypatch.setenv("UH_WS_AUTH_REQUIRED", "true")
    monkeypatch.setenv("UH_WS_AUTH_TOKEN", "owner-token")
    app = create_app()
    with TestClient(app) as creator:
        session_id = creator.post("/api/v1/sessions").json()["data"]["session_id"]
        data = creator.post(
            f"/api/v1/sessions/{session_id}/messages",
            headers={"Authorization": "Bearer owner-token"},
            json={"content": "hello"},
        ).json()["data"]
    # A separate client has no local cookie.  The authenticated bearer token
    # itself must still prove ownership of the task.
    with TestClient(app) as owner:
        with owner.websocket_connect(f"/api/v1/stream?task_id={data['task_id']}&token=owner-token") as ws:
            assert ws.receive_json()["event"] == "task.created"
