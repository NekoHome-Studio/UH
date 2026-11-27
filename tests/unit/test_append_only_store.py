"""v0.2.0 性能：长期记忆与向量库改为追加式存储（原 O(n²) 全量重写）。"""
import json
import time
from pathlib import Path

import pytest

from kernel.long_term_memory import create_long_term_memory
from kernel.retrieval import Document, LocalVectorStore


@pytest.fixture
def ltm(monkeypatch, tmp_path):
    monkeypatch.setenv("UH_LONG_TERM_STORE", str(tmp_path / "ltm.json"))
    monkeypatch.setenv("UH_MEMORY_MASTER_KEY", "")
    return create_long_term_memory()


def test_vector_store_write_is_not_linear_in_backlog(tmp_path):
    """写入成本不得随存量线性增长（原实现每次 add 全量重写 JSON）。"""
    store = LocalVectorStore(path=tmp_path / "vec.json")
    for i in range(400):
        store.add(Document(f"d{i}", f"内容 {i} 用于建立存量", "probe", "long-term"))
    started = time.perf_counter()
    for i in range(20):
        store.add(Document(f"late{i}", f"新内容 {i}", "probe", "long-term"))
    per_add = (time.perf_counter() - started) * 1000 / 20
    assert per_add < 50, f"单次 add {per_add:.1f}ms，疑似仍在全量重写"


def test_long_term_write_is_not_linear_in_backlog(ltm):
    for i in range(300):
        ltm.write(f"存量条目 {i}", session_id="uh_ses_probe0000000000000001")
    started = time.perf_counter()
    for i in range(20):
        ltm.write(f"新条目 {i}", session_id="uh_ses_probe0000000000000001")
    per_write = (time.perf_counter() - started) * 1000 / 20
    assert per_write < 50, f"单次 write {per_write:.1f}ms，疑似仍在全量重写"


def test_vector_store_persists_as_jsonl(tmp_path):
    store = LocalVectorStore(path=tmp_path / "vec.json")
    store.add(Document("d1", "第一条", "probe", "long-term"))
    store.add(Document("d2", "第二条", "probe", "long-term"))
    lines = [l for l in (tmp_path / "vec.json").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) >= 2, "应为 JSONL 追加格式"
    assert all(json.loads(l)["document_id"] for l in lines)


def test_last_write_wins_on_reload(tmp_path):
    """同 id 后写覆盖前写（追加式的归并语义）。"""
    store = LocalVectorStore(path=tmp_path / "vec.json")
    store.add(Document("dup", "旧内容", "probe", "long-term"))
    store.add(Document("dup", "新内容", "probe", "long-term"))
    reloaded = LocalVectorStore(path=tmp_path / "vec.json")
    hits = [d for d in reloaded.documents if d.document_id == "dup"]
    assert len(hits) == 1
    assert hits[0].text == "新内容"


def test_legacy_json_array_format_still_loads(tmp_path):
    """旧版全量 JSON 数组必须仍可读（升级不丢数据）。"""
    path = tmp_path / "vec.json"
    doc = Document("old1", "旧格式内容", "probe", "long-term")
    from dataclasses import asdict

    path.write_text(json.dumps([asdict(doc)], ensure_ascii=False), encoding="utf-8")
    store = LocalVectorStore(path=path)
    assert [d.document_id for d in store.documents] == ["old1"]


def test_long_term_roundtrip_after_append(ltm):
    ltm.write("可检索的条目", session_id="uh_ses_probe0000000000000002")
    rows = ltm.recall("可检索", top_k=3)
    assert any("可检索" in str(r.get("content", "")) for r in rows)
