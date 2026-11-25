"""R2 P0-3：多副本 + 进程内任务状态 → 必须客户端亲和。"""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _docs(name):
    return [d for d in yaml.safe_load_all((ROOT / "deploy" / "k8s" / name).read_text(encoding="utf-8")) if d]


def test_kernel_service_pins_clients_when_multi_replica():
    kernel = next(d for d in _docs("deployments.yaml")
                  if d.get("kind") == "Deployment" and d["metadata"]["name"] == "uh-kernel")
    replicas = kernel["spec"]["replicas"]
    svc = next(d for d in _docs("services.yaml")
               if d.get("kind") == "Service" and d["metadata"]["name"] == "uh-kernel")
    if replicas >= 2:
        assert svc["spec"].get("sessionAffinity") == "ClientIP", \
            "任务状态进程内时，多副本必须客户端亲和，否则约 50% 跨 worker 404"
        assert svc["spec"]["sessionAffinityConfig"]["clientIP"]["timeoutSeconds"] > 0


def test_affinity_limitation_is_documented():
    doc = (ROOT / "docs" / "deployment" / "multiprocess.md").read_text(encoding="utf-8")
    assert "sessionAffinity" in doc
    assert "权宜" in doc or "ingress" in doc, "须说明亲和是权宜且经 ingress 会失效"


def test_cross_worker_task_lookup_is_known_limitation():
    """双 app 实例模拟两 worker：B 查 A 的任务应 404（已知限制，非回归）。"""
    from fastapi.testclient import TestClient
    from kernel.main import create_app

    with TestClient(create_app()) as a, TestClient(create_app()) as b:
        sid = a.post("/api/v1/sessions").json()["data"]["session_id"]
        tid = a.post(f"/api/v1/sessions/{sid}/messages",
                     json={"content": "probe", "mode": "chat"}).json()["data"]["task_id"]
        assert a.get(f"/api/v1/tasks/{tid}").status_code == 200
        # 进程内状态的必然结果；亲和配置是部署侧的缓解手段
        assert b.get(f"/api/v1/tasks/{tid}").status_code == 404
