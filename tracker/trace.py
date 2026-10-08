"""Trace log : one JSON object per line, written as it happens.

Every model call, tool call, retry, and budget stop is recorded with: step,
kind, tool, arguments, status, latency, and tokens/credits. Lines are flushed
immediately so a crash or Ctrl+C still leaves a complete record up to that point.
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _short(value: Any, limit: int = 300) -> Any:
    """Keep traces readable: long strings (article text, reports) are truncated."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + f"... [{len(value)} chars]"
    if isinstance(value, dict):
        return {k: _short(v, limit) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_short(v, limit) for v in value[:20]]
    return value


class Trace:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(path, "a", encoding="utf-8")
        self._t0 = time.monotonic()

    def log(self, kind: str, **fields: Any) -> None:
        event = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "t_ms": int((time.monotonic() - self._t0) * 1000),
            "kind": kind,
            **{k: _short(v) for k, v in fields.items() if v is not None},
        }
        self._f.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._f.flush()

    def close(self) -> None:
        self._f.close()