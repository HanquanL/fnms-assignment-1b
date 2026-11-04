"""Validate what the workers send back (PDF §5): schema AND meaning.

A response that fails here counts as that worker failing: its contents are discarded in full,
never partly used, and never reach the report (PDF §5, §11). Checks on every response:
  - the shared contract's schema (strict: unknown fields, wrong types, bad status/error pairs)
  - request_id and run_id are the ones we sent
  - reported usage is within the share we gave (a worker can't claim to have spent more than
    it was allowed, and can't push the run past its total)
/research additionally: at most K developments, unique development ids, unique claim ids,
    http(s) source URLs.
/verify additionally: every claim_id is one we sent in THIS request, no duplicates, and a
    "complete" answer covers every claim we sent (a "partial" one may leave some out: those
    become unverified).
"""
from dataclasses import dataclass

from pydantic import ValidationError

from app.orchestrator.settings import Share
from shared.contract import Development, ResearchResult, VerdictOut, VerifyResult, WorkerResponse


class InvalidResponse(Exception):
    pass


@dataclass
class Checked:
    response: WorkerResponse


def _short(e: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, x['loc'])) or 'body'}: {x['msg']}" for x in e.errors()[:3])[:300]


def check_envelope(body: dict | None, sent: dict, share: Share) -> WorkerResponse:
    if not isinstance(body, dict):
        raise InvalidResponse("response body is not a JSON object")
    try:
        r = WorkerResponse.model_validate(body)
    except ValidationError as e:
        raise InvalidResponse(f"response does not match the contract: {_short(e)}")
    if r.request_id != sent["request_id"] or r.run_id != sent["run_id"]:
        raise InvalidResponse("response is for a different request_id or run_id")
    u = r.usage
    if u.steps > share.max_steps or u.fetches > share.max_fetches or u.tokens > share.max_tokens:
        raise InvalidResponse(f"reported usage {u.model_dump()} exceeds the share given ({share.wire()})")
    return r


def check_research(r: WorkerResponse, k: int) -> list[Development]:
    if r.result is None:
        return []
    try:
        res = ResearchResult.model_validate(r.result)
    except ValidationError as e:
        raise InvalidResponse(f"research result does not match the contract: {_short(e)}")
    if len(res.developments) > k:
        raise InvalidResponse(f"{len(res.developments)} developments returned, K is {k}")
    dev_ids = [d.id for d in res.developments]
    if len(dev_ids) != len(set(dev_ids)):
        raise InvalidResponse("duplicate development id")
    claim_ids = [c.claim_id for d in res.developments for c in d.claims]
    if len(claim_ids) != len(set(claim_ids)):
        raise InvalidResponse("duplicate claim_id")
    return list(res.developments)


def check_verify(r: WorkerResponse, sent_ids: list[str]) -> dict[str, VerdictOut]:
    if r.result is None:
        return {}
    try:
        res = VerifyResult.model_validate(r.result)
    except ValidationError as e:
        raise InvalidResponse(f"verify result does not match the contract: {_short(e)}")
    sent = set(sent_ids)
    out: dict[str, VerdictOut] = {}
    for v in res.verdicts:
        if v.claim_id not in sent:
            raise InvalidResponse(f"verdict for a claim_id that was not sent: {v.claim_id[:80]!r}")
        if v.claim_id in out:
            raise InvalidResponse(f"two verdicts for claim_id {v.claim_id[:80]!r}")
        out[v.claim_id] = v
    if r.status == "complete" and set(out) != sent:
        raise InvalidResponse(f"status complete but {len(sent - set(out))} sent claim(s) have no verdict")
    return out
