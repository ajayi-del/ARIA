"""Append-only journal + atomic snapshots.

journal/events.jsonl is ETERNAL (never rotated, never rewritten).
Registers are DURABLE snapshots: write tmp + rename, replay journal on boot.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


def append_event(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = dict(record)
    record.setdefault("ts", time.time())
    line = json.dumps(record, separators=(",", ":"), default=str)
    with open(path, "a") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str))
    os.replace(tmp, path)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default
