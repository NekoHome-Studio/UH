import json
import re
import secrets
import subprocess
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from kernel.llm import LLMClient
from kernel.main import create_app
from kernel.plugins import PluginManifest, PluginRecord

ROOT = Path(__file__).resolve().parents[2]
OBSERVABILITY = ROOT / "deploy" / "observability"
TYPES = {
    "uh_kernel_requests_total": "counter",
    "uh_kernel_request_duration_seconds": "histogram",
    "uh_capability_invocations_total": "counter",
    "uh_capability_failures_total": "counter",
    "uh_memory_short_term_keys": "gauge",
    "uh_memory_long_term_vectors": "gauge",
    "uh_plugins_active": "gauge",
    "uh_token_usage_total": "counter",
    "uh_memory_long_term_entries": "gauge",
    "uh_kernel_request_errors_total": "counter",
    "uh_tasks_transitions_total": "counter",
    "uh_plugins_crashes_total": "counter",
    "uh_plugins_crash_quarantined": "gauge",
}
ALLOWED_LABELS = {"method", "route", "status_code", "task_type", "le", "module", "model", "outcome"}


@pytest.fixture
def isolated_metrics_app(monkeypatch, tmp_path):
    monkeypatch.setenv("UH_LLM_PROVIDER", "mock")
    monkeypatch.delenv("UH_REDIS_URL", raising=False)
    monkeypatch.setenv("UH_LOCAL_MODE", "1")
    monkeypatch.setenv("UH_PLUGIN_ROOT", str(tmp_path / "plugins"))
    monkeypatch.setenv("UH_PLUGIN_REGISTRY", str(tmp_path / "plugins.json"))
    monkeypatch.setenv("UH_MEMORY_MASTER_KEY", secrets.token_urlsafe(32))


@pytest.fixture
def client(isolated_metrics_app):
    with TestClient(create_app()) as client:
        yield client


def _read_samples(body):
    rows = {}
    for line in body.splitlines():
        if line.startswith("#"):
            continue
        name, raw_labels, value = re.fullmatch(r'([a-z_]+)(?:\{(.*)\})? (\S+)', line).groups()
        labels = {}
        encoded = []
        for match in re.finditer(r'([a-z_]+)=("(?:[^"\\]|\\.)*")', raw_labels or ""):
            key, quoted = match.groups()
            assert key not in labels
            labels[key] = json.loads(quoted)
            encoded.append(match.group())
        assert ",".join(encoded) == (raw_labels or "")
        assert labels.keys() <= ALLOWED_LABELS
        key = name, tuple(sorted(labels.items()))
        assert key not in rows
        rows[key] = float(value)
    return rows


def _find(rows, name, **labels):
    return rows[name, tuple(sorted(labels.items()))]


def _scrape(client):
    response = client.get("/metrics")
    assert response.status_code == 200
    return _read_samples(response.text)


def test_metrics_inventory_and_memory_gauges(client):
    state = client.app.state
    client.portal.call(state.memory.append, "metrics-session", "user", "hello")
    state.long_term_memory.write("indexed content", session_id="metrics-session")
    state.long_term_memory.write("encrypted content", session_id="metrics-session", privacy_level="L2")
    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"
    assert response.text.endswith("\n")
    types = re.findall(r"^# TYPE (\w+) (\w+)$", response.text, flags=re.MULTILINE)
    assert len(types) == len(TYPES)
    assert dict(types) == TYPES
    assert "metrics-session" not in response.text
    rows = _read_samples(response.text)
    assert _find(rows, "uh_memory_short_term_keys") == 1
    assert _find(rows, "uh_memory_long_term_entries") == 2
    assert _find(rows, "uh_memory_long_term_vectors") == 1
    assert _find(rows, "uh_plugins_active") == 0
    assert _find(rows, "uh_token_usage_total", model="mock") == 0


def test_http_counters_histogram_and_route_cardinality(client, monkeypatch):
    for session in ("session-alpha", "session-beta"):
        path = f"/api/v1/sessions/{session}/messages"
        assert client.get(path, params={"page": 1}).status_code == 200
        assert client.get(path, params={"page": 0}).status_code == 400
    for suffix in ("one", "two"):
        assert client.request(f"CUSTOM-{suffix}", f"/unknown/{suffix}").status_code == 404
    monkeypatch.setenv("UH_AUTH_REQUIRED", "1")
    monkeypatch.setenv("UH_LOCAL_MODE", "0")
    for session in ("auth-one", "auth-two"):
        assert client.get(f"/api/v1/sessions/{session}/messages").status_code == 401

    rows = _scrape(client)
    route = "/api/v1/sessions/{session_id}/messages"
    requests = "uh_kernel_requests_total"
    errors = "uh_kernel_request_errors_total"
    for status in ("200", "400", "401"):
        assert _find(rows, requests, route=route, method="GET", status_code=status, task_type="none") == 2
    for status in ("400", "401"):
        assert _find(rows, errors, route=route, method="GET", status_code=status) == 2
    assert _find(rows, requests, route="unmatched", method="OTHER", status_code="404", task_type="none") == 2
    assert _find(rows, errors, route="unmatched", method="OTHER", status_code="404") == 2
    assert {dict(labels)["route"] for _, labels in rows if "route" in dict(labels)} == {
        route, "unmatched",
    }

    base = "uh_kernel_request_duration_seconds"
    buckets = [(float(dict(labels)["le"]), value) for (name, labels), value in rows.items()
               if name == base + "_bucket" and dict(labels)["route"] == route]
    assert len(buckets) > 1
    assert buckets == sorted(buckets) and buckets[-1] == (float("inf"), 6)
    assert _find(rows, base + "_count", route=route, method="GET") == 6
    assert _find(rows, base + "_sum", route=route, method="GET") > 0


def test_task_type_replay_and_capability_outcomes(client):
    session = client.post("/api/v1/sessions").json()["data"]["session_id"]
    path = f"/api/v1/sessions/{session}/messages"
    rpc = client.app.state.rpc
    rpc.set_available("retrieval.search", False)
    headers = {"Idempotency-Key": "same-search"}
    body = {"content": "find something", "mode": "search"}
    first = client.post(path, json=body, headers=headers)
    replay = client.post(path, json=body, headers=headers)
    assert first.status_code == replay.status_code == 202
    assert replay.json() == first.json()
    task_id = first.json()["data"]["task_id"]
    assert "task.finished" in client.get(f"/api/v1/tasks/{task_id}/stream").text
    rpc.set_available("retrieval.search", True)
    successful_result = client.portal.call(rpc.call, "retrieval.search", {"query": "UH!"})
    assert not successful_result.payload.get("degraded")
    failure = client.portal.call(rpc.call, "untrusted_identifier.perform", {})
    assert failure.payload["degraded"]

    rows = _scrape(client)
    assert _find(rows, "uh_kernel_requests_total", method="POST",
                 route="/api/v1/sessions/{session_id}/messages", status_code="202", task_type="search") == 2
    assert _find(rows, "uh_tasks_transitions_total", outcome="created") == 1
    assert _find(rows, "uh_tasks_transitions_total", outcome="finished") == 1
    assert _find(rows, "uh_capability_invocations_total", module="retrieval") == 2
    assert _find(rows, "uh_capability_failures_total", module="retrieval") == 1
    assert _find(rows, "uh_capability_invocations_total", module="llm") == 1
    assert _find(rows, "uh_capability_failures_total", module="llm") == 0
    assert _find(rows, "uh_capability_invocations_total", module="other") == 1
    assert _find(rows, "uh_capability_failures_total", module="other") == 1


def test_token_counter_uses_llm_stream_usage(isolated_metrics_app, monkeypatch):
    def upstream(request):
        assert json.loads(request.content)["stream_options"]["include_usage"]
        return httpx.Response(
            200,
            content='data: {"choices": [{"delta": {"content": "hello"}}]}\n\n'
                    'data: {"usage": {"prompt_tokens": 4, "completion_tokens": 2}}\n\n'
                    'data: [DONE]\n\n',
        )

    model = 'configured"model\\alpha\nbeta'
    http = httpx.AsyncClient(transport=httpx.MockTransport(upstream), base_url="https://example.invalid")
    llm = LLMClient(client=http, model=model)
    monkeypatch.setattr("kernel.main.create_llm_client", lambda: llm)
    with TestClient(create_app()) as client:
        assert _find(_scrape(client), "uh_token_usage_total", model=model) == 0
        for calls in (1, 2):
            assert client.portal.call(llm.chat, [{"role": "user", "content": "hello"}]) == "hello"
            assert _find(_scrape(client), "uh_token_usage_total", model=model) == 6 * calls
        client.portal.call(llm.aclose)


def test_plugin_crash_and_quarantine_metrics(client, tmp_path):
    driver = client.app.state.plugin_driver
    manifest = PluginManifest.from_dict({
        "name": "test-crashing-plugin", "version": "1.0.0", "type": "tool",
        "description": "test", "author": "test", "license": "MIT",
    })
    record = PluginRecord(manifest, "active", "", str(tmp_path), "", "", "")
    driver.records[manifest.name] = record

    assert _find(_scrape(client), "uh_plugins_active") == 1
    for crash in (1, 2, 3):
        restarted = client.portal.call(driver._on_plugin_crash, manifest, tmp_path)
        assert restarted == (crash < 3)
        rows = _scrape(client)
        assert _find(rows, "uh_plugins_crashes_total") == crash
        assert _find(rows, "uh_plugins_active") == int(crash < 3)
        assert _find(rows, "uh_plugins_crash_quarantined") == int(crash == 3)
    assert record.status == "quarantined" and record.crash_count == 3
    assert client.portal.call(driver._on_plugin_crash, manifest, tmp_path) is False
    assert _find(_scrape(client), "uh_plugins_crashes_total") == 3


@pytest.fixture
def rules():
    groups = yaml.safe_load((OBSERVABILITY / "alert-rules.yaml").read_text(encoding="utf-8"))["groups"]
    rows = [rule for group in groups for rule in group["rules"]]
    result = {rule["alert"]: rule for rule in rows}
    assert len(rows) == len(result) == 3
    assert result.keys() == {"HighErrorRate", "MemoryPressure", "PluginCrashLoop"}
    return result


def test_alert_metadata_and_deployment(rules):
    for name, duration, severity in (
        ("HighErrorRate", "2m", "critical"),
        ("MemoryPressure", "5m", "warning"),
        ("PluginCrashLoop", "1m", "critical"),
    ):
        rule = rules[name]
        assert rule["for"] == duration and rule["labels"]["severity"] == severity
        assert rule["annotations"]["summary"] and rule["annotations"]["description"]

    def config(name):
        return yaml.safe_load((OBSERVABILITY / f"{name}.yaml").read_text(encoding="utf-8"))

    prometheus = config("prometheus")
    assert prometheus["rule_files"] == ["alert-rules.yaml"]
    assert prometheus["scrape_configs"][0]["metrics_path"] == "/metrics"
    assert prometheus["scrape_configs"][0]["static_configs"] == [{"targets": ["kernel:8080"]}]
    assert prometheus["alerting"]["alertmanagers"] == [{"static_configs": [{"targets": ["alertmanager:9093"]}]}]
    overlay = config("compose")
    assert overlay["services"]["uh-kernel"]["networks"]["default"]["aliases"] == ["kernel"]
    assert overlay["secrets"]["uh_alertmanager_webhook"] == {"environment": "UH_ALERTMANAGER_WEBHOOK_URL"}
    assert overlay["services"]["alertmanager"]["secrets"] == ["uh_alertmanager_webhook"]
    alertmanager = config("alertmanager")
    receiver = alertmanager["receivers"][0]
    assert alertmanager["route"]["receiver"] == receiver["name"]
    assert receiver["webhook_configs"] == [
        {"url_file": "/run/secrets/uh_alertmanager_webhook", "send_resolved": True},
    ]


@pytest.mark.parametrize(("failures", "invocations", "fires"), [
    ((0, 0), (0, 0), False),
    ((0, 1), (0, 0), False),
    ((0, 9), (0, 100), False),
    ((0, 10), (0, 100), False),
    ((0, 11), (0, 100), True),
])
def test_high_error_rate_rule(rules, failures, invocations, fires):
    expr = " ".join(rules["HighErrorRate"]["expr"].split())
    match = re.fullmatch(
        r"rate\(uh_capability_failures_total\[5m\]\) / "
        r"\(rate\(uh_capability_invocations_total\[5m\]\) > (0)\) > (0\.1)", expr,
    )
    assert match
    guard, threshold = map(float, match.groups())
    # Both counters use the same interval, so their sample interval cancels.
    fail_delta = failures[-1] - failures[0]
    invocation_delta = invocations[-1] - invocations[0]
    assert (invocation_delta > guard and fail_delta / invocation_delta > threshold) is fires


@pytest.mark.parametrize(("alert", "metric", "threshold", "value", "fires"), [
    ("MemoryPressure", "uh_memory_short_term_keys", 10000, 9999, False),
    ("MemoryPressure", "uh_memory_short_term_keys", 10000, 10000, False),
    ("MemoryPressure", "uh_memory_short_term_keys", 10000, 10001, True),
    ("PluginCrashLoop", "uh_plugins_crash_quarantined", 0, 0, False),
    ("PluginCrashLoop", "uh_plugins_crash_quarantined", 0, 1, True),
])
def test_gauge_rules(rules, alert, metric, threshold, value, fires):
    match = re.fullmatch(rf"{metric} > (\d+)", rules[alert]["expr"])
    assert match and int(match[1]) == threshold
    assert (value > int(match[1])) is fires


@pytest.mark.parametrize(("original", "replacement", "expected"), [
    ('"status_code", "task_type"', '"status_code", "session_id"', "forbidden labels session_id"),
    ('return matched.path_format', 'return scope["path"]', "route values must come from declared route"),
])
def test_cardinality_gate_rejects_regressions(tmp_path, original, replacement, expected):
    kernel = tmp_path / "kernel"
    kernel.mkdir()
    source = (ROOT / "kernel" / "metrics.py").read_text(encoding="utf-8")
    assert original in source
    (kernel / "metrics.py").write_text(source.replace(original, replacement, 1), encoding="utf-8")
    script = (ROOT / "scripts" / "verify-metrics-cardinality.mjs").as_uri()
    command = f"import {{verifyMetricsCardinality}} from {json.dumps(script)}; verifyMetricsCardinality(process.argv[1]);"
    result = subprocess.run(["node", "--input-type=module", "-e", command, str(tmp_path)], capture_output=True, text=True)
    assert result.returncode != 0 and expected in result.stderr
