import base64
import logging

from kernel.long_term_memory import LongTermMemory
from kernel.retrieval import LocalVectorStore


def make_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("UH_MEMORY_MASTER_KEY", base64.b64encode(b"0123456789abcdef0123456789abcdef").decode())
    return LongTermMemory(path=tmp_path / "memory.json", vector_store=LocalVectorStore(tmp_path / "vectors.json"))


def test_l3_is_rejected_and_warned(tmp_path, monkeypatch, caplog):
    memory = make_memory(tmp_path, monkeypatch)
    with caplog.at_level(logging.WARNING):
        assert memory.write("身份证号 11010519491231002X") is None
    assert memory.entry_count == 0
    assert memory.vector_store.documents == []
    assert "memory.long_term.rejected" in caplog.text


def test_l2_is_encrypted_and_never_semantically_indexed(tmp_path, monkeypatch):
    memory = make_memory(tmp_path, monkeypatch)
    entry = memory.write("我的手机号是 13812345678", session_id="s", metadata={"kind": "contact"})
    assert entry is not None and entry.privacy_level == "L2"
    assert entry.ciphertext and "13812345678" not in entry.ciphertext
    assert memory.vector_store.documents == []
    assert memory.recall("13812345678") == []
    assert memory.recall("ignored", session_id="s", metadata={"kind": "contact"}, include_sensitive=True)[0]["content"] == "我的手机号是 13812345678"


def test_l1_recall_and_purge_export(tmp_path, monkeypatch):
    memory = make_memory(tmp_path, monkeypatch)
    entry = memory.write("我喜欢在海边散步", session_id="s")
    assert memory.recall("海边散步")[0]["entry_id"] == entry.entry_id
    assert memory.export()[0]["content"] == "我喜欢在海边散步"
    assert memory.purge() == 1
    assert memory.entry_count == 0 and memory.vector_store.documents == []


def test_memory_recall_false_does_not_write(tmp_path, monkeypatch):
    memory = make_memory(tmp_path, monkeypatch)
    assert memory.write("普通内容", session_id="incognito", memory_recall=False) is None
    assert memory.entry_count == 0
