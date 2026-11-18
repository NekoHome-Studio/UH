"""测试环境隔离（三审 N5）：dev 开启 + 存储/CLI 状态隔离到临时目录，防 pytest 污染仓库 .uh/。"""
import pytest

import kernel.cli as cli


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    monkeypatch.setenv("UH_DEV_MODE", "1")
    # 五个存储后端隔离到 tmp_path
    monkeypatch.setenv("UH_LONG_TERM_STORE", str(tmp_path / "ltm.json"))
    monkeypatch.setenv("UH_LONG_TERM_VECTOR_STORE", str(tmp_path / "ltm_vec.json"))
    monkeypatch.setenv("UH_RETRIEVAL_STORE", str(tmp_path / "ret.json"))
    monkeypatch.setenv("UH_RETRIEVAL_CHROMA", str(tmp_path / "chroma"))
    monkeypatch.setenv("UH_CHECKPOINT_SQLITE_PATH", str(tmp_path / "ckpt.sqlite3"))
    monkeypatch.setenv("UH_CHECKPOINT_JSON_PATH", str(tmp_path / "ckpt.json"))
    # CLI 状态隔离（防跨用例 .uh/ 共享 + 子进程日志竞态）
    state = tmp_path / ".uh"
    monkeypatch.setattr(cli, "STATE", state)
    monkeypatch.setattr(cli, "PID_FILE", state / "dev.pid")
    monkeypatch.setattr(cli, "LOG_FILE", state / "kernel.log")
