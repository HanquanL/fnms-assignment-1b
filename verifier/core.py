"""POST /verify: check each claim against the page it cites, fetched by the verifier itself.

Input is ONLY {claim_id, text, source_url} per claim (PDF §3). The verifier never sees the
researcher's summaries, reasoning, evidence quotes or fetched pages.

For each distinct source URL, in the order the claims arrived:
    1. stop if the time budget is nearly gone          -> partial, time_budget_exhausted
    2. fetch it through the guardrails; every hop takes one fetch from the budget, reserved
       BEFORE the request is sent                       -> partial, budget_exhausted if refused
       rejected by a guardrail / failed                 -> every claim citing it: unreachable
    3. reserve tokens + one step, ask the model about all claims citing that URL
    4. code checks each answer: a "supported" whose excerpt isn't in the page (after the PDF's
       normalization) becomes "unsupported". Code never upgrades a verdict.
Claims not reached are left out of `verdicts`; the response is then partial and `error` says why.
"""
import socket
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable

from shared.workerkit import Cooldown, Job, Reply, response_body
from verifier.evidence import check_evidence
from verifier.fetchguard import DeadlineReached, FetchPolicy, HopRefused, fetch_article
from verifier.judge import ModelTerminal, ModelTransient, estimate_tokens

MIN_SECONDS_TO_START = 2.0      # don't start a fetch or model call with less time than this left


@dataclass
class Deps:
    judge: object                       # .judge(claims, page_text, max_chars=, timeout=) and .model
    policy: FetchPolicy
    max_chars_to_model: int = 15_000
    max_output_tokens: int = 2048
    model_timeout: float = 60.0
    model_attempts: int = 3
    resolver: Callable | None = None
    transport: object | None = None
    sleep: Callable[[float], None] = time.sleep
    cooldown: Cooldown = field(default_factory=Cooldown)


class _Stop(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable


def run_verify(job: Job, deps: Deps) -> Reply:
    env, claims = job.envelope, job.payload.claims
    budget = env.budget
    ids = {"run_id": env.run_id, "request_id": env.request_id}
    usage = {"steps": 0, "fetches": 0, "tokens": 0, "searches": 0}
    verdicts: dict[str, dict] = {}
    stop: _Stop | None = None
    soft_errors: list[str] = []
    terminal = False

    groups: OrderedDict[str, list] = OrderedDict()
    for c in claims:
        groups.setdefault(c.source_url, []).append(c)

    def on_hop(url: str) -> bool:
        if usage["fetches"] >= budget.max_fetches:
            return False
        usage["fetches"] += 1
        return True

    def call_model(group: list, text: str):
        payload = [{"claim_id": c.claim_id, "text": c.text} for c in group]
        for attempt in range(1, deps.model_attempts + 1):
            left = job.remaining_s()
            if left < MIN_SECONDS_TO_START:
                raise _Stop("time_budget_exhausted", "time budget ran out before a model call")
            if usage["steps"] >= budget.max_steps:
                raise _Stop("budget_exhausted", f"step budget used up ({budget.max_steps} model calls)")
            need = estimate_tokens(payload, text, deps.max_chars_to_model, deps.max_output_tokens)
            if usage["tokens"] + need > budget.max_tokens:
                raise _Stop("budget_exhausted", f"token budget would be exceeded ({usage['tokens']:,} used, "
                                                f"next call may need {need:,} of {budget.max_tokens:,})")
            t0 = time.monotonic()
            try:
                out = deps.judge.judge(payload, text, max_chars=deps.max_chars_to_model,
                                       timeout=max(1.0, min(deps.model_timeout, left - 1.0)))
            except ModelTransient as e:
                job.trace.log(**ids, step=usage["steps"] + 1, direction="out",
                              endpoint_or_tool=f"model:{deps.judge.model}", status="retry",
                              latency_ms=int((time.monotonic() - t0) * 1000), attempt=attempt, tokens=0,
                              detail=str(e))
                wait = e.retry_after if e.retry_after is not None else min(20.0, 2.0 ** attempt)
                if "429" in str(e):
                    deps.cooldown.start(wait)
                if attempt == deps.model_attempts:
                    raise _Stop("model_unavailable", f"model still unavailable after {attempt} attempts: {e}")
                if job.remaining_s() - wait < MIN_SECONDS_TO_START:
                    raise _Stop("time_budget_exhausted", "time budget ran out while waiting for the model")
                deps.sleep(wait)
                continue
            except ModelTerminal as e:
                job.trace.log(**ids, step=usage["steps"] + 1, direction="out",
                              endpoint_or_tool=f"model:{deps.judge.model}", status="failed",
                              latency_ms=int((time.monotonic() - t0) * 1000), attempt=attempt, tokens=0,
                              detail=str(e))
                raise
            usage["steps"] += 1
            usage["tokens"] += out.tokens
            job.trace.log(**ids, step=usage["steps"], direction="out", endpoint_or_tool=f"model:{deps.judge.model}",
                          status="ok" if out.parsed else "bad_output",
                          latency_ms=int((time.monotonic() - t0) * 1000), attempt=attempt, tokens=out.tokens,
                          claims=len(group))
            if out.parsed:
                return out.verdicts
            # unparseable answer: one more try if the loop allows it
        return None

    try:
        for url, group in groups.items():
            if job.remaining_s() < MIN_SECONDS_TO_START:
                raise _Stop("time_budget_exhausted", "time budget ran out")

            t0 = time.monotonic()
            before = usage["fetches"]
            try:
                fr = fetch_article(url, deps.policy, on_hop=on_hop, deadline=job.deadline,
                                   resolver=deps.resolver or socket.getaddrinfo,
                                   transport=deps.transport)
            except HopRefused:
                raise _Stop("budget_exhausted", f"fetch budget used up ({budget.max_fetches} requests)")
            except DeadlineReached:
                raise _Stop("time_budget_exhausted", "time budget ran out during a fetch")
            finally:
                hops = usage["fetches"] - before
            job.trace.log(**ids, step=usage["steps"], direction="out", endpoint_or_tool="fetch_article",
                          status=fr.status,
                          latency_ms=int((time.monotonic() - t0) * 1000), attempt=1, tokens=0,
                          hops=hops, http_status=fr.http_status, detail=fr.reason, url=url[:300])
            if fr.status != "fetched":
                for c in group:
                    verdicts[c.claim_id] = {"claim_id": c.claim_id, "verdict": "unreachable", "evidence": None}
                continue

            raw = call_model(group, fr.text)
            if raw is None:
                soft_errors.append(f"model output for {len(group)} claim(s) could not be parsed")
                continue
            proposed: dict[str, dict] = {}
            for item in raw:
                if isinstance(item, dict) and isinstance(item.get("claim_id"), str):
                    proposed.setdefault(item["claim_id"], item)   # first answer per id; unknown ids ignored
            downgraded = 0
            for c in group:
                item = proposed.get(c.claim_id) or {}
                if item.get("verdict") == "supported":
                    ok, _why = check_evidence(item.get("evidence"), fr.text)
                    if ok:
                        verdicts[c.claim_id] = {"claim_id": c.claim_id, "verdict": "supported",
                                                "evidence": item["evidence"]}
                        continue
                    downgraded += 1
                verdicts[c.claim_id] = {"claim_id": c.claim_id, "verdict": "unsupported", "evidence": None}
            if downgraded:
                job.trace.log(**ids, step=usage["steps"], direction="out", endpoint_or_tool="evidence_check",
                              status="downgraded", latency_ms=0, attempt=1, tokens=0, claims=downgraded)
    except _Stop as e:
        stop = e
    except ModelTerminal as e:
        stop, terminal = _Stop("model_error", str(e)), True

    ordered = [verdicts[c.claim_id] for c in claims if c.claim_id in verdicts]
    n, total = len(ordered), len(claims)
    if stop is None and not soft_errors and n == total:
        return Reply(200, response_body(job, "complete", {"verdicts": ordered}, usage))
    if terminal and n == 0:
        return Reply(500, response_body(job, "failed", None, usage,
                                        {"code": stop.code, "message": stop.message[:500], "retryable": False}))
    if stop is not None:
        err = {"code": stop.code, "message": f"stopped after {n} of {total} claims: {stop.message}"[:500],
               "retryable": False}
    else:
        err = {"code": "model_output_invalid",
               "message": f"verdicts for {n} of {total} claims; {'; '.join(soft_errors)}"[:500], "retryable": False}
    return Reply(200, response_body(job, "partial", {"verdicts": ordered}, usage, err))
