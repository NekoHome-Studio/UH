import sqlite3
from pathlib import Path

import yaml

from kernel.plugin_sdk import ToolPlugin, run_stdio
from kernel.plugins import PluginManifest


ROOT = Path(__file__).resolve().parent
manifest = PluginManifest.from_dict(yaml.safe_load((ROOT / "manifest.yaml").read_text(encoding="utf-8")))
plugin = ToolPlugin(manifest, package_dir=ROOT)


@plugin.tool(
    "query",
    description="对插件私有 SQLite 数据库执行 SELECT 查询",
    input_schema={"type": "object", "required": ["database", "sql"], "properties": {"database": {"type": "string"}, "sql": {"type": "string"}}},
)
def query(arguments, context):
    sql = str(arguments["sql"]).strip()
    if not sql.lower().startswith("select"):
        raise ValueError("db-query 只允许 SELECT")
    database = context.policy.check_path(arguments["database"], write=False)
    with sqlite3.connect(database) as connection:
        cursor = connection.execute(sql)
        columns = [item[0] for item in cursor.description or ()]
        return {"columns": columns, "rows": [dict(zip(columns, row)) for row in cursor.fetchall()]}


if __name__ == "__main__":
    run_stdio(plugin)
