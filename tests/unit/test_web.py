import json

from fastapi.testclient import TestClient

from kernel.main import create_app


def test_index_served():
    with TestClient(create_app()) as c:
        r = c.get("/")
        assert r.status_code == 200
        assert "UH!" in r.text and "prompt" in r.text


def test_create_session():
    with TestClient(create_app()) as c:
        r = c.post("/api/v1/sessions")
        assert r.status_code == 201
        assert r.json()["data"]["session_id"].startswith("uh_ses_")


def test_message_returns_task_handle():
    with TestClient(create_app()) as c:
        sid = c.post("/api/v1/sessions").json()["data"]["session_id"]
        r = c.post("/api/v1/sessions/" + sid + "/messages", json={"content": "你好", "mode": "auto"})
        assert r.status_code == 202
        data = r.json()["data"]
        assert data["task_id"].startswith("uh_task_")
        assert "/stream" in data["stream_url"] and data["trace_id"].startswith("uh_trc_")


def test_stream_end_to_end():
    with TestClient(create_app()) as c:
        sid = c.post("/api/v1/sessions").json()["data"]["session_id"]
        handle = c.post("/api/v1/sessions/" + sid + "/messages", json={"content": "你好"}).json()["data"]
        with c.stream("GET", handle["stream_url"]) as r:
            assert r.status_code == 200
            text = "".join(chunk for chunk in r.iter_text())
    events = [json.loads(line[len("data: "):]) for line in text.split("\n\n") if line.startswith("data: ")]
    names = [e["event"] for e in events]
    assert "task.created" in names and "delta" in names and "task.finished" in names
    assert all(e["trace_id"] == handle["trace_id"] for e in events)
    ids = [e["event_id"] for e in events]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
    finished = [e for e in events if e["event"] == "task.finished"][0]
    assert "你好" in finished["payload"]["final_result"]["content"]


def test_task_status_and_404():
    with TestClient(create_app()) as c:
        assert c.get("/api/v1/tasks/uh_task_nonexistent").status_code == 404


def _search_events(c, available):
    c.post("/api/v1/dev/capability/retrieval.search/toggle?available=" + str(available).lower())
    sid = c.post("/api/v1/sessions").json()["data"]["session_id"]
    handle = c.post("/api/v1/sessions/" + sid + "/messages", json={"content": "搜索一下 MCP 协议"}).json()["data"]
    with c.stream("GET", handle["stream_url"]) as r:
        text = "".join(chunk for chunk in r.iter_text())
    return [json.loads(line[len("data: "):]) for line in text.split("\n\n") if line.startswith("data: ")]


def test_search_degrades_when_retrieval_off():
    with TestClient(create_app()) as c:
        events = _search_events(c, False)
        names = [e["event"] for e in events]
        assert "task.degraded" in names and "task.finished" in names  # 检索关闭，对话降级仍完成


def test_search_no_degrade_when_retrieval_on():
    with TestClient(create_app()) as c:
        events = _search_events(c, True)
        names = [e["event"] for e in events]
        assert "task.degraded" not in names and "task.finished" in names
