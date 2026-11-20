import json
from pathlib import Path

import yaml

from kernel.hooks import HookManager
from kernel.plugins import PluginManifest


def register_audit_hook(manager: HookManager, package_dir: str | Path):
    root = Path(package_dir).resolve()
    manifest = PluginManifest.from_dict(yaml.safe_load((root / "manifest.yaml").read_text(encoding="utf-8")))
    log_path = root / "audit.jsonl"

    async def on_event(envelope):
        record = {"event": envelope.action, "trace_id": envelope.trace_id, "event_id": envelope.event_id, "payload": envelope.payload}
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    manager.register(manifest.name, list(manifest.config["events"]), on_event, timeout_ms=int(manifest.config["timeout_ms"]))
    return manifest
