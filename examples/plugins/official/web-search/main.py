from pathlib import Path

import yaml

from kernel.plugin_sdk import ToolPlugin, run_stdio
from kernel.plugins import PluginManifest


ROOT = Path(__file__).resolve().parent
manifest = PluginManifest.from_dict(yaml.safe_load((ROOT / "manifest.yaml").read_text(encoding="utf-8")))
plugin = ToolPlugin(manifest, package_dir=ROOT)


@plugin.tool(
    "web_search",
    description="查询允许的搜索站点",
    input_schema={"type": "object", "required": ["query"], "properties": {"query": {"type": "string", "minLength": 1}}},
)
def web_search(arguments, context):
    context.allow_host("https://api.duckduckgo.com")
    query = str(arguments.get("query", "")).strip()
    return {"query": query, "status": "proxy_required", "message": "由内核代理执行外部搜索"}


if __name__ == "__main__":
    run_stdio(plugin)
