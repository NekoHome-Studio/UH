"""D2 门禁探针：导出 FastAPI 真实路由表供契约覆盖比对。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kernel.main import create_app

app = create_app()
out = []
for r in app.routes:
    p = getattr(r, "path", None)
    for m in (getattr(r, "methods", set()) or set()):
        if p and m not in ("HEAD", "OPTIONS"):
            out.append([m.lower(), p])
print("ROUTES_JSON=" + json.dumps(out))
