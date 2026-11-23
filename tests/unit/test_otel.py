import asyncio
import builtins
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient

from kernel import __version__, otel, tracing
from kernel.main import create_app
from kernel.mcp import MCPHost, StreamableHTTPTransport
from kernel.retrieval import create_mcp_app


@pytest.mark.parametrize("enabled, endpoint", [(None, None), (None, "http://otlp:4318"), ("1", None)])
def test_disabled_request_does_not_start_otel(monkeypatch, enabled, endpoint):
    monkeypatch.delenv("UH_OTEL_ENABLED", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    if enabled is not None:
        monkeypatch.setenv("UH_OTEL_ENABLED", enabled)
    if endpoint is not None:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint)
    exporter = Mock(side_effect=AssertionError("exporter initialized"))
    span = Mock(side_effect=AssertionError("span allocated"))
    monkeypatch.setattr(otel, "_export_processor", exporter)
    monkeypatch.setattr(otel, "span", span)
    app = create_app()
    assert all(item.cls is not otel.TracingMiddleware for item in app.user_middleware)
    response = TestClient(app).post("/api/v1/dev/route", json={"content": "search", "mode": "search"})
    assert response.status_code == 200
    exporter.assert_not_called()
    span.assert_not_called()


def test_missing_sdk_is_a_noop_even_with_endpoint(monkeypatch):
    monkeypatch.setenv("UH_OTEL_ENABLED", "true")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otlp:4318")
    original_import = builtins.__import__

    def without_sdk(name, *args, **kwargs):
        if name.startswith("opentelemetry"):
            raise ModuleNotFoundError(name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_sdk)
    exporter = Mock(side_effect=AssertionError("exporter initialized"))
    monkeypatch.setattr(otel, "_export_processor", exporter)
    assert TestClient(create_app()).get("/health").status_code == 200
    exporter.assert_not_called()


@pytest.mark.parametrize(("protocol", "module", "override", "expected"), [
    ("grpc", "grpc", None, "http://otlp:4317"),
    ("http/protobuf", "http", None, "http://otlp:4317/v1/traces"),
    ("http/protobuf", "http", "https://traces.example.invalid/custom", "https://traces.example.invalid/custom"),
])
def test_otlp_transport_and_endpoint(monkeypatch, protocol, module, override, expected):
    transport = pytest.importorskip(f"opentelemetry.exporter.otlp.proto.{module}.trace_exporter")
    export = pytest.importorskip("opentelemetry.sdk.trace.export")
    exporter = Mock()
    create_exporter = Mock(return_value=exporter)
    create_batch_processor = Mock()
    monkeypatch.setattr(transport, "OTLPSpanExporter", create_exporter)
    monkeypatch.setattr(export, "BatchSpanProcessor", create_batch_processor)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    if override is not None:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", override)
    otel._export_processor("http://otlp:4317", protocol)
    create_exporter.assert_called_once_with(endpoint=expected)
    create_batch_processor.assert_called_once_with(exporter)


@pytest.fixture
def exporter(monkeypatch):
    module = pytest.importorskip("opentelemetry.sdk.trace.export.in_memory_span_exporter")
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    exporter = module.InMemorySpanExporter()
    monkeypatch.setenv("UH_OTEL_ENABLED", "1")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otlp:4318")
    monkeypatch.setenv("UH_OTEL_TAIL_SAMPLING", "local")
    monkeypatch.setenv("UH_OTEL_SAMPLE_RATIO", "1")
    monkeypatch.setattr(otel, "_export_processor", lambda *_: SimpleSpanProcessor(exporter))
    return exporter


@pytest.mark.parametrize("ratio", [0, 1])
def test_http_sampling_and_metrics(monkeypatch, exporter, ratio):
    from opentelemetry.trace import StatusCode

    monkeypatch.setenv("UH_OTEL_SAMPLE_RATIO", str(ratio))
    with TestClient(create_app()) as client:
        valid = client.get("/health")
        failed = client.get("/api/v1/tasks/private-task")
        metrics = client.get("/metrics").text
        assert valid.status_code == 200
        assert failed.status_code == 404
        assert 'uh_kernel_requests_total{method="GET",route="/health",status_code="200",task_type="none"} 1' in metrics
        assert 'uh_kernel_request_errors_total{method="GET",route="/api/v1/tasks/{task_id}",status_code="404"} 1' in metrics
        spans = exporter.get_finished_spans()
        routes = {s.attributes["http.route"]: s for s in spans}
        assert ("/health" in routes) == bool(ratio)
        error = routes["/api/v1/tasks/{task_id}"]
        assert error.context.trace_id == otel.trace_id_to_otel(failed.headers["X-Trace-Id"])
        assert error.status.status_code is StatusCode.ERROR
        assert "private-task" not in repr(error.attributes)


def test_traceparent_round_trip_and_remote_parent_sampling(monkeypatch, exporter):
    monkeypatch.setenv("UH_OTEL_SAMPLE_RATIO", "0")
    runtime = otel.configure()
    trace_hex = "0123456789abcdef" * 2
    trace_id = "uh_trc_" + trace_hex
    carrier = {"traceparent": f"00-{trace_hex}-1122334455667788-01", "tracestate": "vendor=opaque"}
    with runtime.operation("http.request", carrier=carrier, trace_id="uh_trc_" + "fe" * 16) as inbound:
        assert tracing.get_trace_id() == trace_id
        assert otel.envelope_ids()[1] == "sp_55667788"
        with otel.span("mcp.call") as outbound:
            headers = otel.outbound_headers()
        with runtime.operation("mcp.receive", carrier=headers):
            assert tracing.get_trace_id() == trace_id
    assert tracing.get_trace_id() is None
    sent = f"00-{trace_hex}-{outbound.get_span_context().span_id:016x}-01"
    assert headers == {"traceparent": sent, "tracestate": "vendor=opaque"}
    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert spans["mcp.receive"].parent.span_id == outbound.get_span_context().span_id
    assert spans["mcp.receive"].parent.is_remote
    assert spans["mcp.call"].parent.span_id == inbound.get_span_context().span_id
    assert all(s.context.trace_id == int(trace_hex, 16) for s in spans.values())
    assert otel.trace_id_from_otel(int(trace_hex, 16)) == trace_id


def test_ratio_zero_drops_long_successful_trace(monkeypatch, exporter):
    monkeypatch.setenv("UH_OTEL_SAMPLE_RATIO", "0")
    runtime = otel.configure()
    with runtime.operation("http.request"):
        for _ in range(1100):
            with otel.span("capability.call"):
                pass
    assert not exporter.get_finished_spans()


def test_malformed_w3c_header_uses_valid_legacy_trace_id(exporter):
    with TestClient(create_app()) as client:
        tid = "uh_trc_" + "01" * 16
        response = client.get("/health", headers={"traceparent": "bad", "X-Trace-Id": tid})
        assert response.headers["X-Trace-Id"] == tid
        span, = exporter.get_finished_spans()
        assert span.context.trace_id == int(tid[7:], 16)
        assert span.parent is None
        invalid = client.get("/health", headers={"X-Trace-Id": "uh_trc_" + "00" * 16})
        assert otel.trace_id_to_otel(invalid.headers["X-Trace-Id"]) != 0


def test_cross_service_request_tree_and_envelope_ids(exporter):
    # Both services run the actual ASGI middleware. The MCP hop uses HTTPX's
    # ASGI transport so request headers can travel without a listening socket.
    app = create_app()
    original = app.state.rpc._handlers["retrieval.search"]
    rpc = {}
    events = {}

    async def record_rpc(req):
        rpc["request"] = req
        rpc["response"] = await original(req)
        rpc["response"].trace_id = ""  # A handler may rely on the registry to supply the trace fields.
        return rpc["response"]

    async def record_event(event):
        events[event.action] = event

    app.state.rpc.register("retrieval.search", record_rpc)
    app.state.bus.subscribe("mcp.tool.*", record_event)

    async def request():
        remote = create_mcp_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=remote)) as http:
            fixture = await http.post("http://retrieval/mcp", json={
                "method": "tools/call", "id": 0, "params": {
                    "name": "index_document",
                    "arguments": {"content": "observability fixture", "source": "otel.md"},
                },
            })
            assert not fixture.json()["result"]["isError"]
            app.state.retrieval_host = MCPHost(StreamableHTTPTransport("http://retrieval/mcp", client=http), bus=app.state.bus)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://kernel") as client:
                session = (await client.post("/api/v1/sessions")).json()["data"]["session_id"]
                response = await client.post(f"/api/v1/sessions/{session}/messages", json={
                    "content": "observability", "mode": "search", "context": {"memory_recall": False},
                })
                data = response.json()["data"]
                task = app.state.tasks.tasks[data["task_id"]]
                await task["_runner"]
                return response, data, task

    response, data, task = asyncio.run(request())
    assert response.status_code == 202 and task["status"] == "finished"
    trace_id = data["trace_id"]
    assert trace_id == response.headers["X-Trace-Id"] == response.json()["meta"]["trace_id"]
    assert all(entry["event"]["trace_id"] == trace_id for entry in task["event_buffer"])
    spans = [s for s in exporter.get_finished_spans() if s.context.trace_id == otel.trace_id_to_otel(trace_id)]
    named = {s.name: s for s in spans}
    assert set(named) == {"http.request", "router.decide", "capability.retrieve", "mcp.call", "mcp.receive", "memory.write"}
    root, cap, tool = (named[name] for name in ("http.request", "capability.retrieve", "mcp.call"))
    assert named["router.decide"].parent.span_id == cap.parent.span_id == root.context.span_id
    assert tool.parent.span_id == cap.context.span_id
    assert all(s.parent.span_id == tool.context.span_id and s.parent.is_remote for s in spans if s.name == "mcp.receive")
    assert all(s.context.trace_id == otel.trace_id_to_otel(trace_id) for s in spans)
    assert root.resource.attributes["service.name"] == "uh-kernel"
    assert root.resource.attributes["service.version"] == __version__
    assert named["mcp.receive"].resource.attributes["service.name"] == "uh-retrieval"
    assert root.attributes["http.route"] == "/api/v1/sessions/{session_id}/messages"
    assert all(set(s.attributes) <= {"http.route", "http.response.status_code"} for s in spans)

    for envelope in (rpc["request"], rpc["response"]):
        assert envelope.trace_id == trace_id
        assert envelope.span_id == otel.span_id_from_otel(cap.context.span_id)
        assert envelope.parent_span_id == otel.span_id_from_otel(root.context.span_id)
    for event in ("mcp.tool.call", "mcp.tool.result"):
        envelope = events[event]
        assert envelope.trace_id == trace_id
        assert envelope.span_id == otel.span_id_from_otel(tool.context.span_id)
        assert envelope.parent_span_id == otel.span_id_from_otel(cap.context.span_id)


def test_late_task_error_keeps_ended_http_span(monkeypatch, exporter):
    from opentelemetry.trace import StatusCode

    monkeypatch.setenv("UH_OTEL_SAMPLE_RATIO", "0")
    app = create_app()

    async def request():
        release = asyncio.Event()

        async def fail_late(*args):
            await release.wait()
            raise RuntimeError("sensitive error detail")
            yield  # Make this an async generator like the production client.

        app.state.tasks.llm.stream_chat = fail_late
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://kernel") as client:
            session = (await client.post("/api/v1/sessions")).json()["data"]["session_id"]
            response = await client.post(f"/api/v1/sessions/{session}/messages", json={
                "content": "hello", "context": {"memory_recall": False},
            })
            assert response.status_code == 202
            assert not exporter.get_finished_spans()
            release.set()
            task = app.state.tasks.tasks[response.json()["data"]["task_id"]]
            await task["_runner"]
            return task

    assert asyncio.run(request())["status"] == "failed"
    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert set(spans) == {"http.request", "router.decide", "memory.write", "task.failed"}
    assert spans["task.failed"].status.status_code is StatusCode.ERROR
    assert spans["task.failed"].parent.span_id == spans["http.request"].context.span_id
    assert all(not s.events for s in spans.values())
    assert spans["task.failed"].end_time > spans["http.request"].end_time


def test_collector_mode_forwards_unsampled_segments_for_shared_tail_decision(monkeypatch, exporter):
    monkeypatch.setenv("UH_OTEL_SAMPLE_RATIO", "0")
    monkeypatch.setenv("UH_OTEL_TAIL_SAMPLING", "collector")
    runtime = otel.configure()
    with runtime.operation("http.request"):
        assert otel.outbound_headers()["traceparent"].endswith("-00")
        with otel.span("router.decide"):
            pass
    spans = exporter.get_finished_spans()
    assert {span.name for span in spans} == {"http.request", "router.decide"}
    assert all(span.attributes["uh.sample_candidate"] == "false" for span in spans)
