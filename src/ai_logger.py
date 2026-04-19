import json
from datetime import datetime, timezone
from pathlib import Path


def log_call(path: Path | str, record: dict) -> None:
    record = dict(record)
    record.setdefault("ts", datetime.now(timezone.utc).isoformat())
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
