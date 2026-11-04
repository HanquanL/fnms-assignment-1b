"""POST /research: the A1B tracker loop (tracker/agent.py) behind the A2 contract.

What changes from the A1B CLI run (`python -m tracker run`):
  - topic and K come from the payload; step / fetch / token limits come from the envelope's budget;
    the wall-clock limit is time_budget_ms, measured from when THIS request arrived;
  - every redirect hop counts as one fetch (hop_fetch=True);
  - no memory and no database: the worker never logs in to the A1 API, never reads seen URLs or
    known developments, never saves a run. The orchestrator owns all of that (PDF §Task);
  - the verified report is converted to the wire shape. Evidence quotes, fetched pages, the
    model's reasoning and the tool history stay here; only {id, title, summary, claims:[{claim_id,
    text, source_url}]} goes on the wire.
"""
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from shared.tracelog import TraceLog
from shared.workerkit import Cooldown, Job, Reply, response_body
from tracker.agent import AgentResult, run_agent
from tracker.budget import Budget, BudgetExceeded
from tracker.errors import RetryPolicy, ToolInputError, call_with_retry
from tracker.llm.base import Pacer

MODEL_COOLDOWN_S = 60


class TraceAdapter:
    """tracker/agent.py logs events as trace.log(kind, **fields). This maps the ones that are
    calls (model, search, fetch, finish, retries) onto the cross-service fields of PDF §13 and
    drops loop bookkeeping (run start/stop is the inbound line written by workerkit)."""

    def __init__(self, trace: TraceLog, run_id: str, request_id: str):
        self.trace, self.run_id, self.request_id = trace, run_id, request_id
        self._attempts: dict[str, int] = {}

    def log(self, kind: str, **f: Any) -> None:
        ids = {"run_id": self.run_id, "request_id": self.request_id}
        if kind == "retry":
            target = "model" if f.get("target") == "model" else str(f.get("target"))
            self._attempts[target] = int(f.get("attempt") or 1)
            self.trace.log(**ids, step=f.get("step"), direction="out",
                           endpoint_or_tool=target if target != "model" else "model:gemini",
                           status="retry", latency_ms=None, attempt=f.get("attempt"), tokens=0,
                           detail=f.get("error"), wait_s=f.get("wait_s"))
        elif kind == "model":
            tokens = f.get("tokens") or {}
            self.trace.log(**ids, step=f.get("step"), direction="out", endpoint_or_tool=f"model:{f.get('model')}",
                           status=str(f.get("status")), latency_ms=f.get("latency_ms"),
                           attempt=self._attempts.pop("model", 0) + 1,
                           tokens=tokens.get("total", 0) if isinstance(tokens, dict) else 0,
                           pacer_wait_ms=f.get("pacer_wait_ms"),
                           tool_calls=[c.get("tool") for c in (f.get("tool_calls") or [])] or None,
                           detail=f.get("detail") or f.get("error"))
        elif kind == "tool":
            tool = str(f.get("tool"))
            self.trace.log(**ids, step=f.get("step"), direction="out", endpoint_or_tool=tool,
                           status=str(f.get("status")), latency_ms=f.get("latency_ms", 0),
                           attempt=self._attempts.pop(tool, 0) + 1, tokens=0,
                           hops=f.get("hops"), http_status=f.get("http_status"), detail=f.get("detail"),
                           url=(f.get("args") or {}).get("url"), query=(f.get("args") or {}).get("query"),
                           results=f.get("results"), credits=f.get("credits"), developments=f.get("developments"))


@dataclass
class Deps:
    base_cfg: Mapping                      # config.yaml (frozen)
    llm: Any                               # tracker LLMClient
    fallback_llm: Any | None
    search_raw: Callable[..., Any]         # (query, timeout) -> SearchResponse; ONE attempt
    fetch_raw: Callable[..., Any]          # (url, on_hop=, deadline=) -> FetchResult
    max_searches: int = 8
    pacer: Pacer | None = None
    sleep: Callable[[float], None] = time.sleep
    cooldown: Cooldown = field(default_factory=Cooldown)


def _cfg_for(job: Job, deps: Deps) -> dict:
    base = deps.base_cfg
    b = job.envelope.budget
    cfg = {k: v for k, v in base.items()}
    cfg["topic"] = job.payload.topic
    cfg["k"] = job.payload.k
    cfg["limits"] = {"max_steps": b.max_steps, "max_searches": deps.max_searches, "max_fetches": b.max_fetches,
                     "max_total_tokens": b.max_tokens, "max_wall_seconds": job.envelope.time_budget_ms / 1000.0}
    return cfg


def to_wire(report: dict, k: int) -> dict:
    developments = []
    for d in sorted(report.get("developments") or [], key=lambda d: d["rank"])[:k]:
        claims = [{"claim_id": f"{d['key']}#{j}", "text": c["text"][:1000], "source_url": c["url"]}
                  for j, c in enumerate(d.get("claims") or [], start=1)][:10]
        if claims:
            developments.append({"id": d["key"], "title": d["title"][:300], "summary": d["summary"][:4000],
                                 "claims": claims})
    return {"developments": developments}


def _error_for(result: AgentResult) -> tuple[str, str]:
    reason = result.stop_reason or ""
    if reason.startswith("budget:"):
        timed = "max_wall_seconds" in reason or "time budget" in reason
        return ("time_budget_exhausted" if timed else "budget_exhausted"), reason
    if reason.startswith("RetriesExhausted"):
        return "model_unavailable", reason
    if reason.startswith("TerminalError"):
        return "terminal_error", reason
    if "finish rejected" in reason:
        return "report_salvaged", reason
    if "never produced" in reason:
        return "no_verified_report", reason
    return "incomplete", reason or "the run did not complete"


def run_research(job: Job, deps: Deps) -> Reply:
    env = job.envelope
    cfg = _cfg_for(job, deps)
    deadline = job.deadline
    budget = Budget(max_steps=env.budget.max_steps, max_searches=deps.max_searches,
                    max_fetches=env.budget.max_fetches, max_total_tokens=env.budget.max_tokens,
                    max_wall_seconds=env.time_budget_ms / 1000.0, started=job.received_at)
    retry = RetryPolicy.from_config(cfg["retry"])
    trace = TraceAdapter(job.trace, env.run_id, env.request_id)

    def bounded_sleep(seconds: float) -> None:
        if time.monotonic() + seconds > deadline - 2:
            raise BudgetExceeded("time budget would run out while waiting (backoff)")
        deps.sleep(seconds)

    def search(query: str, on_retry=None):
        left = deadline - time.monotonic()
        if left < 5:
            raise ToolInputError("not enough time left to search")
        return call_with_retry(lambda: deps.search_raw(query, timeout=min(20.0, left - 1)), retry,
                               what="search_web", on_retry=on_retry, sleep=bounded_sleep)

    def fetch(url: str, on_hop=None, deadline=None):
        return deps.fetch_raw(url, on_hop=on_hop, deadline=deadline)

    result = run_agent(cfg, deps.llm, search, fetch, trace, memory=None, pacer=deps.pacer, retry=retry,
                       fallback_llm=deps.fallback_llm, sleep=deps.sleep, budget=budget, deadline=deadline,
                       hop_fetch=True)

    st = result.stats
    usage = {"steps": int(st["steps"]), "fetches": int(st["fetches"]), "tokens": int(st["tokens_total"]),
             "searches": int(st["searches"])}
    wire = to_wire(result.report, job.payload.k)
    if result.status == "complete":
        return Reply(200, response_body(job, "complete", wire, usage))

    code, message = _error_for(result)
    if code == "model_unavailable":
        deps.cooldown.start(MODEL_COOLDOWN_S)   # new requests get 503 + Retry-After for a while
    if code == "terminal_error" and not wire["developments"]:
        return Reply(500, response_body(job, "failed", None, usage,
                                        {"code": code, "message": message[:500], "retryable": False}))
    return Reply(200, response_body(job, "partial", wire, usage,
                                    {"code": code, "message": message[:500], "retryable": False}))
