"""MCP Host 适配层（M1-2）。

内核只依赖 :class:`MCPHost` 的 tools/list、tools/call 和 Registry 适配，
JSON-RPC 以及具体传输留在本模块内。默认传输是 stdio，也提供 in-process
传输便于测试和可选的 streamable HTTP 传输。
"""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import os
import shlex
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from .protocol import CapabilityRegistry, Envelope, EventBus, module_error_code

MAX_RESPONSE_BYTES = 262144
DEFAULT_TIMEOUT_MS = 5000
DEFAULT_MAX_RETRIES = 2


class MCPError(Exception):
    """结构化 MCP 错误，字段与 errors.md 的 CAP 错误响应对齐。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        retry_after_ms: int = 0,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.retry_after_ms = retry_after_ms
        self.detail = detail or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "retry_after_ms": self.retry_after_ms,
            "detail": dict(self.detail),
        }


class MCPTransport(Protocol):
    async def connect(self) -> None: ...

    async def request(self, method: str, params: dict[str, Any], request_id: int) -> dict[str, Any]: ...

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None: ...

    async def cancel(self, request_id: int, reason: str) -> None: ...

    async def close(self) -> None: ...


async def _maybe_await(value: Any) -> Any:
    return await value if inspect_is_awaitable(value) else value


def inspect_is_awaitable(value: Any) -> bool:
    return hasattr(value, "__await__")


class InProcessTransport:
    """最小 JSON-RPC transport，接受 ``handler(request)`` mock server。"""

    def __init__(self, handler: Callable[..., Any]) -> None:
        self.handler = handler
        self._tasks: dict[int, asyncio.Task[Any]] = {}
        self.notifications: list[dict[str, Any]] = []
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def request(self, method: str, params: dict[str, Any], request_id: int) -> dict[str, Any]:
        request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        task = asyncio.create_task(self._dispatch(request))
        self._tasks[request_id] = task
        try:
            return await task
        finally:
            self._tasks.pop(request_id, None)

    async def _dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            value = self.handler(request)
        except TypeError:
            value = self.handler(request["method"], request.get("params", {}))
        response = await _maybe_await(value)
        if response is None:
            return {"jsonrpc": "2.0", "id": request.get("id"), "result": {}}
        return response

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self.notifications.append(message)
        await self._dispatch(message)

    async def cancel(self, request_id: int, reason: str) -> None:
        await self.notify("notifications/cancelled", {"requestId": request_id, "reason": reason})
        task = self._tasks.get(request_id)
        if task is not None:
            task.cancel()

    async def close(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        self._tasks.clear()
        self.connected = False


class StdioTransport:
    """MCP stdio transport，按行收发 JSON-RPC 消息。"""

    def __init__(self, command: str | list[str], *, env: dict[str, str] | None = None, cwd: str | None = None) -> None:
        self.command = command
        self.env = env
        self.cwd = cwd
        self.process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._write_lock = asyncio.Lock()

    async def connect(self) -> None:
        if self.process is not None:
            return
        argv = self.command if isinstance(self.command, list) else shlex.split(self.command, posix=os.name != "nt")
        child_env = os.environ.copy()
        if self.env:
            child_env.update(self.env)
        self.process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env,
            cwd=self.cwd,
        )
        self._reader_task = asyncio.create_task(self._read_loop())

    async def _send(self, message: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise MCPError(module_error_code(), "MCP stdio server 未连接", retryable=True, detail={"module": "tool"})
        data = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        async with self._write_lock:
            self.process.stdin.write(data)
            await self.process.stdin.drain()

    async def _read_loop(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                request_id = message.get("id")
                if request_id is not None and request_id in self._pending:
                    future = self._pending.pop(request_id)
                    if not future.done():
                        future.set_result(message)
        except asyncio.CancelledError:
            raise
        finally:
            error = MCPError(module_error_code(), "MCP stdio server 连接已关闭", retryable=True, detail={"module": "tool"})
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(error)
            self._pending.clear()

    async def request(self, method: str, params: dict[str, Any], request_id: int) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = future
        try:
            await self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            return await future
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        await self._send(message)

    async def cancel(self, request_id: int, reason: str) -> None:
        with contextlib.suppress(Exception):
            await self.notify("notifications/cancelled", {"requestId": request_id, "reason": reason})
        future = self._pending.get(request_id)
        if future is not None and not future.done():
            future.cancel()

    async def close(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None
        if self.process:
            if self.process.stdin:
                self.process.stdin.close()
            if self.process.returncode is None:
                self.process.terminate()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.process.wait(), timeout=1)
            self.process = None


class StreamableHTTPTransport:
    """可选 HTTP transport；服务端必须接受 JSON-RPC POST。"""

    def __init__(self, url: str, *, headers: dict[str, str] | None = None, client: Any = None) -> None:
        self.url = url
        self.headers = headers or {}
        self._client = client
        self._owns_client = client is None

    async def connect(self) -> None:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(headers=self.headers)

    async def request(self, method: str, params: dict[str, Any], request_id: int) -> dict[str, Any]:
        await self.connect()
        response = await self._client.post(self.url, json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        response.raise_for_status()
        return response.json()

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self.connect()
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        await self._client.post(self.url, json=message)

    async def cancel(self, request_id: int, reason: str) -> None:
        await self.notify("notifications/cancelled", {"requestId": request_id, "reason": reason})

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None


@dataclass
class _ToolInfo:
    name: str
    description: str = ""
    input_schema: dict[str, Any] | None = None
    risk_level: str | None = None
    idempotent: bool | None = None


class MCPHost:
    """连接 MCP Server，并将工具暴露为 UH 内部能力。"""

    @classmethod
    def from_stdio(cls, command: str | list[str], **kwargs: Any) -> "MCPHost":
        return cls(command=command, **kwargs)

    @classmethod
    def from_http(cls, url: str, **kwargs: Any) -> "MCPHost":
        return cls(url=url, **kwargs)

    def __init__(
        self,
        transport: MCPTransport | None = None,
        *,
        command: str | list[str] | None = None,
        url: str | None = None,
        timeout_ms: int | None = None,
        max_retries: int | None = None,
        backoff_base_s: float = 0.1,
        bus: EventBus | None = None,
    ) -> None:
        if transport is None:
            if command is not None:
                transport = StdioTransport(command)
            elif url is not None:
                transport = StreamableHTTPTransport(url)
            else:
                raise ValueError("MCPHost 需要 transport、command 或 url")
        self.transport = transport
        self.timeout_ms = timeout_ms if timeout_ms is not None else int(os.environ.get("UH_MCP_TIMEOUT_MS", str(DEFAULT_TIMEOUT_MS)))
        self.max_retries = max_retries if max_retries is not None else int(os.environ.get("UH_MCP_MAX_RETRIES", str(DEFAULT_MAX_RETRIES)))
        self.backoff_base_s = backoff_base_s
        self.bus = bus
        self._request_ids = itertools.count(1)
        self._connected = False
        self._tools: dict[str, _ToolInfo] = {}
        self._manifests: dict[str, dict[str, Any]] = {}

    async def connect(self) -> None:
        if self._connected:
            return
        await self.transport.connect()
        response = await self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "uh-mcp-host", "version": "0.1.0"},
        }, timeout_ms=self.timeout_ms)
        result = response.get("result", {})
        if not isinstance(result, dict):
            raise MCPError(module_error_code(), "MCP initialize 响应无效", retryable=False, detail={"module": "tool"})
        await self.transport.notify("notifications/initialized", {})
        self._connected = True

    async def close(self) -> None:
        self._connected = False
        await self.transport.close()

    async def _request(self, method: str, params: dict[str, Any], *, timeout_ms: int) -> dict[str, Any]:
        request_id = next(self._request_ids)
        task = asyncio.create_task(self.transport.request(method, params, request_id))
        try:
            response = await asyncio.wait_for(asyncio.shield(task), timeout=timeout_ms / 1000)
        except asyncio.TimeoutError as exc:
            with contextlib.suppress(Exception):
                await self.transport.cancel(request_id, f"timeout after {timeout_ms}ms")
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            raise MCPError(
                "UH-CAP-5021",
                f"工具调用超时 ({timeout_ms}ms)",
                retryable=True,
                retry_after_ms=2000,
                detail={"module": "tool", "timeout_ms": timeout_ms},
            ) from exc
        except Exception as exc:
            if isinstance(exc, MCPError):
                raise
            raise MCPError(module_error_code(), f"MCP 上游服务错误: {exc}", retryable=True, detail={"module": "tool"}) from exc
        if not isinstance(response, dict) or response.get("jsonrpc") != "2.0":
            raise MCPError(module_error_code(), "MCP 响应不是 JSON-RPC 2.0", retryable=True, detail={"module": "tool"})
        if "error" in response:
            error = response.get("error") or {}
            data = error.get("data") if isinstance(error, dict) else {}
            raise MCPError(
                module_error_code(),
                str(error.get("message", "MCP 上游服务错误")) if isinstance(error, dict) else "MCP 上游服务错误",
                retryable=True,
                detail={"module": "tool", "upstream_code": error.get("code") if isinstance(error, dict) else None, **(data if isinstance(data, dict) else {})},
            )
        return response

    async def list_tools(self) -> list[dict[str, Any]]:
        await self.connect()
        response = await self._request("tools/list", {}, timeout_ms=self.timeout_ms)
        result = response.get("result", {})
        tools = result.get("tools", []) if isinstance(result, dict) else []
        if not isinstance(tools, list):
            raise MCPError(module_error_code(), "MCP tools/list 响应无效", retryable=False, detail={"module": "tool"})
        self._tools = {}
        for raw in tools:
            if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
                continue
            annotations = raw.get("annotations") or {}
            self._tools[raw["name"]] = _ToolInfo(
                name=raw["name"],
                description=str(raw.get("description", "")),
                input_schema=raw.get("inputSchema") if isinstance(raw.get("inputSchema"), dict) else {},
                risk_level=raw.get("risk_level") or annotations.get("riskLevel"),
                idempotent=annotations.get("readOnlyHint") if isinstance(annotations.get("readOnlyHint"), bool) else None,
            )
        return tools

    async def _request_call(self, name: str, arguments: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
        try:
            response = await self._request("tools/call", {"name": name, "arguments": arguments}, timeout_ms=timeout_ms)
        except MCPError as exc:
            if exc.code == "UH-CAP-5021":
                exc.detail.update({"module": "tool", "tool_name": name, "timeout_ms": timeout_ms})
                exc.message = f"工具 {name} 调用超时 ({timeout_ms}ms)"
            raise
        result = response.get("result")
        if not isinstance(result, dict):
            raise MCPError(module_error_code(), "MCP tools/call 响应无效", retryable=True, detail={"module": "tool", "tool_name": name})
        if result.get("isError"):
            raise MCPError(module_error_code(), f"工具 {name} 返回错误", retryable=True, detail={"module": "tool", "tool_name": name})
        return result

    @staticmethod
    def _redact(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: ("<redacted>" if any(s in k.lower() for s in ("token", "secret", "password", "passwd", "pwd", "api_key", "apikey", "authorization", "credential", "private_key", "access_key", "cookie", "bearer")) else MCPHost._redact(v)) for k, v in value.items()}
        if isinstance(value, list):
            return [MCPHost._redact(v) for v in value]
        return value

    @classmethod
    def _args_summary(cls, arguments: dict[str, Any]) -> str:
        raw = json.dumps(cls._redact(arguments), ensure_ascii=False, separators=(",", ":"))
        return raw[:512] + ("…" if len(raw) > 512 else "")

    @staticmethod
    def _content_preview(content: Any) -> Any:
        raw = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
        if len(raw.encode("utf-8")) <= 2048:
            return content
        return raw.encode("utf-8")[:2045].decode("utf-8", "ignore") + "..."

    @staticmethod
    def _truncate_result(result: dict[str, Any]) -> dict[str, Any]:
        result = dict(result)
        content = result.get("content", [])
        result["truncated"] = bool(result.get("truncated", False))
        encoded = lambda obj: len(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if encoded(result) <= MAX_RESPONSE_BYTES:
            return result
        result["truncated"] = True
        if isinstance(content, list):
            kept: list[Any] = []
            for item in content:
                candidate = dict(result)
                candidate["content"] = kept + [item]
                if encoded(candidate) > MAX_RESPONSE_BYTES:
                    break
                kept.append(item)
            result["content"] = kept
        if encoded(result) > MAX_RESPONSE_BYTES:
            result["content"] = [{"type": "text", "text": "[truncated]"}]
        # Ensure even an oversized non-content field cannot violate the cap.
        while encoded(result) > MAX_RESPONSE_BYTES:
            if isinstance(result.get("content"), list) and result["content"]:
                item = result["content"][-1]
                if isinstance(item, dict) and isinstance(item.get("text"), str) and item["text"]:
                    item = dict(item)
                    item["text"] = item["text"][: max(1, len(item["text"]) // 2)]
                    result["content"][-1] = item
                else:
                    result["content"].pop()
            else:
                break
        if encoded(result) > MAX_RESPONSE_BYTES:
            result = {"content": [{"type": "text", "text": "[truncated]"}], "truncated": True}
        return result

    def _tool_policy(self, name: str, manifest: dict[str, Any] | None, idempotent: bool | None, risk_level: str | None) -> tuple[bool, str, int]:
        info = self._tools.get(name)
        manifest = manifest or self._manifests.get(name) or {}
        config = manifest.get("config") if isinstance(manifest.get("config"), dict) else {}
        timeout_ms = int(config.get("timeout_ms", self.timeout_ms))
        # 二审 GPT-6 P0 修复：风险级取调用方/manifest/工具发现三者最严重，禁止调用方降级绕过确认。
        _RISK_ORDER = {"read-only": 0, "write-scoped": 1, "write-host": 2, "irreversible": 3}
        _cands = [r for r in (risk_level, manifest.get("risk_level"), info.risk_level if info else None) if r in _RISK_ORDER]
        risk = max(_cands, key=lambda r: _RISK_ORDER[r]) if _cands else None
        idem = idempotent if idempotent is not None else manifest.get("idempotent")
        if idem is None and info is not None:
            idem = info.idempotent
        if risk in {"write-host", "irreversible"}:
            idem = False
        if idem is None:
            idem = risk == "read-only"
        if not risk:
            risk = "read-only" if idem else "write-scoped"
        return bool(idem), str(risk), timeout_ms

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        manifest: dict[str, Any] | None = None,
        idempotent: bool | None = None,
        risk_level: str | None = None,
        timeout_ms: int | None = None,
        max_retries: int | None = None,
        trace_id: str = "",
        call_id: str | None = None,
        confirm: Any = None,
        task_id: str = "",
    ) -> dict[str, Any]:
        arguments = arguments or {}
        await self.connect()
        idem, risk, policy_timeout = self._tool_policy(name, manifest, idempotent, risk_level)
        timeout = int(timeout_ms if timeout_ms is not None else policy_timeout)
        retries = int(max_retries if max_retries is not None else self.max_retries) if idem else 0
        call_id = call_id or f"mcp_call_{next(self._request_ids)}"
        started = time.monotonic()
        if self.bus:
            await self.bus.publish("mcp.tool.call", {
                "call_id": call_id, "tool_name": name, "args_summary": self._args_summary(arguments),
                "risk_level": risk, "requires_confirmation": risk in {"write-host", "irreversible"},
                "task_id": task_id,
            }, trace_id=trace_id, from_="mcp_host")
        # M1-8 确认门 + 二审 N3 修复：高危工具 fail-closed——无确认通道一律拒绝（RPC 路径不可绕过）。
        if risk in {"write-host", "irreversible"}:
            if confirm is None:
                raise MCPError("UH-USR-1002", f"高危工具 {name} 须经确认通道", retryable=False, detail={"module": "tool", "tool_name": name, "confirmation_required": True, "risk_level": risk})
            decision = confirm(name, risk, self._args_summary(arguments), call_id)
            if asyncio.iscoroutine(decision):
                decision = await decision
            approved = decision.get("decision") if isinstance(decision, dict) else decision
            if approved != "approved":
                if self.bus:
                    await self.bus.publish("mcp.tool.result", {
                        "call_id": call_id, "tool_name": name, "status": "denied",
                        "truncated": False, "content_preview": None,
                        "duration_ms": int((time.monotonic() - started) * 1000), "task_id": task_id,
                    }, trace_id=trace_id, from_="mcp_host")
                raise MCPError("UH-USR-1002", f"高危工具 {name} 确认被拒绝，未执行", retryable=False, detail={"module": "tool", "tool_name": name, "confirmation_required": True, "risk_level": risk, "call_id": call_id})
        last_error: MCPError | None = None
        for attempt in range(retries + 1):
            try:
                result = self._truncate_result(await self._request_call(name, arguments, timeout))
                if self.bus:
                    await self.bus.publish("mcp.tool.result", {
                        "call_id": call_id, "tool_name": name, "status": "ok", "truncated": result.get("truncated", False),
                        "content_preview": self._content_preview(result.get("content", [])),
                        "duration_ms": int((time.monotonic() - started) * 1000), "task_id": task_id,
                    }, trace_id=trace_id, from_="mcp_host")
                return result
            except MCPError as exc:
                last_error = exc
                if attempt < retries and exc.retryable:
                    await asyncio.sleep(self.backoff_base_s * (2**attempt))
                    continue
                break
        assert last_error is not None
        if self.bus:
            await self.bus.publish("mcp.tool.result", {
                "call_id": call_id, "tool_name": name, "status": "timeout" if last_error.code == "UH-CAP-5021" else "error",
                "truncated": False, "content_preview": None,
                "duration_ms": int((time.monotonic() - started) * 1000), "error": last_error.to_dict(), "task_id": task_id,
            }, trace_id=trace_id, from_="mcp_host")
        raise last_error

    async def register_capabilities(
        self,
        registry: CapabilityRegistry,
        *,
        manifest: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> list[str]:
        discovered = tools if tools is not None else await self.list_tools()
        manifest = manifest or {}
        for raw in discovered:
            name = raw.get("name") if isinstance(raw, dict) else None
            if not isinstance(name, str):
                continue
            tool_manifest = manifest.get(name) if isinstance(manifest.get(name), dict) else manifest
            if tool_manifest:
                self._manifests[name] = tool_manifest
            action = f"mcp.tool.{name}"

            async def handler(req: Envelope, tool_name: str = name, tool_manifest_: dict[str, Any] = tool_manifest or {}) -> Envelope:
                try:
                    result = await self.call_tool(tool_name, req.payload.get("arguments", req.payload), manifest=tool_manifest_, trace_id=req.trace_id, call_id=req.payload.get("call_id"))
                    return Envelope(action=req.action, payload=result, trace_id=req.trace_id, from_=req.to, to=req.from_)
                except MCPError as exc:
                    return Envelope(action=req.action, payload={"degraded": True, "error_code": exc.code, "error": exc.to_dict()}, trace_id=req.trace_id, from_=req.to, to=req.from_)

            registry.register(action, handler)
        return [f"mcp.tool.{raw['name']}" for raw in discovered if isinstance(raw, dict) and isinstance(raw.get("name"), str)]


# 兼容调用方把 Host 适配层称作 Client 的简短别名。
MCPClient = MCPHost

