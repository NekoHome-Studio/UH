import asyncio
import json

import httpx
from fastapi.testclient import TestClient

from kernel.llm import MockLLMClient, create_llm_client
from kernel.main import create_app


def test_ollama_compatible_url_and_sse(monkeypatch):
    monkeypatch.setenv("UH_LLM_BASE_URL", "http://localhost:11434/v1/")
    monkeypatch.setenv("UH_LLM_PROVIDER", "openai")
    monkeypatch.setenv("UH_LLM_API_KEY", "ollama")  # Ollama ignores this placeholder.
    monkeypatch.setenv("UH_LLM_MODEL", "test-local-model")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    real_client = httpx.AsyncClient
    seen = []

    def respond(request):
        seen.append(request)
        assert request.method == "POST"
        assert str(request.url) == "http://localhost:11434/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer ollama"
        assert json.loads(request.content) == {
            "model": "test-local-model",
            "messages": [{"role": "user", "content": "offline"}],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=(
            'data: {"choices":[{"delta":{"role":"assistant","content":""}}],"usage":null}\n\n'
            'data: {"choices":[{"delta":{"content":"local "}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"reply"}}]}\n\n'
            'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2}}\n\n'
            'data: [DONE]\n\n'
        ))

    def transport_client(**options):
        assert options["trust_env"] is False
        return real_client(transport=httpx.MockTransport(respond), **options)

    monkeypatch.setattr(httpx, "AsyncClient", transport_client)

    async def chat():
        client = create_llm_client()
        try:
            assert await client.chat([{"role": "user", "content": "offline"}]) == "local reply"
            assert client.uh_token_usage_total["test-local-model"] == {"prompt_tokens": 3, "completion_tokens": 2}
        finally:
            await client.aclose()

    asyncio.run(chat())
    assert len(seen) == 1


def test_mock_chat_completes_over_websocket_without_http_egress(monkeypatch):
    monkeypatch.setenv("UH_LLM_PROVIDER", "mock")
    monkeypatch.delenv("UH_LLM_API_KEY", raising=False)
    monkeypatch.setenv("UH_LOCAL_MODE", "1")
    outbound = []

    async def reject_outbound(self, request):
        outbound.append(str(request.url))
        raise AssertionError("Offline chat made an outbound HTTP request")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", reject_outbound)
    app = create_app()
    prompt = "你好，离线聊天"
    with TestClient(app, base_url="http://127.0.0.1:8080") as desktop:
        assert isinstance(app.state.llm, MockLLMClient)
        assert desktop.get("/?desktop=1").status_code == 200
        session = desktop.post("/api/v1/sessions").json()["data"]["session_id"]
        response = desktop.post(f"/api/v1/sessions/{session}/messages", json={"content": prompt, "mode": "auto"})
        assert response.status_code == 202
        task_id = response.json()["data"]["task_id"]
        with desktop.websocket_connect(f"ws://127.0.0.1:8080/api/v1/stream?task_id={task_id}&after_event_id=0") as events:
            events.send_json({"op": "subscribe", "task_id": task_id, "after_event_id": 0})
            deltas = []
            while True:
                event = events.receive_json()
                assert "event" in event, event
                if event["event"] in {"task.finished", "task.failed", "task.cancelled"}:
                    break
                if event["event"] == "delta":
                    deltas.append(event["payload"]["text"])
        assert event["event"] == "task.finished"
        reply = event["payload"]["final_result"]["content"]
        assert "mock provider" in reply and prompt in reply
        assert "".join(deltas) == reply
    assert outbound == []
