"""Researcher service (the A1B tracker behind an endpoint).

    uvicorn researcher.app:app --port 8001          (from the repository root)

Environment (repo-root .env is read locally; real environment variables win):
    RESEARCHER_TOKEN   bearer token the orchestrator must send (required)
    GEMINI_API_KEY     model key
    TAVILY_API_KEY     search key
    RESEARCHER_TRACE_PATH  optional, default traces/researcher.jsonl
The worker has no database credentials and never talks to the A1 API.
"""
import os

from shared.contract import ResearchPayload
from shared.tracelog import TraceLog
from shared.workerkit import Cooldown, create_worker_app
from researcher.core import Deps, run_research
from tracker.config import ROOT, load_config
from tracker.env import _load_dotenv
from tracker.fetch import fetch_article
from tracker.llm.base import Pacer
from tracker.llm.gemini import GeminiClient
from tracker.search import tavily_search
from tracker.urlguard import FetchPolicy


def default_deps() -> Deps:
    _load_dotenv()
    cfg = load_config()
    m = cfg["model"]
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    tavily_key = os.environ.get("TAVILY_API_KEY", "").strip()
    opts = {"temperature": float(m["temperature"]), "max_output_tokens": int(m["max_output_tokens"])}
    llm = GeminiClient(m["name"], key, **opts)
    fallback = GeminiClient(m["fallback_name"], key, **opts) if m.get("fallback_name") else None
    policy = FetchPolicy.from_config(cfg["fetch"])
    rcfg = cfg.get("researcher") or {}
    return Deps(
        base_cfg=cfg, llm=llm, fallback_llm=fallback,
        search_raw=lambda q, timeout: tavily_search(q, api_key=tavily_key, cfg=cfg["search"], timeout=timeout),
        fetch_raw=lambda url, on_hop=None, deadline=None: fetch_article(url, policy, on_hop=on_hop, deadline=deadline),
        max_searches=int(rcfg.get("max_searches", cfg["limits"]["max_searches"])),
        pacer=Pacer(float(m["requests_per_minute"])),
        cooldown=Cooldown(),
    )


def build_app(deps: Deps | None = None, *, token: str | None = None, trace: TraceLog | None = None):
    _load_dotenv()
    deps = deps or default_deps()
    token = token if token is not None else os.environ.get("RESEARCHER_TOKEN", "").strip()
    trace = trace or TraceLog("researcher", os.environ.get("RESEARCHER_TRACE_PATH") or ROOT / "traces" / "researcher.jsonl")
    app = create_worker_app(service="researcher", path="/research", token=token, payload_model=ResearchPayload,
                            handler=lambda job: run_research(job, deps), trace=trace, max_concurrent=1,
                            admission=deps.cooldown.retry_after)
    app.state.deps = deps
    return app


app = build_app() if os.environ.get("RESEARCHER_NO_AUTOSTART") != "1" else None
