"""Cross-service JSONL trace (PDF §13).

Every line has the required fields, in this order:
    ts, service, run_id, request_id, step, direction (in|out), endpoint_or_tool, status,
    latency_ms, attempt, tokens
plus optional extra fields (detail, http_status, ...).

Rules this module enforces:
  - latency is measured by the CALLER on its own time.monotonic() clock and passed in; nothing here
    subtracts wall-clock timestamps taken on two machines;
  - `tokens` is a model token COUNT (an int); secrets never reach the log: extra fields whose name
    looks like a credential are dropped, and long strings are truncated.
Each process owns its own TraceLog (no state shared between services).
"""
import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REQUIRED = ("ts", "service", "run_id", "request_id", "step", "direction", "endpoint_or_tool", "status",
            "latency_ms", "attempt", "tokens")
_SECRET_WORDS = ("authorization", "api_key", "apikey", "password", "secret", "bearer", "credential")


def _short(value: Any, limit: int = 300) -> Any:
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + f"... [{len(value)} chars]"
    if isinstance(value, dict):
        return {k: _short(v, limit) for k, v in value.items() if not _secretish(k)}
    if isinstance(value, (list, tuple)):
        return [_short(v, limit) for v in value[:20]]
    return value


def _secretish(key: str) -> bool:
    k = str(key).lower()
    return any(w in k for w in _SECRET_WORDS) or k in ("token", "service_token", "access_token")


class TraceLog:
    def __init__(self, service: str, path: Path | str | None, *, stdout: bool = False):
        self.service = service
        self.path = Path(path) if path else None
        self.stdout = stdout
        self._lock = threading.Lock()
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, *, run_id: str | None, request_id: str | None, step: int | None, direction: str,
            endpoint_or_tool: str, status: str, latency_ms: int | None = None, attempt: int | None = 1,
            tokens: int | None = 0, **extra: Any) -> dict:
        if direction not in ("in", "out"):
            raise ValueError("direction must be 'in' or 'out'")
        event = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "service": self.service,
            "run_id": run_id,
            "request_id": request_id,
            "step": step,
            "direction": direction,
            "endpoint_or_tool": endpoint_or_tool,
            "status": status,
            "latency_ms": latency_ms,
            "attempt": attempt,
            "tokens": int(tokens or 0),
        }
        for k, v in extra.items():
            if v is not None and not _secretish(k) and k not in event:
                event[k] = _short(v)
        line = json.dumps(event, ensure_ascii=False)
        with self._lock:
            if self.path:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            if self.stdout:
                print(line, file=sys.stdout, flush=True)
        return event

    def read(self, run_id: str | None = None, limit: int = 5000) -> list[dict]:
        """Lines for one run (used by the verifier's authenticated GET /trace on Render, where the
        file is the only copy). Malformed lines are skipped."""
        if not self.path or not self.path.exists():
            return []
        out = []
        with self._lock, open(self.path, encoding="utf-8") as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if run_id is None or ev.get("run_id") == run_id:
                    out.append(ev)
        return out[-limit:]
