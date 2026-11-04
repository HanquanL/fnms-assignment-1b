"""Background runs started from the API. A run takes minutes, so POST /api/tracker/orchestrate
returns 202 with the run_id at once and the page polls GET /api/tracker/orchestrate/{run_id}.

Progress lives in this process's memory (it is only "what stage is it at"; the run's results go
to the database when it finishes). One run at a time per user."""
import threading
import uuid
from datetime import datetime, timezone

from app.db import SessionLocal
from app.orchestrator.service import Orchestrator
from app.orchestrator.settings import load_settings
from app.orchestrator.worker_client import WorkerClient
from shared.tracelog import TraceLog

_lock = threading.Lock()
_progress: dict[str, dict] = {}
_orchestrator: Orchestrator | None = None


def orchestrator() -> Orchestrator:
    global _orchestrator
    if _orchestrator is None:
        s = load_settings()
        if not s.researcher.token or not s.verifier.token:
            raise RuntimeError("RESEARCHER_TOKEN and VERIFIER_TOKEN must be set (repo-root .env or environment)")
        trace = TraceLog("orchestrator", s.trace_path)
        _orchestrator = Orchestrator(s, SessionLocal, WorkerClient(s, trace), trace)
    return _orchestrator


def start(user_id) -> dict:
    orch = orchestrator()
    with _lock:
        for p in _progress.values():
            if p["user_id"] == user_id and p["state"] == "running":
                return {"conflict": True, **_public(p)}
        run_id = str(uuid.uuid4())
        p = {"run_id": run_id, "user_id": user_id, "state": "running", "stage": "starting",
             "started_at": datetime.now(timezone.utc).isoformat(), "error": None}
        _progress[run_id] = p

    def stage(name: str) -> None:
        p["stage"] = name

    def work() -> None:
        try:
            orch.run(user_id, run_id=run_id, progress=stage)
            p["state"], p["stage"] = "done", "done"
        except Exception as e:  # noqa: BLE001 -- e.g. the database is unreachable when saving
            p["state"], p["error"] = "error", f"{type(e).__name__}: {str(e)[:300]}"

    threading.Thread(target=work, daemon=True, name=f"run-{run_id}").start()
    return _public(p)


def status(run_id: str, user_id) -> dict | None:
    p = _progress.get(run_id)
    if p is None or p["user_id"] != user_id:
        return None
    return _public(p)


def _public(p: dict) -> dict:
    return {k: v for k, v in p.items() if k != "user_id"}
