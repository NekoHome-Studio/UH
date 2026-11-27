"""M3 独立复审 P1：Compose/K8s 能力 Deployment 未承接内核请求。"""
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def _k8s_docs(name):
    return [d for d in yaml.safe_load_all(
        (ROOT / "deploy" / "k8s" / name).read_text(encoding="utf-8")) if d]


def _kernel_env():
    kernel = next(d for d in _k8s_docs("deployments.yaml")
                  if d.get("kind") == "Deployment" and d["metadata"]["name"] == "uh-kernel")
    env = {}
    for e in kernel["spec"]["template"]["spec"]["containers"][0]["env"]:
        env[e["name"]] = e.get("value")
    return env


def test_kernel_points_at_remote_capability_services():
    """内核必须配远程 URL，否则能力仍走进程内、副本数无意义。"""
    env = _kernel_env()
    assert env.get("UH_RETRIEVAL_MCP_URL", "").startswith("http://uh-llama-index")
    assert env.get("UH_ORCHESTRATION_MCP_URL", "").startswith("http://uh-langgraph")


@pytest.mark.parametrize(("workload", "module"), [
    ("uh-llama-index", "kernel.retrieval"),
    ("uh-langgraph", "kernel.orchestration"),
])
def test_capability_deployment_actually_serves_mcp(workload, module):
    """能力容器须显式起 MCP HTTP 服务，否则远程无对端。"""
    dep = next(d for d in _k8s_docs("deployments.yaml")
               if d.get("kind") == "Deployment" and d["metadata"]["name"] == workload)
    container = dep["spec"]["template"]["spec"]["containers"][0]
    command = " ".join(container.get("command") or [])
    assert module in command, f"{workload} 未起 {module}: {command}"
    env = {e["name"]: e.get("value") for e in container.get("env") or []}
    assert env.get("UH_MCP_TRANSPORT") == "http", f"{workload} 未启用 HTTP 传输"


def test_compose_capabilities_serve_mcp():
    compose = yaml.safe_load((ROOT / "docker-compose.yaml").read_text(encoding="utf-8"))
    for name, module in (("uh-llama-index", "kernel.retrieval"),
                         ("uh-langgraph", "kernel.orchestration")):
        svc = compose["services"][name]
        assert module in " ".join(svc.get("command") or []), name
        assert (svc.get("environment") or {}).get("UH_MCP_TRANSPORT") == "http", name
    kernel_env = compose["services"]["uh-kernel"]["environment"]
    assert "uh-llama-index" in str(kernel_env.get("UH_RETRIEVAL_MCP_URL"))
    assert "uh-langgraph" in str(kernel_env.get("UH_ORCHESTRATION_MCP_URL"))


def test_capability_http_apps_expose_probes():
    """K8s 探针打 /ready 与 /health，缺则能力 Pod 永不就绪。"""
    from fastapi.testclient import TestClient

    from kernel.orchestration import create_mcp_app as orch_app
    from kernel.retrieval import create_mcp_app as retr_app

    for factory, role in ((retr_app, "retrieval"), (orch_app, "orchestration")):
        with TestClient(factory()) as c:
            assert c.get("/health").json()["status"] == "ok"
            assert c.get("/ready").json()["status"] == "ok"
            assert c.get("/health").json()["role"] == role


def test_orchestration_mcp_returns_events_with_result():
    """远程 emit 无法跨进程，事件须随结果返回供内核回放。"""
    from kernel.orchestration import OrchestrationMCPServer

    server = OrchestrationMCPServer()
    resp = server.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "run",
                   "arguments": {"task_id": "uh_task_probe01", "content": "分步任务",
                                 "trace_id": "uh_trc_probe"}},
    })
    assert "error" not in resp, resp
    payload = resp["result"]
    assert "events" in payload, "缺 events，内核无法回放任务事件流"
    assert isinstance(payload["events"], list)
    assert payload["events"], "编排应至少产生一条事件"
    first = payload["events"][0]
    assert isinstance(first, (list, tuple)) and len(first) == 2
    assert first[0].startswith("step.") or first[0].startswith("task.")


def test_orchestration_mcp_tools_list_and_unknown():
    from kernel.orchestration import OrchestrationMCPServer

    server = OrchestrationMCPServer()
    init = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert init["result"]["serverInfo"]["name"] == "uh-orchestration"
    tools = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in tools["result"]["tools"]}
    assert {"run", "checkpoint"} <= names
    bad = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                         "params": {"name": "nope", "arguments": {}}})
    assert bad["error"]["code"] == -32601
