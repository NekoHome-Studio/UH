"""测试环境默认开启 dev 端点（生产默认关闭，见 _dev_enabled）。"""
import pytest


@pytest.fixture(autouse=True)
def _dev_mode(monkeypatch):
    monkeypatch.setenv("UH_DEV_MODE", "1")
