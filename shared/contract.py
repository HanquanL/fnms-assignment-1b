"""The wire contract between the orchestrator and the two workers (PDF §3).

This is the one file every service shares (PDF §2: "Sharing a schema file is fine"). It holds
only data shapes and pure functions; no service logic and no state.

Request (orchestrator -> worker), for every POST:
    {"request_id", "run_id", "time_budget_ms", "budget": {max_steps, max_fetches, max_tokens}, "payload"}

Response (worker -> orchestrator), same shape for both workers (DECISIONS.md C5/D14):
    {"request_id", "run_id", "status": complete|partial|failed,
     "result": <the PDF's "result out" object> | null,
     "usage": {"steps", "fetches", "tokens", "searches"},
     "error": {"code", "message", "retryable"} | null}

Validation is strict: unknown fields are refused (extra="forbid") and numbers must be real JSON
integers, not strings that look like numbers. A worker answers 400 to anything that fails here.
"""
import hashlib
import json
import re
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator, model_validator

MAX_BODY_BYTES = 1_000_000          # a request body larger than this is refused (400) before parsing
MAX_EVIDENCE_CHARS = 300            # PDF §9
CLAIM_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:#/-]{0,199}$"
DEV_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:#/-]{0,199}$"

Verdict = Literal["supported", "unsupported", "unreachable"]
WorkerStatus = Literal["complete", "partial", "failed"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _uuid(v: str) -> str:
    try:
        return str(uuid.UUID(v))
    except (ValueError, AttributeError, TypeError):
        raise ValueError("must be a UUID string")


# ---------------------------------------------------------------- request ----

class WireBudget(_Strict):
    """A worker's share of the run budget. One step = one model call; one fetch = one HTTP request
    to a web page, every redirect hop included."""
    max_steps: StrictInt = Field(ge=0, le=1_000)
    max_fetches: StrictInt = Field(ge=0, le=1_000)
    max_tokens: StrictInt = Field(ge=0, le=10_000_000)


class Envelope(_Strict):
    request_id: StrictStr
    run_id: StrictStr
    time_budget_ms: StrictInt = Field(ge=1, le=900_000)
    budget: WireBudget
    payload: dict[str, Any]

    _ids = field_validator("request_id", "run_id")(_uuid)


class ResearchPayload(_Strict):
    topic: StrictStr = Field(min_length=1, max_length=200)
    k: StrictInt = Field(ge=1, le=10)


class ClaimIn(_Strict):
    claim_id: StrictStr = Field(pattern=CLAIM_ID_PATTERN)
    text: StrictStr = Field(min_length=1, max_length=1000)
    # Any string: an unsafe or non-http URL is not a schema error, it is a claim whose source is
    # unreachable (the guardrails decide, and say so in the verdict).
    source_url: StrictStr = Field(min_length=1, max_length=2048)


class VerifyPayload(_Strict):
    claims: list[ClaimIn] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def unique_ids(self):
        ids = [c.claim_id for c in self.claims]
        if len(ids) != len(set(ids)):
            raise ValueError("claim_id values must be unique")
        return self


# --------------------------------------------------------------- response ----

class Usage(_Strict):
    steps: StrictInt = Field(ge=0)
    fetches: StrictInt = Field(ge=0)
    tokens: StrictInt = Field(ge=0)
    searches: StrictInt = Field(default=0, ge=0)


class ErrorInfo(_Strict):
    code: StrictStr = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    message: StrictStr = Field(max_length=500)
    retryable: StrictBool


class ResearchClaim(_Strict):
    claim_id: StrictStr = Field(pattern=CLAIM_ID_PATTERN)
    text: StrictStr = Field(min_length=1, max_length=1000)
    source_url: StrictStr = Field(min_length=1, max_length=2048)

    @field_validator("source_url")
    @classmethod
    def http_url(cls, v: str) -> str:
        if not re.match(r"^https?://[^\s/]+", v, re.IGNORECASE):
            raise ValueError("source_url must be an absolute http(s) URL")
        return v


class Development(_Strict):
    id: StrictStr = Field(pattern=DEV_ID_PATTERN)
    title: StrictStr = Field(min_length=1, max_length=300)
    summary: StrictStr = Field(max_length=4000)
    claims: list[ResearchClaim] = Field(min_length=1, max_length=10)


class ResearchResult(_Strict):
    developments: list[Development] = Field(max_length=10)


class VerdictOut(_Strict):
    claim_id: StrictStr = Field(pattern=CLAIM_ID_PATTERN)
    verdict: Verdict
    evidence: StrictStr | None

    @model_validator(mode="after")
    def evidence_only_when_supported(self):
        if self.verdict == "supported":
            if not self.evidence or len(self.evidence) > MAX_EVIDENCE_CHARS:
                raise ValueError(f"a supported verdict needs evidence of 1-{MAX_EVIDENCE_CHARS} characters")
        elif self.evidence is not None:
            raise ValueError("evidence must be null unless the verdict is supported")
        return self


class VerifyResult(_Strict):
    verdicts: list[VerdictOut] = Field(max_length=50)


class WorkerResponse(_Strict):
    request_id: StrictStr
    run_id: StrictStr
    status: WorkerStatus
    result: dict[str, Any] | None
    usage: Usage
    error: ErrorInfo | None

    _ids = field_validator("request_id", "run_id")(_uuid)

    @model_validator(mode="after")
    def error_matches_status(self):
        if self.status == "complete" and self.error is not None:
            raise ValueError("a complete response has no error")
        if self.status != "complete" and self.error is None:
            raise ValueError("a partial or failed response must say why in error")
        if self.status == "failed" and self.result is not None:
            raise ValueError("a failed response has no result")
        if self.status != "failed" and self.result is None:
            raise ValueError("a complete or partial response must include result")
        return self


# ------------------------------------------------------------ comparisons ----

def canonical_json(obj: Any) -> str:
    """The same JSON once parsed -> the same string: keys sorted, no whitespace (PDF §7)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def body_hash(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def error_body(code: str, message: str, retryable: bool, **extra: Any) -> dict:
    """Body for non-200 answers (400/401/409/500/503)."""
    return {"status": "failed", "error": {"code": code, "message": message[:500], "retryable": retryable}, **extra}
