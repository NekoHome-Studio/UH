import asyncio
import io
import ipaddress
import json
import logging
import re
from pathlib import Path

import httpx
import pytest
import yaml

from kernel import scaler as scaler_module
from kernel import tracing
from kernel.scaler import CrewAIScaler, ScaleResult


MANIFESTS = Path(__file__).resolve().parents[2] / "deploy" / "k8s"
NAMESPACE = "uh-prod"
KERNEL = "uh-kernel"
CREWAI = "uh-crewai"
CAPABILITIES = ("uh-llama-index", "uh-langgraph", CREWAI)
STORES = {"redis": 6379, "chroma": 8000, "postgres": 5432}
DEPLOYMENT_API = "/apis/apps/v1/namespaces/uh-prod/deployments/uh-crewai"


@pytest.fixture(scope="module")
def manifests():
    documents = {}
    for file in sorted(MANIFESTS.glob("*.yaml")):
        for doc in yaml.safe_load_all(file.read_text(encoding="utf-8")):
            key = (doc["kind"], doc["metadata"]["name"])
            assert key not in documents
            if doc["kind"] != "Namespace":
                assert doc["metadata"]["namespace"] == NAMESPACE
            documents[key] = doc
    assert set(name for kind, name in documents if kind == "Namespace") == {NAMESPACE}
    return documents


@pytest.mark.parametrize("name", [KERNEL, *CAPABILITIES])
def test_deployment_and_service(manifests, name):
    deployment = manifests["Deployment", name]
    assert deployment["apiVersion"] == "apps/v1"
    spec = deployment["spec"]
    tier = "kernel" if name == KERNEL else "capability"
    labels = {"app": name, "tier": tier}
    assert spec["selector"]["matchLabels"] == spec["template"]["metadata"]["labels"] == deployment["metadata"]["labels"] == labels
    pod = spec["template"]["spec"]
    container, = pod["containers"]
    assert not pod.get("hostNetwork")
    assert container["livenessProbe"]["httpGet"] == {"path": "/health", "port": "http"}
    for kind in ("requests", "limits"):
        assert set(container["resources"][kind]) == {"cpu", "memory"}
    if name == KERNEL:
        assert spec["replicas"] >= 2
        assert pod["serviceAccountName"] == "uh-scaler"
    else:
        assert pod.get("serviceAccountName", "default") == "default"
        assert pod["automountServiceAccountToken"] is False
        assert container["readinessProbe"]["httpGet"] == {"path": "/ready", "port": "http"}
    if name == CREWAI:
        assert spec["replicas"] == 0
    service = manifests["Service", name]["spec"]
    assert service["selector"] == labels
    assert service["type"] == "ClusterIP"
    assert service["ports"] == [{"name": "http", "port": 8080, "targetPort": "http"}]
    assert not service.get("externalIPs")


@pytest.mark.parametrize(("status", "exit_code"), [("ok", 0), ("degraded", 1)])
def test_kernel_readiness_probe_reads_ready_status(manifests, monkeypatch, status, exit_code):
    container, = manifests["Deployment", KERNEL]["spec"]["template"]["spec"]["containers"]
    python, flag, script = container["readinessProbe"]["exec"]["command"]
    assert (python, flag) == ("python", "-c")

    def urlopen(url, timeout):
        assert url == "http://127.0.0.1:8080/ready"
        assert timeout < container["readinessProbe"]["timeoutSeconds"]
        return io.StringIO(json.dumps({"status": status}))

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    with pytest.raises(SystemExit) as exc:
        exec(script, {})
    assert exc.value.code == exit_code


@pytest.mark.parametrize("name", CAPABILITIES)
def test_cpu_hpa(manifests, name):
    hpa = manifests["HorizontalPodAutoscaler", name]
    assert hpa["apiVersion"] == "autoscaling/v2"
    spec = hpa["spec"]
    assert spec["scaleTargetRef"] == {"kind": "Deployment", "name": name, "apiVersion": "apps/v1"}
    assert spec["metrics"] == [{
        "type": "Resource",
        "resource": {"name": "cpu", "target": {"type": "Utilization", "averageUtilization": 70}},
    }]
    assert (spec["minReplicas"], spec["maxReplicas"]) == ((1, 1) if name == CREWAI else (2, 5))
    if name != CREWAI:
        assert "replicas" not in manifests["Deployment", name]["spec"]


@pytest.mark.parametrize(("name", "port"), STORES.items())
def test_datastore_uses_headless_service_and_persistent_volume(manifests, name, port):
    workload = manifests["StatefulSet", name]
    spec = workload["spec"]
    service = manifests["Service", name]["spec"]
    assert spec["serviceName"] == name
    assert service["clusterIP"] == "None"
    assert service["ports"][0]["port"] == port
    assert service["selector"] == spec["selector"]["matchLabels"] == {"app": f"uh-{name}", "tier": "datastore"}
    assert spec["template"]["metadata"]["labels"] == service["selector"]
    claim, = spec["volumeClaimTemplates"]
    assert claim["spec"]["resources"]["requests"]["storage"]
    pod = spec["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    container, = pod["containers"]
    mount, = container["volumeMounts"]
    assert mount["name"] == claim["metadata"]["name"]


def test_scaler_rbac_has_only_the_two_namespaced_rules(manifests):
    role = "uh-crewai-scaler"
    binding_kinds = {"Role", "RoleBinding", "ClusterRole", "ClusterRoleBinding", "ServiceAccount"}
    assert {key for key in manifests if key[0] in binding_kinds} == {
        ("ServiceAccount", "uh-scaler"), ("Role", role), ("RoleBinding", role),
    }
    rules = manifests["Role", role]["rules"]
    assert len(rules) == 2
    actual = {tuple(rule["resources"]): set(rule["verbs"]) for rule in rules}
    assert actual == {("deployments/scale",): {"get", "patch", "update"}, ("deployments",): {"get"}}
    for rule in rules:
        assert rule["apiGroups"] == ["apps"]
        assert rule["resourceNames"] == [CREWAI]
    binding = manifests["RoleBinding", role]
    assert binding["subjects"] == [{"kind": "ServiceAccount", "name": "uh-scaler", "namespace": NAMESPACE}]
    assert binding["roleRef"] == {"kind": "Role", "name": role, "apiGroup": "rbac.authorization.k8s.io"}


def test_network_policy_has_the_seven_flows_and_no_wider_rules(manifests):
    policies = {name: doc["spec"] for (kind, name), doc in manifests.items() if kind == "NetworkPolicy"}
    assert policies.pop("default-deny") == {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}
    dns = policies.pop("allow-dns")
    dns_rule, = dns["egress"]
    assert dns["policyTypes"] == ["Egress"] and dns["podSelector"] == {}
    assert {(item["port"], item["protocol"]) for item in dns_rule["ports"]} == {(53, "TCP"), (53, "UDP")}
    assert dns_rule["to"] == [{
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
        "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
    }]

    llm = policies.pop("llm-egress")
    llm_rule, = llm["egress"]
    assert llm["policyTypes"] == ["Egress"]
    assert llm["podSelector"] == {"matchExpressions": [{"key": "app", "operator": "In", "values": [KERNEL, CREWAI]}]}
    assert llm_rule["ports"] == [{"port": 443}]
    assert llm_rule["to"]
    for peer in llm_rule["to"]:
        assert set(peer) == {"ipBlock"} and set(peer["ipBlock"]) == {"cidr"}
        network = ipaddress.ip_network(peer["ipBlock"]["cidr"])
        assert network.num_addresses == 1
        address = network.network_address
        assert address.is_global or str(address) == "192.0.2.1"
        assert not address.is_multicast

    def selected(selector):
        assert set(selector) == {"matchLabels"}
        label, = selector["matchLabels"].items()
        return label

    flows = set()
    for spec in policies.values():
        policy_type, = spec["policyTypes"]
        direction = policy_type.lower()
        assert set(spec) == {"podSelector", "policyTypes", direction}
        local = selected(spec["podSelector"])
        for rule in spec[direction]:
            peer_key = "from" if direction == "ingress" else "to"
            assert set(rule) == {peer_key, "ports"}
            # M3 加固项可携带多端口（OTel 4317/4318 同规则），逐端口展开
            assert rule["ports"]
            for port in rule["ports"]:
                assert port.get("protocol", "TCP") == "TCP" and set(port) <= {"port", "protocol"}
                for peer in rule[peer_key]:
                    peer_type, = peer
                    if peer_type == "ipBlock":
                        # 加固项 apiserver 走 ipBlock，不参与 pod 选择器流量矩阵
                        flows.add((direction, local, ("ipBlock", peer["ipBlock"]["cidr"]), port["port"]))
                        continue
                    flows.add((direction, local, selected(peer[peer_type]), port["port"]))

    kernel = ("app", KERNEL)
    capability = ("tier", "capability")
    expected = {
        ("ingress", kernel, ("kubernetes.io/metadata.name", "ingress-controller"), 8080),
        ("ingress", capability, kernel, 8080),
        ("egress", kernel, capability, 8080),
    }
    for store, port in STORES.items():
        database = ("app", f"uh-{store}")
        consumer = ("app", "uh-llama-index" if store == "chroma" else "uh-langgraph")
        for source in (kernel, consumer):
            expected.update({("egress", source, database, port), ("ingress", database, source, port)})
    # M3 加固追加（阻断①②）：apiserver 控制面 + OTel collector 双端口
    otel = ("app", "otel-collector")
    expected.update({
        ("egress", kernel, otel, 4317), ("egress", kernel, otel, 4318),
        ("ingress", otel, kernel, 4317), ("ingress", otel, kernel, 4318),
    })
    # 精确匹配基线流量；加固追加项额外校验存在性（apiserver ipBlock 无 podSelector 不入 flows 元组）
    assert expected <= flows, f"missing base flows: {expected - flows}"
    extra = flows - expected
    # 允许两类加固追加：外部 443（llm/apiserver）与 ipBlock 控制面通路
    assert all(port == 443 or peer[0] == "ipBlock" for _, _, peer, port in extra), f"unexpected extra flows: {extra}"


def test_secrets_are_external_and_references_resolve(manifests):
    secret = manifests["Secret", "uh-runtime"]
    assert {name for kind, name in manifests if kind == "Secret"} == {"uh-runtime"}
    assert "data" not in secret and secret["type"] == "Opaque"
    entries = secret["stringData"]
    assert set(entries) == {
        "DB_PASSWORD", "UH_LLM_API_KEY", "UH_CLIENT_ID_SECRET", "UH_MEMORY_MASTER_KEY",
        "uh_auth_token", "uh_plugin_trusted_keys",
    }
    assert all(re.fullmatch(r"\$\{[A-Z_][A-Z0-9_]*\}", value) for value in entries.values())
    sensitive = {
        (KERNEL, "UH_LLM_API_KEY"), (KERNEL, "PGPASSWORD"), (KERNEL, "UH_CLIENT_ID_SECRET"),
        (KERNEL, "UH_MEMORY_MASTER_KEY"), ("uh-langgraph", "PGPASSWORD"),
        (CREWAI, "UH_LLM_API_KEY"), ("postgres", "POSTGRES_PASSWORD"),
    }
    found = set()
    secret_volumes = set()
    for (kind, name), doc in manifests.items():
        if kind not in {"Deployment", "StatefulSet"}:
            continue
        pod = doc["spec"]["template"]["spec"]
        for container in pod["containers"]:
            for env in container.get("env", []):
                field = env["name"]
                value = env.get("value", "")
                assert not re.search(r"://[^\s/@:]*:[^\s/@]+@", value)
                if field in {"PGPASSWORD", "UH_CLIENT_ID_SECRET", "UH_MEMORY_MASTER_KEY"} or re.search(
                    r"(?:_PASSWORD|_TOKEN|_KEY|_SECRET)$", field,
                ):
                    assert "value" not in env
                    ref = env["valueFrom"]["secretKeyRef"]
                    assert ref["name"] == "uh-runtime" and ref["key"] in entries
                    found.add((name, field))
        for volume in pod.get("volumes", []):
            if "emptyDir" in volume:
                assert kind == "Deployment" and volume["name"] in {"tmp", "state"}
                assert set(volume) == {"name", "emptyDir"}
                continue
            if "cgroup" in volume:
                # M3 加固（阻断③）：委派 cgroup v2 子树，供 PluginCgroup 写 pids.max
                assert volume["name"] == "cgroup"
                assert set(volume) == {"name", "cgroup"}
                continue
            if "configMap" in volume:
                # M3 可观测性（阻断②）：otel-collector 的配置卷，无凭据
                assert kind == "Deployment"
                assert set(volume) == {"name", "configMap"}
                continue
            ref = volume["secret"]
            assert ref["secretName"] == "uh-runtime"
            assert all(item["key"] in entries for item in ref["items"])
            secret_volumes.add((name, volume["name"], tuple(sorted(
                (item["key"], item["path"]) for item in ref["items"]
            ))))
    assert found == sensitive
    assert secret_volumes == {(KERNEL, "runtime", (
        ("uh_auth_token", "uh_auth_token"), ("uh_plugin_trusted_keys", "uh_plugin_trusted_keys"),
    ))}


@pytest.mark.parametrize(("target", "replicas", "reason"), [
    (CREWAI, 2, "replicas"), (CREWAI, True, "replicas"), (CREWAI, 1.0, "replicas"),
    (KERNEL, 1, "target"), ("other/uh-crewai", 1, "target"),
])
def test_scaler_rejects_invalid_request_before_connecting(monkeypatch, caplog, target, replicas, reason):
    scaler = CrewAIScaler()
    monkeypatch.setattr(scaler, "_client", lambda: pytest.fail("unexpected API request"))
    trace_id = tracing.new_trace_id()
    result = asyncio.run(scaler.scale(target, replicas, trace_id=trace_id))
    assert result == ScaleResult("rejected", reason)
    records = [record for record in caplog.records if record.name == "kernel.scaler"]
    event, = records
    assert event.event == "audit.scaler.result" and event.trace_id == trace_id
    assert event.fields == {"target": target, "replicas": replicas, "result": "rejected", "reason": reason}


def use_mock_api(monkeypatch, scaler, handler):
    monkeypatch.setattr(scaler, "_client", lambda: httpx.AsyncClient(
        base_url="https://kubernetes.test", transport=httpx.MockTransport(handler),
    ))


@pytest.mark.parametrize("replicas", [0, 1])
def test_scaler_patches_only_scale_and_waits_for_current_generation(monkeypatch, caplog, replicas):
    scaler = CrewAIScaler(poll_interval_s=0.001)
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.method == "PATCH":
            assert request.headers["Content-Type"] == "application/merge-patch+json"
            assert json.loads(request.content) == {"spec": {"replicas": replicas}}
            return httpx.Response(200, json={"spec": {"replicas": replicas}})
        status = {"observedGeneration": 1 if len(calls) == 2 else 2}
        if replicas:
            status.update(replicas=1, readyReplicas=1)
        return httpx.Response(200, json={
            "metadata": {"generation": 2}, "spec": {"replicas": replicas}, "status": status,
        })

    use_mock_api(monkeypatch, scaler, handler)
    with caplog.at_level(logging.INFO, logger="kernel.scaler"), tracing.request_context() as trace_id:
        assert asyncio.run(scaler.scale(CREWAI, replicas)) == ScaleResult("ok")
    assert calls == [("PATCH", DEPLOYMENT_API + "/scale"), ("GET", DEPLOYMENT_API), ("GET", DEPLOYMENT_API)]
    event, = [record for record in caplog.records if record.name == "kernel.scaler"]
    assert event.trace_id == trace_id and event.event == "audit.scaler.result"
    assert event.fields == {"target": CREWAI, "replicas": replicas, "result": "ok", "reason": ""}


def test_scaler_times_out_during_readiness_poll_without_a_second_patch(monkeypatch, caplog):
    scaler = CrewAIScaler(timeout_s=0.03, poll_interval_s=0.1)
    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json={
            "metadata": {"generation": 2}, "spec": {"replicas": 1},
            "status": {"observedGeneration": 2, "replicas": 1, "readyReplicas": 0},
        })

    use_mock_api(monkeypatch, scaler, handler)
    assert asyncio.run(scaler.scale(CREWAI, 1)) == ScaleResult("degraded", "timeout")
    assert calls == ["PATCH", "GET"]
    assert any(record.fields["reason"] == "timeout" for record in caplog.records if record.name == "kernel.scaler")


def test_scaler_bounds_the_api_call_itself(monkeypatch):
    scaler = CrewAIScaler(timeout_s=0.03)

    async def stalled(request):
        await asyncio.Event().wait()

    use_mock_api(monkeypatch, scaler, stalled)
    assert asyncio.run(scaler.scale(CREWAI, 1)) == ScaleResult("degraded", "timeout")


def test_scaler_degrades_without_a_cluster(monkeypatch):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.delenv("UH_K8S_API_HOST", raising=False)
    assert asyncio.run(CrewAIScaler().scale(CREWAI, 1)) == ScaleResult("degraded", "cluster_unavailable")


def test_scaler_uses_service_account_credentials_without_proxy_or_redirect(monkeypatch, tmp_path):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
    monkeypatch.setattr(scaler_module, "_SERVICE_ACCOUNT", tmp_path)
    (tmp_path / "token").write_text("synthetic\n", encoding="utf-8")
    ca = scaler_module.ssl.SSLContext(scaler_module.ssl.PROTOCOL_TLS_CLIENT)

    def create_context(*, cafile):
        assert cafile == str(tmp_path / "ca.crt")
        return ca

    monkeypatch.setattr(scaler_module.ssl, "create_default_context", create_context)

    async def check_client():
        async with CrewAIScaler()._client() as client:
            assert client.base_url == httpx.URL("https://10.96.0.1")
            assert client.headers["Authorization"] == "Bearer synthetic"
            assert not client.trust_env and not client.follow_redirects

    asyncio.run(check_client())
