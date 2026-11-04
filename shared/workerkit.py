"""HTTP plumbing both workers share: service-token auth, strict request validation, the
idempotency registry, admission control, and the inbound trace line.

The order of checks on every POST (PDF §3-§7):

    1. Authorization: Bearer <token>        -> 401   (before anything about the body is revealed)
    2. body size, JSON, envelope, payload   -> 400
    3. request_id seen before?
         same id, different body            -> 409
         same id, same body, still running  -> 503 + Retry-After  (no second execution)
         same id, same body, finished       -> the stored status + body, no new work
    4. capacity / model cooldown             -> 503 + Retry-After
    5. run the worker's handler in a thread; its clock started at step 0, when the request arrived

Only final answers (200 and 500) are stored for replay. A 503 is not stored: the retry is meant
to run the work. The registry is in memory, one per process: a restart forgets it
(ORCHESTRATION.md Q3).
"""
import hmac
import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from shared.contract import MAX_BODY_BYTES, Envelope, body_hash, error_body
from shared.tracelog import TraceLog

IN_FLIGHT_RETRY_AFTER_S = 5


# ------------------------------------------------------------ idempotency ----

@dataclass
class Entry:
    digest: str
    state: str = "in_flight"          # in_flight | done
    status_code: int | None = None
    body: dict | None = None
    created: float = field(default_factory=time.monotonic)


class Registry:
    """request_id -> (hash of the canonical body, state, stored answer). Thread-safe; the
    check-and-register in begin() is one atomic step, so two copies of a request arriving at the
    same moment can't both start."""

    def __init__(self, max_entries: int = 1000):
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, Entry] = OrderedDict()
        self.max_entries = max_entries

    def begin(self, request_id: str, digest: str) -> tuple[str, Entry]:
        with self._lock:
            entry = self._entries.get(request_id)
            if entry is None:
                entry = Entry(digest)
                self._entries[request_id] = entry
                return "new", entry
            if entry.digest != digest:
                return "conflict", entry
            if entry.state == "in_flight":
                return "in_flight", entry
            return "replay", entry

    def finish(self, request_id: str, status_code: int, body: dict) -> None:
        with self._lock:
            entry = self._entries.get(request_id)
            if entry is None:
                return
            entry.state, entry.status_code, entry.body = "done", status_code, body
            self._evict()

    def abandon(self, request_id: str) -> None:
        """Forget an in-flight entry that produced no final answer (a 503), so a retry can run."""
        with self._lock:
            entry = self._entries.get(request_id)
            if entry is not None and entry.state == "in_flight":
                del self._entries[request_id]

    def _evict(self) -> None:
        while len(self._entries) > self.max_entries:
            oldest_done = next((rid for rid, e in self._entries.items() if e.state == "done"), None)
            if oldest_done is None:
                return
            del self._entries[oldest_done]

    def __len__(self) -> int:
        return len(self._entries)


# ------------------------------------------------------------------- jobs ----

@dataclass
class Job:
    """What a handler gets: the validated request, plus its own deadline (monotonic seconds)."""
    envelope: Envelope
    payload: BaseModel
    received_at: float
    trace: TraceLog

    @property
    def deadline(self) -> float:
        return self.received_at + self.envelope.time_budget_ms / 1000.0

    def remaining_s(self) -> float:
        return self.deadline - time.monotonic()


@dataclass
class Reply:
    status_code: int
    body: dict
    retry_after: int | None = None


Handler = Callable[[Job], Reply]
Admission = Callable[[], int | None]      # returns Retry-After seconds to refuse new work, or None


class Cooldown:
    """Per-process memory of "the model just rate-limited us". While it lasts, new requests get
    503 + Retry-After BEFORE any work starts (PDF §3: 503 for a model per-minute rate limit).
    A request that already started never turns into a 503 afterwards: it answers 200 partial
    with the usage it really spent, so the orchestrator's accounting stays exact."""

    def __init__(self):
        self._until = 0.0
        self._lock = threading.Lock()

    def start(self, seconds: float) -> None:
        with self._lock:
            self._until = max(self._until, time.monotonic() + max(1.0, seconds))

    def retry_after(self) -> int | None:
        with self._lock:
            left = self._until - time.monotonic()
        return int(left) + 1 if left > 0 else None


def token_ok(header: str | None, expected: str) -> bool:
    if not header or not expected:
        return False
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return False
    return hmac.compare_digest(value.strip().encode(), expected.encode())


def _schema_message(e: ValidationError) -> str:
    """Where and why, never the submitted value (it could be large or sensitive)."""
    parts = [f"{'.'.join(str(x) for x in err['loc']) or 'body'}: {err['msg']}" for err in e.errors()[:5]]
    return "; ".join(parts)[:500]


def create_worker_app(*, service: str, path: str, token: str, payload_model: type[BaseModel], handler: Handler,
                      trace: TraceLog, registry: Registry | None = None, max_concurrent: int = 1,
                      admission: Admission | None = None) -> FastAPI:
    if not token:
        raise RuntimeError(f"{service}: service token is not set; refusing to start an unauthenticated worker")
    registry = registry or Registry()
    slots = threading.BoundedSemaphore(max_concurrent)
    app = FastAPI(title=f"FNMS {service}", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.registry = registry
    app.state.trace = trace
    endpoint_name = f"POST {path}"

    def answer(reply: Reply, received: float, *, run_id=None, request_id=None, status=None, **extra) -> JSONResponse:
        latency = int((time.monotonic() - received) * 1000)
        usage = reply.body.get("usage") if isinstance(reply.body.get("usage"), dict) else {}
        trace.log(run_id=run_id, request_id=request_id, step=None, direction="in", endpoint_or_tool=endpoint_name,
                  status=status or str(reply.body.get("status", reply.status_code)), latency_ms=latency,
                  attempt=None, tokens=usage.get("tokens", 0), http_status=reply.status_code, **extra)
        headers = {"Retry-After": str(reply.retry_after)} if reply.retry_after is not None else None
        return JSONResponse(reply.body, status_code=reply.status_code, headers=headers)

    @app.get("/healthz")
    def healthz():
        trace.log(run_id=None, request_id=None, step=None, direction="in", endpoint_or_tool="GET /healthz",
                  status="ok", latency_ms=0, attempt=None, tokens=0)
        return {"status": "ok"}

    @app.post(path)
    async def work(request: Request):
        received = time.monotonic()   # the worker's clock starts here (PDF §6)

        # 1. auth first: an unauthenticated caller learns nothing about the schema
        if not token_ok(request.headers.get("authorization"), token):
            return answer(Reply(401, error_body("unauthorized", "missing or wrong service token", False)),
                          received, status="unauthorized")

        # 2. schema
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            return answer(Reply(400, error_body("bad_request", f"body larger than {MAX_BODY_BYTES} bytes", False)),
                          received, status="bad_request")
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return answer(Reply(400, error_body("bad_request", "body is not valid JSON", False)),
                          received, status="bad_request")
        if not isinstance(data, dict):
            return answer(Reply(400, error_body("bad_request", "body must be a JSON object", False)),
                          received, status="bad_request")
        try:
            envelope = Envelope.model_validate(data)
            payload = payload_model.model_validate(envelope.payload)
        except ValidationError as e:
            rid = data.get("run_id") if isinstance(data.get("run_id"), str) else None
            qid = data.get("request_id") if isinstance(data.get("request_id"), str) else None
            return answer(Reply(400, error_body("bad_request", _schema_message(e), False)), received,
                          run_id=rid, request_id=qid, status="bad_request")
        ids = {"run_id": envelope.run_id, "request_id": envelope.request_id}

        # 3. idempotency
        try:
            digest = body_hash(data)
        except ValueError:  # NaN/Infinity can't be hashed canonically
            return answer(Reply(400, error_body("bad_request", "body contains non-finite numbers", False)),
                          received, status="bad_request", **ids)
        kind, entry = registry.begin(envelope.request_id, digest)
        if kind == "conflict":
            return answer(Reply(409, error_body("request_id_conflict",
                                                "this request_id was already used with a different body", False,
                                                **ids)), received, status="conflict", **ids)
        if kind == "in_flight":
            return answer(Reply(503, error_body("in_progress", "this request_id is still being processed", True,
                                                **ids), retry_after=IN_FLIGHT_RETRY_AFTER_S),
                          received, status="in_flight_duplicate", **ids)
        if kind == "replay":
            return answer(Reply(entry.status_code, entry.body), received, status="replay", **ids)

        # 4. admission: capacity and model cooldown
        wait = admission() if admission else None
        if wait is not None:
            registry.abandon(envelope.request_id)
            return answer(Reply(503, error_body("rate_limited", "the model is rate-limited; try again later", True,
                                                **ids), retry_after=wait), received, status="rate_limited", **ids)
        if not slots.acquire(blocking=False):
            registry.abandon(envelope.request_id)
            return answer(Reply(503, error_body("overloaded", "worker is busy with another request", True, **ids),
                                retry_after=10), received, status="overloaded", **ids)

        # 5. the work itself
        job = Job(envelope=envelope, payload=payload, received_at=received, trace=trace)
        try:
            reply = await run_in_threadpool(handler, job)
        except Exception as e:  # a bug must still produce a well-formed, final answer
            reply = Reply(500, {**ids, "status": "failed", "result": None,
                                "usage": {"steps": 0, "fetches": 0, "tokens": 0, "searches": 0},
                                "error": {"code": "internal_error", "message": f"{type(e).__name__}"[:500],
                                          "retryable": False}})
        finally:
            slots.release()

        if reply.status_code == 503:
            registry.abandon(envelope.request_id)
        else:
            registry.finish(envelope.request_id, reply.status_code, reply.body)
        return answer(reply, received, **ids)

    return app


def response_body(job: Job, status: str, result: dict | None, usage: dict, error: dict | None = None) -> dict:
    return {"request_id": job.envelope.request_id, "run_id": job.envelope.run_id, "status": status,
            "result": result, "usage": usage, "error": error}
