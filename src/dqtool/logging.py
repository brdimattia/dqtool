"""Structured run logs: JSON lines tied together by a run ID (spec: Logging)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def to_json(value: Any) -> Any:
    """json.dumps default for DuckDB values (timestamps, dates, decimals)."""
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return float(value)


class RunLog:
    def __init__(self, path: Path, run_id: str):
        self.path = path
        self.run_id = run_id

    def event(self, event: str, **fields: Any) -> None:
        record = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "run_id": self.run_id,
            "event": event,
            **fields,
        }
        with self.path.open("a") as f:
            f.write(json.dumps(record, default=to_json) + "\n")
