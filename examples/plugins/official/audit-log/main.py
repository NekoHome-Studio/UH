"""Append one UH! Hook event to the plugin's private audit log."""
import json
import sys
from pathlib import Path


def main() -> None:
    envelope = json.load(sys.stdin)
    record = {
        "event": envelope["action"],
        "trace_id": envelope["trace_id"],
        "span_id": envelope["span_id"],
        "event_id": envelope["event_id"],
        "timestamp": envelope["ts"],
        "payload": envelope["payload"],
    }
    with Path(__file__).with_name("audit.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
