"""官方工具模板的 manifest 与入口回归。"""
from pathlib import Path

from kernel.plugins import PluginDriver, PluginSecurityConfig


ROOT = Path(__file__).resolve().parents[2]


def test_official_tools_validate_in_local_mode():
    driver = PluginDriver(security=PluginSecurityConfig(allow_unverified=True))
    for name in ("web-search", "file", "db-query"):
        result = driver.validate(ROOT / "examples" / "plugins" / "official" / name, no_verify=True)
        assert result["status"] == "validated"
        assert result["type"] == "tool"

