"""M3 独立复审 P1：会话归属 fail-open 与会话关闭不跨副本传播。"""
import pytest
from fastapi.testclient import TestClient

from kernel.main import create_app
from kernel.session_registry import SessionRegistry

TOKEN = "probe-token-0123456789abcdef"


def h(cid=None):
    headers = {"Authorization": f"Bearer {TOKEN}"}
    if cid:
        headers["X-UH-Client-ID"] = cid
    return headers


def _app_pair(monkeypatch, tmp_path):
    monkeypatch.setenv("UH_LOCAL_MODE", "0")
    monkeypatch.setenv("UH_AUTH_REQUIRED", "1")
    monkeypatch.setenv("UH_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("UH_LONG_TERM_STORE", str(tmp_path / "ltm.json"))
    # 两个副本必须共享签名密钥，否则 client_id 跨副本验签失败
    # （生产由 Redis SETNX 派生，见 _derive_shared_identity_secret）
    monkeypatch.setenv("UH_CLIENT_ID_SECRET", "shared-secret-for-test-0123456789")
    shared = SessionRegistry()
    a = create_app()
    b = create_app()
    a.state.session_registry = shared
    b.state.session_registry = shared
    return a, b


def _create(c):
    r = c.post("/api/v1/sessions", headers=h())
    assert r.status_code == 201, r.text
    return r.json()["data"]


def test_guard_requires_client_id_in_strict_mode(monkeypatch, tmp_path):
    """无任何身份凭据（无 cookie / 无 X-UH-Client-ID）时必须 403。

    注意：浏览器同源请求靠 set_cookie 自动带 uh_client_id，属正常路径；
    这里显式清空 cookie 模拟「缺签名身份」的请求。
    """
    app, _ = _app_pair(monkeypatch, tmp_path)
    with TestClient(app) as c:
        sid = _create(c)["session_id"]
        c.cookies.clear()
        r = c.get(f"/api/v1/sessions/{sid}", headers=h())
        assert r.status_code == 403, r.text
        assert r.json()["error"]["code"] == "UH-USR-1002"


def test_guard_rejects_forged_identity_without_valid_cookie(monkeypatch, tmp_path):
    """伪造的 X-UH-Client-ID + 无合法 cookie → 拒绝。

    注意：伪造 header 会被跳过并回退到合法 cookie（防恶意 header 遮挡浏览器
    真实身份），所以必须同时清 cookie 才能验证「无有效身份」路径。
    """
    app, _ = _app_pair(monkeypatch, tmp_path)
    with TestClient(app) as c:
        sid = _create(c)["session_id"]
        c.cookies.clear()
        r = c.get(f"/api/v1/sessions/{sid}", headers=h("uhcid1.forged.sig"))
        assert r.status_code == 403, r.text
        assert r.json()["error"]["code"] == "UH-USR-1002"


def test_closed_status_propagates_across_replicas(monkeypatch, tmp_path):
    app_a, app_b = _app_pair(monkeypatch, tmp_path)
    with TestClient(app_a) as a, TestClient(app_b) as b:
        created = _create(a)
        sid, cid = created["session_id"], created["client_id"]
        closed = b.delete(f"/api/v1/sessions/{sid}", headers=h(cid))
        assert closed.status_code == 200, closed.text
        assert closed.json()["data"]["status"] == "closed"
        seen = a.get(f"/api/v1/sessions/{sid}", headers=h(cid))
        assert seen.status_code == 200
        assert seen.json()["data"]["status"] == "closed", "关闭状态未跨副本传播"


def test_closed_session_rejects_new_messages(monkeypatch, tmp_path):
    app_a, app_b = _app_pair(monkeypatch, tmp_path)
    with TestClient(app_a) as a, TestClient(app_b) as b:
        created = _create(a)
        sid, cid = created["session_id"], created["client_id"]
        b.delete(f"/api/v1/sessions/{sid}", headers=h(cid))
        sent = a.post(f"/api/v1/sessions/{sid}/messages", headers=h(cid),
                      json={"content": "after close", "mode": "chat"})
        assert sent.status_code == 409, sent.text
        assert sent.json()["error"]["code"] == "UH-USR-1003"


def test_created_at_comes_from_registry_not_query_time(monkeypatch, tmp_path):
    app_a, app_b = _app_pair(monkeypatch, tmp_path)
    with TestClient(app_a) as a, TestClient(app_b) as b:
        created = _create(a)
        sid, cid, original = created["session_id"], created["client_id"], created["created_at"]
        seen = b.get(f"/api/v1/sessions/{sid}", headers=h(cid))
        assert seen.json()["data"]["created_at"] == original


# ---------- 全仓审计 #1：消息与短期记忆端点越权 ----------
@pytest.mark.parametrize("path_tpl", [
    "/api/v1/sessions/{sid}/messages",
    "/api/v1/memory/short-term?session_id={sid}",
])
def test_message_and_memory_endpoints_require_ownership(monkeypatch, tmp_path, path_tpl):
    """持有全局 token 但无该会话身份时，不得读取他人消息/短期记忆。"""
    app, _ = _app_pair(monkeypatch, tmp_path)
    with TestClient(app) as c:
        sid = _create(c)["session_id"]
        c.cookies.clear()
        r = c.get(path_tpl.format(sid=sid), headers=h())
        assert r.status_code == 403, r.text
        assert r.json()["error"]["code"] == "UH-USR-1002"


def test_owner_can_still_read_messages_and_memory(monkeypatch, tmp_path):
    """合法属主（带 client_id）读取不受影响。"""
    app, _ = _app_pair(monkeypatch, tmp_path)
    with TestClient(app) as c:
        created = _create(c)
        sid, cid = created["session_id"], created["client_id"]
        c.post(f"/api/v1/sessions/{sid}/messages", headers=h(cid),
               json={"content": "属主自读", "mode": "chat"})
        for path in (f"/api/v1/sessions/{sid}/messages?page=1&page_size=20",
                     f"/api/v1/memory/short-term?session_id={sid}"):
            r = c.get(path, headers=h(cid))
            assert r.status_code == 200, (path, r.text)
