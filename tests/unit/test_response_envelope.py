from fastapi.testclient import TestClient

from kernel.main import create_app


def test_business_success_has_envelope_and_request_trace():
    client = TestClient(create_app())
    response = client.post("/api/v1/sessions", headers={"X-Trace-Id": "uh_trc_" + "ab" * 16})

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"data", "meta", "error"}
    assert body["meta"]["trace_id"] == response.headers["X-Trace-Id"]
    assert body["error"] is None


def test_business_failure_has_contract_error_fields():
    client = TestClient(create_app())
    response = client.get("/api/v1/tasks/uh_task_missing")

    assert response.status_code == 404
    body = response.json()
    assert body["data"] is None
    assert body["meta"]["trace_id"] == response.headers["X-Trace-Id"]
    assert set(body["error"]) == {"code", "message", "retryable", "retry_after_ms", "detail"}
    assert body["error"]["code"] == "UH-USR-1003"


def test_unhandled_exception_maps_to_kernel_error():
    app = create_app()

    def explode(task_id):
        raise RuntimeError("boom")

    app.state.tasks.status = explode
    response = TestClient(app).get("/api/v1/tasks/uh_task_broken")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "UH-KRN-5001"


def test_idempotency_replays_first_result_without_repeating_side_effect():
    app = create_app()
    calls = 0

    def fake_create(*args, **kwargs):
        nonlocal calls
        calls += 1
        return "uh_task_once", "uh_trc_" + "cd" * 16

    app.state.tasks.create = fake_create
    app.state.sessions.add("demo")
    client = TestClient(app)
    headers = {"Idempotency-Key": "message-once"}
    first = client.post("/api/v1/sessions/demo/messages", json={"content": "hello"}, headers=headers)
    second = client.post("/api/v1/sessions/demo/messages", json={"content": "hello"}, headers=headers)

    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    assert calls == 1


def test_message_pagination_reports_total_and_has_more():
    app = create_app()

    async def fake_context(session_id):
        return [
            {"role": "user", "content": "one", "ts": 1},
            {"role": "assistant", "content": "two", "ts": 2},
            {"role": "user", "content": "three", "ts": 3},
        ]

    app.state.memory.get_context = fake_context
    app.state.sessions.add("demo")
    response = TestClient(app).get("/api/v1/sessions/demo/messages?page=2&page_size=2")

    assert response.status_code == 200
    page = response.json()["data"]
    assert page["total"] == 3
    assert page["has_more"] is False
    assert [item["content"] for item in page["messages"]] == ["three"]


def test_ops_endpoints_remain_plain():
    client = TestClient(create_app())
    body = client.get("/health").json()

    assert body["status"] == "ok"
    assert "data" not in body
