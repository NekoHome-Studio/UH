from pathlib import Path

import yaml

from kernel.plugin_sdk import ToolPlugin, run_stdio
from kernel.plugins import PluginManifest


ROOT = Path(__file__).resolve().parent
manifest = PluginManifest.from_dict(yaml.safe_load((ROOT / "manifest.yaml").read_text(encoding="utf-8")))
plugin = ToolPlugin(manifest, package_dir=ROOT)


@plugin.tool(
    "read_file",
    description="读取插件沙箱内的 UTF-8 文本",
    input_schema={"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}}},
    risk_level="read-only",
)
def read_file(arguments, context):
    return context.read_text(arguments["path"])


@plugin.tool(
    "write_file",
    description="写入插件沙箱内的 UTF-8 文本",
    input_schema={"type": "object", "required": ["path", "content"], "properties": {"path": {"type": "string"}, "content": {"type": "string"}}},
    risk_level="write-scoped",
    idempotent=False,
)
def write_file(arguments, context):
    context.write_text(arguments["path"], str(arguments["content"]))
    return {"path": str(arguments["path"]), "written": True}


if __name__ == "__main__":
    run_stdio(plugin)
