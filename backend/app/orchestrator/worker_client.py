"""Calling a worker over HTTP: timeouts, bounded retries, and the caller-side trace (PDF §6, §7, §13).

Retry policy (only for failures where trying again can help):
    retried    connection refused/reset, DNS failure, connect or read timeout, HTTP 503
               (and 502/504, which a proxy such as Render's sends while an instance is starting)
    never      200 (complete or partial), 400, 401, 409, 500, any other status
Backoff is exponential with jitter, capped, and never shorter than Retry-After. A Retry-After
longer than max_retry_after_seconds ends the call (waiting that long isn't a "blip").

A retry resends EXACTLY the same bytes: the envelope is serialized once, before the first attempt,
and the same request_id goes out every time (a changed body would be a different request -> 409).

Latency is measured here, on the orchestrator's monotonic clock, for every attempt.
"""
import json
import random
import time
from dataclasses import dataclass
from typing import Callable

import httpx

from app.orchestrator.settings import OrchestratorSettings, WorkerSettings
from shared.tracelog import TraceLog

TRANSIENT_STATUSES = (502, 503, 504)


@dataclass
class CallResult:
    worker: str
    request_id: str
    answered: bool              # a final HTTP answer arrived (any status that isn't retried)
    http_status: int | None
    body: dict | None           # parsed JSON, NOT yet validated
    attempts: int
    error: str | None           # why the call failed, for the report / run history
    latency_ms: int             # whole call incl. retries and backoff, caller-side


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        v = resp.headers.get("retry-after")
        return max(0.0, float(v)) if v is not None else None
    except ValueError:
        return None


class WorkerClient:
    def __init__(self, settings: OrchestratorSettings, trace: TraceLog, *,
                 transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep):
        self.s, self.trace, self.transport, self.sleep = settings, trace, transport, sleep

    def _client(self, w: WorkerSettings, read_timeout: float) -> httpx.Client:
        c = self.s.connect_timeout_s
        return httpx.Client(timeout=httpx.Timeout(connect=c, read=read_timeout, write=c, pool=c),
                            transport=self.transport, follow_redirects=False)

    def call(self, w: WorkerSettings, envelope: dict, *, step: int) -> CallResult:
        rid, run_id = envelope["request_id"], envelope["run_id"]
        body_bytes = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False).encode("utf-8")  # once
        headers = {"Authorization": f"Bearer {w.token}", "Content-Type": "application/json"}
        url = f"{w.url}{w.path}"
        endpoint = f"POST {w.path}"
        read_timeout = self.s.response_timeout(w)
        policy = self.s.retry
        started = time.monotonic()
        last_error = "not attempted"

        with self._client(w, read_timeout) as client:
            for attempt in range(1, policy.max_attempts + 1):
                t0 = time.monotonic()
                wait: float | None = None
                try:
                    resp = client.post(url, content=body_bytes, headers=headers)
                except httpx.TimeoutException as e:
                    last_error = f"{type(e).__name__} after {time.monotonic() - t0:.1f}s"
                    status = "timeout"
                except httpx.TransportError as e:
                    last_error = f"{type(e).__name__}: connection failed"
                    status = "connect_error"
                else:
                    latency = int((time.monotonic() - t0) * 1000)
                    try:
                        data = resp.json()
                        data = data if isinstance(data, dict) else None
                    except ValueError:
                        data = None
                    usage = (data or {}).get("usage") if isinstance((data or {}).get("usage"), dict) else {}
                    tokens = usage.get("tokens") if isinstance(usage.get("tokens"), int) else 0
                    if resp.status_code not in TRANSIENT_STATUSES:
                        st = (data or {}).get("status") if resp.status_code == 200 else f"http_{resp.status_code}"
                        self.trace.log(run_id=run_id, request_id=rid, step=step, direction="out",
                                       endpoint_or_tool=endpoint, status=str(st or "invalid_body"),
                                       latency_ms=latency, attempt=attempt, tokens=tokens,
                                       http_status=resp.status_code, worker=w.name)
                        err = None if resp.status_code == 200 else (
                            f"HTTP {resp.status_code}: "
                            + str(((data or {}).get("error") or {}).get("message", "no message"))[:200])
                        return CallResult(w.name, rid, True, resp.status_code, data, attempt, err,
                                          int((time.monotonic() - started) * 1000))
                    status = f"http_{resp.status_code}"
                    last_error = f"HTTP {resp.status_code}" + (
                        f": {((data or {}).get('error') or {}).get('code')}" if data else "")
                    wait = _retry_after(resp)
                    if wait is not None and wait > policy.max_retry_after:
                        self.trace.log(run_id=run_id, request_id=rid, step=step, direction="out",
                                       endpoint_or_tool=endpoint, status="failed", latency_ms=latency,
                                       attempt=attempt, tokens=0, http_status=resp.status_code, worker=w.name,
                                       detail=f"Retry-After {wait:.0f}s exceeds {policy.max_retry_after:.0f}s")
                        return CallResult(w.name, rid, False, resp.status_code, None, attempt,
                                          f"{last_error}; asked to wait {wait:.0f}s, giving up",
                                          int((time.monotonic() - started) * 1000))

                latency = int((time.monotonic() - t0) * 1000)
                delay = min(policy.max_delay, policy.base_delay * 2 ** (attempt - 1)) + random.uniform(0, policy.base_delay)
                if wait is not None:
                    delay = max(delay, wait)       # honour Retry-After
                out_of_attempts = attempt == policy.max_attempts
                out_of_time = (time.monotonic() - started) + delay > self.s.max_seconds_per_worker
                final = out_of_attempts or out_of_time
                self.trace.log(run_id=run_id, request_id=rid, step=step, direction="out", endpoint_or_tool=endpoint,
                               status="failed" if final else "retry", latency_ms=latency, attempt=attempt, tokens=0,
                               worker=w.name, detail=last_error, error_kind=status,
                               next_delay_s=None if final else round(delay, 1))
                if final:
                    why = "attempts" if out_of_attempts else "time"
                    return CallResult(w.name, rid, False, None, None, attempt,
                                      f"{w.name} did not answer ({last_error}); gave up after {attempt} attempt(s), "
                                      f"out of {why}", int((time.monotonic() - started) * 1000))
                self.sleep(delay)
        raise AssertionError("unreachable")

    def ping(self, w: WorkerSettings, *, run_id: str) -> tuple[bool, int]:
        """GET /healthz (no auth). Used to wake a sleeping Render instance early; never retried."""
        t0 = time.monotonic()
        try:
            with self._client(w, w.cold_start_s + self.s.slack_s) as client:
                r = client.get(f"{w.url}/healthz")
            ok = r.status_code == 200 and r.json() == {"status": "ok"}
            status = "ok" if ok else f"http_{r.status_code}"
        except (httpx.HTTPError, ValueError) as e:
            ok, status = False, type(e).__name__
        latency = int((time.monotonic() - t0) * 1000)
        self.trace.log(run_id=run_id, request_id=None, step=0, direction="out", endpoint_or_tool="GET /healthz",
                       status=status, latency_ms=latency, attempt=1, tokens=0, worker=w.name, purpose="prewarm")
        return ok, latency
