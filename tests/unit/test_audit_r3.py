"""2026-11-28 审计第 2 轮：形状缓存绕过 / 取消枚举 / 索引漏召回 / 短期记忆不进 LLM。"""
import base64
import json
from pathlib import Path
from dataclasses import asdict

import pytest

from kernel import event_mapping
from kernel.event_mapping import EventValidationError, validate_external_event
from kernel.retrieval import Document, LocalVectorStore

TASK = "uh_task_mapping1"
TRACE = "uh_trc_mapping1"


def _ev(payload, event_id=1, event="delta"):
    return {"event_id": event_id, "event": event, "version": 1, "task_id": TASK,
            "trace_id": TRACE, "span_id": "sp_1", "ts": "2026-11-16T00:00:00Z",
            "payload": payload}


# ---------- 形状缓存不得绕过值级约束 ----------
def test_same_shape_invalid_pattern_rejected():
    """session_id 有 pattern 约束；同类型非法值必须被拒（原实现按类型名缓存而放行）。"""
    event_mapping._SHAPE_PASSED.clear()
    validate_external_event(_ev({"session_id": "uh_ses_abc123", "mode": "chat"},
                                event=("task.created")))
    with pytest.raises(EventValidationError):
        validate_external_event(_ev({"session_id": "NOT-A-SESSION-ID", "mode": "chat"},
                                    event_id=2, event="task.created"))


def test_same_shape_invalid_minimum_rejected():
    """index 有 minimum: 0；负值必须被拒。"""
    event_mapping._SHAPE_PASSED.clear()
    validate_external_event(_ev({"text": "a", "index": 0}))
    with pytest.raises(EventValidationError):
        validate_external_event(_ev({"text": "b", "index": -5}, event_id=2))


def test_same_shape_extra_key_rejected():
    """additionalProperties: false；多余键必须被拒。"""
    event_mapping._SHAPE_PASSED.clear()
    validate_external_event(_ev({"text": "a", "index": 0}))
    with pytest.raises(EventValidationError):
        validate_external_event(_ev({"text": "b", "index": 1, "extra": 1}, event_id=2))


def test_shape_cache_still_effective_for_valid_repeats():
    """合法重复仍走快速路径（性能特性不能因加固而丢失）。"""
    event_mapping._SHAPE_PASSED.clear()
    for i in range(10):
        validate_external_event(_ev({"text": "x", "index": i}, event_id=i + 1))
    assert len(event_mapping._SHAPE_PASSED) >= 1


# ---------- 取消原因必须在契约枚举内 ----------
def test_cancel_session_reason_is_contract_valid():
    """task.cancelled.reason 枚举只有 4 值；session_closed 不在其中会导致事件校验失败。"""
    schema = json.loads(
        (Path(__file__).resolve().parents[2] / "docs" / "contracts" / "events.schema.json")
        .read_text(encoding="utf-8"))
    allowed = set(schema["$defs"]["p_task_cancelled"]["properties"]["reason"]["enum"])
    import inspect
    from kernel.tasks import TaskManager

    src = inspect.getsource(TaskManager.cancel_session)
    default = src.split("reason: str = ")[1].split(")")[0].strip().strip(chr(34) + chr(39))
    assert default in allowed, f"默认 reason {default!r} 不在契约枚举 {sorted(allowed)}"


# ---------- 载入后索引必须包含已加载文档 ----------
def test_loaded_documents_are_indexed(tmp_path):
    """重启后首次 add 不得只索引新文档；旧内容必须仍可召回。"""
    path = tmp_path / "vec.json"
    path.write_text(json.dumps([
        asdict(Document("old1", "历史文档 独有词汇甲", "s", "long-term"))
    ], ensure_ascii=False), encoding="utf-8")
    store = LocalVectorStore(path=path)
    assert store._inverted, "载入后倒排索引为空"
    store.add(Document("new1", "新文档 独有词汇乙", "s", "long-term"))
    hits = [d.document_id for d, _ in store.query("独有词汇甲", top_k=5)]
    assert "old1" in hits, f"旧文档漏召回: {hits}"


# ---------- 短期记忆必须进入 LLM 上下文 ----------
def test_build_messages_includes_history():
    from kernel.tasks import _build_messages

    history = [
        {"role": "user", "content": "第一问", "ts": 1.0},
        {"role": "assistant", "content": "第一答", "ts": 2.0},
    ]
    msgs = _build_messages(history, "第二问")
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[0]["content"] == "第一问"
    assert msgs[-1]["content"] == "第二问"


def test_build_messages_skips_invalid_entries():
    from kernel.tasks import _build_messages

    history = [{"role": "system", "content": "x"}, {"role": "user", "content": ""},
               {"role": "user", "content": "有效"}, "not-a-dict"]
    msgs = _build_messages(history, "当前")
    assert [m["content"] for m in msgs] == ["有效", "当前"]


def test_chat_passes_history_to_llm(monkeypatch, tmp_path):
    """端到端：第二轮请求必须带上一轮对话。"""
    import asyncio
    import os

    monkeypatch.setenv("UH_LOCAL_MODE", "1")
    monkeypatch.setenv("UH_AUTH_REQUIRED", "0")
    monkeypatch.setenv("UH_LONG_TERM_STORE", str(tmp_path / "ltm.json"))
    from fastapi.testclient import TestClient

    from kernel.main import create_app

    app = create_app()
    captured = []
    real = app.state.llm.stream_chat

    async def spy(messages, **kw):
        captured.append([dict(m) for m in messages])
        async for chunk in real(messages, **kw):
            yield chunk

    app.state.llm.stream_chat = spy
    with TestClient(app) as c:
        sid = c.post("/api/v1/sessions").json()["data"]["session_id"]
        for q in ("第一轮", "第二轮"):
            tid = c.post(f"/api/v1/sessions/{sid}/messages",
                         json={"content": q, "mode": "chat"}).json()["data"]["task_id"]
            for _ in range(200):
                if app.state.tasks.status(tid) in ("finished", "failed", "cancelled"):
                    break
                asyncio.run(asyncio.sleep(0.01)) if False else time_sleep()
    assert len(captured) == 2
    assert len(captured[0]) == 1, "第一轮应只有当前输入"
    assert len(captured[1]) >= 3, f"第二轮未带历史: {captured[1]}"


def time_sleep():
    import time

    time.sleep(0.02)
