"""Verifier service.

    uvicorn verifier.app:app --port 8002            (local, from the repository root)
    uvicorn verifier.app:app --host 0.0.0.0 --port $PORT   (Render)

Environment (secrets never in the repo; on Render they live in the service's Environment settings):
    VERIFIER_TOKEN       bearer token the orchestrator must send (required; the app won't start without it)
    GEMINI_API_KEY       model key
    VERIFIER_MODEL       optional, overrides config.yaml verifier.model
    VERIFIER_TRACE_PATH  optional, default traces/verifier.jsonl
    VERIFIER_TRACE_STDOUT  "1" to also print trace lines to stdout (on by default on Render)
Locally, a git-ignored repo-root .env is read too (real environment variables win).
"""
import os
from pathlib import Path

import yaml
from fastapi import Query, Request
from fastapi.responses import JSONResponse

from shared.contract import VerifyPayload, error_body
from shared.tracelog import TraceLog
from shared.workerkit import Cooldown, Reply, create_worker_app, token_ok
from verifier.core import Deps, run_verify
from verifier.fetchguard import FetchPolicy
from verifier.judge import GeminiJudge

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _config() -> dict:
    path = ROOT / "config.yaml"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def build_app(deps: Deps | None = None, *, token: str | None = None, trace: TraceLog | None = None):
    _load_dotenv()
    cfg = _config()
    vcfg = cfg.get("verifier") or {}
    if deps is None:
        policy = FetchPolicy.from_config(cfg["fetch"]) if "fetch" in cfg else FetchPolicy()
        judge = GeminiJudge(os.environ.get("VERIFIER_MODEL") or vcfg.get("model", "gemini-3.5-flash-lite"),
                            os.environ.get("GEMINI_API_KEY", "").strip(),
                            max_output_tokens=int(vcfg.get("max_output_tokens", 2048)))
        deps = Deps(judge=judge, policy=policy,
                    max_chars_to_model=int(vcfg.get("max_chars_to_model", 15000)),
                    max_output_tokens=int(vcfg.get("max_output_tokens", 2048)),
                    model_timeout=float(vcfg.get("model_timeout_seconds", 60)),
                    cooldown=Cooldown())
    token = token if token is not None else os.environ.get("VERIFIER_TOKEN", "").strip()
    if trace is None:
        trace = TraceLog("verifier", os.environ.get("VERIFIER_TRACE_PATH") or ROOT / "traces" / "verifier.jsonl",
                         stdout=os.environ.get("VERIFIER_TRACE_STDOUT", "1" if os.environ.get("RENDER") else "0") == "1")
    app = create_worker_app(service="verifier", path="/verify", token=token, payload_model=VerifyPayload,
                            handler=lambda job: run_verify(job, deps), trace=trace,
                            max_concurrent=int(vcfg.get("max_concurrent", 2)), admission=deps.cooldown.retry_after)
    app.state.deps = deps

    @app.get("/trace")
    def get_trace(request: Request, run_id: str | None = Query(default=None, max_length=64)):
        """This service's own trace lines (for collecting Render's log after a run). Same token as /verify."""
        if not token_ok(request.headers.get("authorization"), token):
            return JSONResponse(error_body("unauthorized", "missing or wrong service token", False), status_code=401)
        return {"service": "verifier", "lines": trace.read(run_id)}

    return app


app = build_app() if os.environ.get("VERIFIER_NO_AUTOSTART") != "1" else None
