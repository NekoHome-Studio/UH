"""独立审查 P2-4：契约声明的 getSession/closeSession 曾未实现（D 项自证式校验查不到）。"""
import pytest
from fastapi.testclient import TestClient

from kernel.main import create_app


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("UH_LOCAL_MODE", "1")
    monkeypatch.setenv("UH_AUTH_REQUIRED", "0")
    monkeypatch.setenv("UH_LONG_TERM_STORE", str(tmp_path / "ltm.json"))
    with TestClient(create_app()) as c:
        yield c


def test_contract_session_endpoints_exist_in_implementation(client):
    routes = {(r.path, m) for r in client.app.routes
              for m in (getattr(r, "methods", set()) or set())}
    assert ("/api/v1/sessions/{session_id}", "GET") in routes
    assert ("/api/v1/sessions/{session_id}", "DELETE") in routes


def test_get_session_returns_contract_shape(client):
    sid = client.post("/api/v1/sessions").json()["data"]["session_id"]
    r = client.get(f"/api/v1/sessions/{sid}")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    # Session schema required: session_id / status / created_at
    for key in ("session_id", "status", "created_at"):
        assert key in data, key
    assert data["session_id"] == sid
    assert data["status"] == "active"


def test_get_missing_session_returns_404_usr1003(client):
    r = client.get("/api/v1/sessions/uh_ses_doesnotexist0000")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "UH-USR-1003"


def test_close_session_clears_short_term_memory(client):
    sid = client.post("/api/v1/sessions").json()["data"]["session_id"]
    sent = client.post(f"/api/v1/sessions/{sid}/messages",
                       json={"content": "敏感内容探针", "mode": "chat"})
    assert sent.status_code == 202
    before = client.get(f"/api/v1/memory/short-term?session_id={sid}")
    assert before.status_code == 200
    r = client.delete(f"/api/v1/sessions/{sid}")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["status"] == "closed"
    after = client.get(f"/api/v1/memory/short-term?session_id={sid}").json()["data"]
    entries = after.get("entries") or after.get("messages") or []
    assert not entries, f"L0 短期记忆未清理: {entries}"


def test_closed_session_status_is_reflected(client):
    sid = client.post("/api/v1/sessions").json()["data"]["session_id"]
    client.delete(f"/api/v1/sessions/{sid}")
    assert client.get(f"/api/v1/sessions/{sid}").json()["data"]["status"] == "closed"


def test_delete_missing_session_returns_404(client):
    r = client.delete("/api/v1/sessions/uh_ses_absent00000000")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "UH-USR-1003"
