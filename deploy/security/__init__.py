"""安全配置资产。

R2 审查 P0-1：pyproject 将 deploy.security 声明为包，但缺 __init__.py 使其成为
namespace package；editable install 的 finder 会注入伪路径
`__editable__.uh_kernel-*.finder.__path_hook__`，导致
`importlib.resources.files("deploy.security")` 抛
`NotADirectoryError: MultiplexedPath only supports directories`。
按 RELEASE.md 的 `pip install -e .` 步骤安装后门禁必红（418/3/16）。
本文件使该目录成为常规包，消除 MultiplexedPath。
"""
