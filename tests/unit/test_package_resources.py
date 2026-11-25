"""R2 P0-1：editable install 下 importlib.resources 必须能定位部署资产。

pyproject 声明 deploy.security 为包但曾缺 __init__.py，使其成为 namespace package；
editable finder 注入伪路径致 files() 抛 MultiplexedPath only supports directories，
按 RELEASE.md 的 pip install -e . 步骤安装后门禁必红。
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_deploy_packages_have_init():
    """常规包（非 namespace package）才有稳定的 resources 定位。"""
    assert (ROOT / "deploy" / "__init__.py").exists()
    assert (ROOT / "deploy" / "security" / "__init__.py").exists()


def test_resources_files_returns_real_directory():
    """files() 必须返回真实目录而非 MultiplexedPath。"""
    from importlib.resources import files

    anchor = files("deploy.security")
    assert type(anchor).__name__ != "MultiplexedPath", \
        "deploy.security 仍是 namespace package，editable install 下会失败"
    profile = anchor.joinpath("seccomp.json")
    text = profile.read_text(encoding="utf-8")
    assert len(text) > 100
    assert "defaultAction" in text


def test_pyproject_declared_packages_all_have_init():
    """pyproject 声明的每个包都必须是常规包。"""
    import re

    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r"packages\s*=\s*\[([^\]]*)\]", text)
    assert m, "pyproject 未声明 packages"
    names = re.findall(r'"([^"]+)"', m.group(1))
    assert names
    for name in names:
        rel = name.replace(".", "/")
        assert (ROOT / rel / "__init__.py").exists(), \
            f"{name} 缺 __init__.py，editable install 下 resources 定位会失败"
