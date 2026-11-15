import asyncio
import json

import httpx
import pytest

from kernel.llm import LLMClient, LLMError


def _sse(chunks, usage=None):
    lines = []
    for c in chunks:
        lines.append("data: " + json.dumps({"choices": [{"delta": {"content": c}}]}) + "\n\n")
    if usage:
        lines.append("data: " + json.dumps({"choices": [], "usage": usage}) + "\n\n")
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode("utf-8")


def _client(handler, max_retries=3):
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, base_url="http://test")
    return LLMClient(base_url="http://test", api_key="test-key", model="test-model",
                     max_retries=max_retries, backoff_base_s=0, client=http)


def test_stream_complete():
    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=_sse(["Hello", " ", "world"], usage={"prompt_tokens": 10, "completion_tokens": 3}))
    c = _client(handler)
    text = asyncio.run(c.chat([{"role": "user", "content": "hi"}]))
    assert text == "Hello world"
    assert c.uh_token_usage_total["test-model"] == {"prompt_tokens": 10, "completion_tokens": 3}


def test_timeout_retry_then_fail():
    calls = {"n": 0}
    def handler(request):
        calls["n"] += 1
        raise httpx.ReadTimeout("timeout", request=request)
    c = _client(handler, max_retries=2)
    with pytest.raises(LLMError) as ei:
        asyncio.run(c.chat([{"role": "user", "content": "hi"}]))
    assert ei.value.code == "UH-CAP-4003" and ei.value.retryable is True
    assert calls["n"] == 3  # 初始 + 2 次重试


def test_429_maps_cap_4002():
    def handler(request):
        return httpx.Response(429, headers={"retry-after": "1"}, json={"error": {"message": "rate limited"}})
    c = _client(handler, max_retries=0)
    with pytest.raises(LLMError) as ei:
        asyncio.run(c.chat([{"role": "user", "content": "hi"}]))
    assert ei.value.code == "UH-CAP-4002"
    assert ei.value.retryable is True and ei.value.retry_after_ms == 1000


def test_400_token_maps_cap_4001_no_retry():
    calls = {"n": 0}
    def handler(request):
        calls["n"] += 1
        return httpx.Response(400, json={"error": {"message": "maximum context length exceeded"}})
    c = _client(handler, max_retries=3)
    with pytest.raises(LLMError) as ei:
        asyncio.run(c.chat([{"role": "user", "content": "hi"}]))
    assert ei.value.code == "UH-CAP-4001" and ei.value.retryable is False
    assert calls["n"] == 1  # 不重试


def test_5xx_retry_then_success():
    calls = {"n": 0}
    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, json={"error": {"message": "internal"}})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_sse(["ok"]))
    c = _client(handler, max_retries=2)
    text = asyncio.run(c.chat([{"role": "user", "content": "hi"}]))
    assert text == "ok" and calls["n"] == 2


def test_token_usage_accumulates():
    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=_sse(["x"], usage={"prompt_tokens": 5, "completion_tokens": 1}))
    c = _client(handler)
    async def two():
        await c.chat([{"role": "user", "content": "a"}])
        await c.chat([{"role": "user", "content": "b"}])
    asyncio.run(two())
    assert c.uh_token_usage_total["test-model"] == {"prompt_tokens": 10, "completion_tokens": 2}


def test_401_maps_cap_4003_no_retry():
    def handler(request):
        return httpx.Response(401, json={"error": {"message": "invalid api key"}})
    c = _client(handler, max_retries=3)
    with pytest.raises(LLMError) as ei:
        asyncio.run(c.chat([{"role": "user", "content": "hi"}]))
    assert ei.value.code == "UH-CAP-4003" and ei.value.retryable is False
