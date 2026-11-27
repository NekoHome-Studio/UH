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


def clean_git_env(env=None):
    """返回移除继承的 GIT_CONFIG_* 的环境副本（M3 复审 P2）。

    背景：Git 安装夹具会用 GIT_CONFIG_COUNT/KEY_n/VALUE_n 注入临时配置。
    若启动环境残留成对不全的键（例如有 GIT_CONFIG_COUNT 但 VALUE_0 为空），
    所有继承该环境的子进程（node → git）都会报
    "missing config value GIT_CONFIG_VALUE_0"，表现为无关用例（如密钥扫描）失败。
    子进程调用统一走这里，避免测试间经环境变量互相污染。
    """
    import os

    base = dict(os.environ if env is None else env)
    for key in [k for k in base if k.startswith("GIT_CONFIG_")]:
        base.pop(key, None)
    return base
