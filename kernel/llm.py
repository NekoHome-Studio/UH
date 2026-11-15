"""LLM 调用封装（M0-3，§7.5.1 / errors.md CAP-4xxx）。

Provider 无关接口 LLMClient，默认 OpenAI 兼容实现：
- 流式 SSE 输出（完整无截断）
- 超时 + 指数退避重试（仅可重试错误）
- 上游错误映射 UH-CAP-4001/4002/4003
- Token 统计（uh_token_usage_total，按 model）
配置经环境变量，严禁硬编码密钥。业务只依赖 LLMClient 接口，厂商差异不穿透。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, AsyncIterator

import httpx

from .logging import get_logger, log_event

log = get_logger("kernel.llm")


class LLMError(Exception):
    """结构化 LLM 错误，与 errors.md CAP-4xxx 对齐。"""

    def __init__(self, code: str, message: str, retryable: bool, retry_after_ms: int = 0, detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.retry_after_ms = retry_after_ms
        self.detail = detail or {}


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


class LLMClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout_ms: int | None = None,
        max_retries: int | None = None,
        backoff_base_s: float = 0.5,
        client: httpx.AsyncClient | None = None,
    ):
        self.base_url = (base_url or _env("UH_LLM_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.api_key = api_key if api_key is not None else _env("UH_LLM_API_KEY")
        self.model = model or _env("UH_LLM_MODEL", "gpt-4o-mini")
        self.timeout_ms = timeout_ms if timeout_ms is not None else int(_env("UH_LLM_TIMEOUT_MS", "30000"))
        self.max_retries = max_retries if max_retries is not None else int(_env("UH_LLM_MAX_RETRIES", "3"))
        self.backoff_base_s = backoff_base_s
        self._client = client or httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout_ms / 1000.0)
        self._usage: dict[str, dict[str, int]] = {}

    @property
    def uh_token_usage_total(self) -> dict[str, dict[str, int]]:
        return {m: dict(v) for m, v in self._usage.items()}

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = "Bearer " + self.api_key
        return h

    def _payload(self, messages: list[dict], stream: bool, **kwargs: Any) -> dict:
        p: dict[str, Any] = {"model": self.model, "messages": messages, "stream": stream}
        if stream:
            p["stream_options"] = {"include_usage": True}
        p.update(kwargs)
        return p

    def _record_usage(self, usage: dict | None) -> None:
        if not usage:
            return
        bucket = self._usage.setdefault(self.model, {"prompt_tokens": 0, "completion_tokens": 0})
        bucket["prompt_tokens"] += int(usage.get("prompt_tokens", 0))
        bucket["completion_tokens"] += int(usage.get("completion_tokens", 0))
        log_event(log, logging.INFO, "llm.usage", model=self.model, **bucket)

    def _map_error(self, status: int, body: bytes, headers: httpx.Headers) -> LLMError:
        try:
            msg = json.loads(body).get("error", {}).get("message", body.decode("utf-8", "replace"))
        except Exception:
            msg = body.decode("utf-8", "replace")
        if status == 400 and ("token" in msg.lower() or "context" in msg.lower() or "length" in msg.lower()):
            return LLMError("UH-CAP-4001", "LLM Token 超限: " + msg, retryable=False, detail={"model": self.model})
        if status == 429:
            ra = headers.get("retry-after")
            retry_after_ms = int(float(ra) * 1000) if ra else 2000
            return LLMError("UH-CAP-4002", "LLM 上游限流: " + msg, retryable=True, retry_after_ms=retry_after_ms, detail={"model": self.model})
        if status in (401, 403):
            return LLMError("UH-CAP-4003", "LLM 上游鉴权失败: " + msg, retryable=False, detail={"model": self.model, "status": status})
        return LLMError("UH-CAP-4003", "LLM 上游服务错误: " + msg, retryable=True, detail={"model": self.model, "status": status})

    async def _open_stream(self, messages: list[dict], **kwargs: Any) -> httpx.Response:
        req = self._client.build_request("POST", "/chat/completions", json=self._payload(messages, True, **kwargs), headers=self._headers())
        resp = await self._client.send(req, stream=True)
        if resp.status_code != 200:
            body = await resp.aread()
            await resp.aclose()
            raise self._map_error(resp.status_code, body, resp.headers)
        return resp

    async def stream_chat(self, messages: list[dict], **kwargs: Any) -> AsyncIterator[str]:
        attempt = 0
        while True:
            try:
                resp = await self._open_stream(messages, **kwargs)
                break
            except (httpx.TimeoutException, httpx.TransportError) as e:
                err = LLMError("UH-CAP-4003", "LLM 上游网络/超时: " + str(e), retryable=True, detail={"model": self.model})
                if attempt < self.max_retries:
                    log_event(log, logging.WARNING, "llm.retry", attempt=attempt + 1, code=err.code)
                    await asyncio.sleep(self.backoff_base_s * (2 ** attempt))
                    attempt += 1
                    continue
                log_event(log, logging.ERROR, "llm.error", code=err.code)
                raise err from e
            except LLMError as e:
                if e.retryable and attempt < self.max_retries:
                    log_event(log, logging.WARNING, "llm.retry", attempt=attempt + 1, code=e.code)
                    await asyncio.sleep(max(e.retry_after_ms / 1000.0, self.backoff_base_s * (2 ** attempt)))
                    attempt += 1
                    continue
                log_event(log, logging.ERROR, "llm.error", code=e.code)
                raise
        log_event(log, logging.INFO, "llm.call", model=self.model, stream=True)
        try:
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if obj.get("usage"):
                    self._record_usage(obj["usage"])
                for choice in obj.get("choices", []):
                    delta = choice.get("delta", {}).get("content")
                    if delta:
                        yield delta
        finally:
            await resp.aclose()

    async def chat(self, messages: list[dict], **kwargs: Any) -> str:
        return "".join([chunk async for chunk in self.stream_chat(messages, **kwargs)])

    async def aclose(self) -> None:
        await self._client.aclose()


class MockLLMClient:
    """开发/演示用 mock provider（无密钥时回退），接口与 LLMClient 一致。"""

    def __init__(self, **kwargs: Any):
        self.model = "mock"

    @property
    def uh_token_usage_total(self) -> dict:
        return {}

    async def stream_chat(self, messages: list[dict], **kwargs: Any) -> AsyncIterator[str]:
        last = messages[-1]["content"] if messages else ""
        text = "这是 UH! 内核的演示回复（mock provider，未配置真实 LLM）。收到：" + last
        for ch in text:
            yield ch
            await asyncio.sleep(0.005)

    async def chat(self, messages: list[dict], **kwargs: Any) -> str:
        return "".join([c async for c in self.stream_chat(messages, **kwargs)])

    async def aclose(self) -> None:
        pass


def create_llm_client() -> Any:
    if os.environ.get("UH_LLM_PROVIDER") == "mock" or not os.environ.get("UH_LLM_API_KEY"):
        log_event(log, logging.WARNING, "llm.backend", backend="mock", reason="UH_LLM_API_KEY unset (dev fallback)")
        return MockLLMClient()
    return LLMClient()
