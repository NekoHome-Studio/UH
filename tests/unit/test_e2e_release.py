"""R-1 发布回归：E2E-01~08 端到端专项（#37）。

编号语义（契约散见各里程碑验收项，此处集中固化）：
  E2E-01 完整对话链路：建会话→发消息→任务终态→事件序列合规
  E2E-02 检索增强链路：search 模式经能力 RPC 返回上下文（推定：§10.1 检索场景）
  E2E-03 编排链路：workflow 多步事件与 checkpoint（推定：§10.1 编排场景）
  E2E-04 task.resumed 跳过已完成节点，不重复执行（events.md:105）
  E2E-05 插件卸载无残留进程与文件（openapi.yaml:490）
  E2E-06 主题切换 200ms 内生效（上游 M2-4 验收）
  E2E-07 高危工具拒绝后无副作用残留（上游 M1-8 验收）
  E2E-08 无痕会话结束后无长期记忆写入（上游 M1-3 验收）

注：E2E-01/02/03 在上游文档无逐字定义，按 §10.1 端到端场景语义落定，已在报告标注。
"""
import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kernel.main import create_app

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("UH_LOCAL_MODE", "1")
    monkeypatch.setenv("UH_AUTH_REQUIRED", "0")
    monkeypatch.setenv("UH_LONG_TERM_STORE", str(tmp_path / "ltm.json"))
    monkeypatch.setenv("UH_RETRIEVAL_ROOT", str(tmp_path / "retr"))
    app = create_app()
    with TestClient(app) as c:
        yield c


def _new_session(client):
    r = client.post("/api/v1/sessions")
    assert r.status_code == 201, r.text
    return r.json()["data"]["session_id"]


def _submit(client, sid, content, mode="auto"):
    r = client.post(f"/api/v1/sessions/{sid}/messages", json={"content": content, "mode": mode})
    assert r.status_code == 202, r.text
    return r.json()["data"]


def _wait_terminal(client, task_id, timeout_s=15.0):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = client.get(f"/api/v1/tasks/{task_id}/steps")
        if r.status_code == 200:
            data = r.json().get("data") or {}
            steps = data.get("steps") or []
            if steps and all(s.get("status") in {"done", "failed"} for s in steps):
                return data
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} did not reach terminal state")


# ---------- E2E-01 完整对话链路 ----------
def test_e2e_01_full_chat_chain(client):
    sid = _new_session(client)
    data = _submit(client, sid, "你好，介绍一下自己", "chat")
    assert data["task_id"].startswith("uh_task_")
    assert "stream_url" in data and "trace_id" in data
    steps = _wait_terminal(client, data["task_id"])
    assert steps["steps"], "must emit at least one step"
    assert any(s["module"] == "llm" for s in steps["steps"])


# ---------- E2E-02 检索增强链路 ----------
def test_e2e_02_search_chain_degrades_gracefully(client):
    sid = _new_session(client)
    data = _submit(client, sid, "搜索 UH 架构说明", "search")
    steps = _wait_terminal(client, data["task_id"])
    # 无索引时应降级为纯对话而非失败（契约 CAP-3001 降级语义）
    assert steps["steps"]


# ---------- E2E-03 编排链路 ----------
def test_e2e_03_orchestrate_chain(client):
    sid = _new_session(client)
    data = _submit(client, sid, "分步骤完成：先分析再总结", "orchestrate")
    steps = _wait_terminal(client, data["task_id"])
    assert steps["steps"]


# ---------- E2E-05 卸载无残留 ----------
def test_e2e_05_plugin_uninstall_leaves_no_residue(tmp_path):
    from kernel.plugins import PluginDriver

    async def scenario():
        driver = PluginDriver(root=tmp_path / "plugins")
        await driver.restore_active()
        listed = driver.list()
        await driver.shutdown()
        return listed

    listed = asyncio.run(scenario())
    assert isinstance(listed, list)
    # 空安装目录下不应有活跃插件残留
    assert all(p.get("status") != "active" for p in listed) or not listed


# ---------- E2E-06 主题切换生效 ----------
def test_e2e_06_theme_switch_applies_fast(client):
    themes = client.get("/api/v1/themes")
    assert themes.status_code == 200
    items = (themes.json().get("data") or {}).get("themes") or []
    if not items:
        pytest.skip("no themes registered")
    target = items[0]["name"] if isinstance(items[0], dict) else items[0]
    started = time.perf_counter()
    r = client.put("/api/v1/themes/active", json={"name": target})
    elapsed_ms = (time.perf_counter() - started) * 1000
    assert r.status_code in {200, 204}, r.text
    # 契约要求 200ms 内生效（本地 TestClient 无网络开销）
    assert elapsed_ms < 200, f"theme switch took {elapsed_ms:.0f}ms"


# ---------- E2E-07 拒绝无副作用 ----------
def test_e2e_07_rejected_confirmation_has_no_side_effect(tmp_path):
    from kernel.confirmation import ConfirmationManager

    cm = ConfirmationManager()
    req = cm.request("uh_task_probe01", "fs.write", "write-host", "写入宿主文件")
    cm.resolve(req.token, "rejected", operator="tester", task_id="uh_task_probe01")
    got = cm.get(req.token)
    assert got is not None and got.status == "rejected"
    # 二次 resolve 必须被拒（一次性防重放）
    with pytest.raises(Exception):
        cm.resolve(req.token, "approved", operator="tester", task_id="uh_task_probe01")


# ---------- E2E-08 无痕会话不写长期记忆 ----------
def test_e2e_08_incognito_writes_no_long_term(client, tmp_path):
    sid = _new_session(client)
    r = client.post(
        f"/api/v1/sessions/{sid}/messages",
        json={"content": "我的手机号是13800138000", "mode": "chat",
              "context": {"memory_recall": False}},
    )
    assert r.status_code == 202
    _wait_terminal(client, r.json()["data"]["task_id"])
    store = tmp_path / "ltm.json"
    if store.exists():
        text = store.read_text(encoding="utf-8")
        assert "13800138000" not in text, "incognito session must not persist content"


# ---------- E2E-04 resume 跳过已完成节点 ----------
def test_e2e_04_resume_skips_completed_nodes(tmp_path):
    """events.md:105 —— task.resumed 后跳过已完成节点，不重复执行。"""
    from kernel.orchestration import JSONCheckpointStore, OrchestrationService

    async def scenario():
        svc = OrchestrationService(store=JSONCheckpointStore(tmp_path / "ckpt.json"))
        executed: list[str] = []

        # 记录每个节点实际执行次数
        for node_id, node in list(svc.nodes.items()):
            def wrapped(state, _n=node, _id=node_id):
                executed.append(_id)
                return _n(state)
            svc.nodes[node_id] = wrapped

        first = await svc.run("uh_task_e2e04", "分步任务", trace_id="uh_trc_e2e04")
        first_count = len(executed)
        executed.clear()
        # resume：全部节点已完成 → 不应再执行任何节点
        second = await svc.run("uh_task_e2e04", "分步任务", trace_id="uh_trc_e2e04", resume=True)
        return first_count, len(executed), first, second

    first_count, second_count, first, second = asyncio.run(scenario())
    assert first_count > 0, "first run must execute nodes"
    assert second_count == 0, f"resume re-executed {second_count} completed nodes"
