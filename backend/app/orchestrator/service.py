"""One orchestrated run, start to finish. Plain code; no model decides anything here.

    1. load memory from the A1 database (known development keys, last run's top K)
    2. split the run budget (config.yaml run_budget) into the two shares; refuse to over-allocate
    3. (background) GET /healthz on the verifier, to wake a sleeping Render instance early
    4. POST /research  -> validate -> developments (or: researcher failed)
    5. POST /verify with every claim's {claim_id, text, source_url} -> validate -> verdicts
       (or: verifier failed -> every claim "unverified")
    6. classify new / still / dropped, write the report from validated data only, and save
       everything in ONE transaction -- even when a worker failed (status partial, worker named)
"""
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import TrackerClaim, TrackerDevelopment, TrackerRanking, TrackerRun, TrackerSource
from app.orchestrator.memory import canonical_keys, classify
from app.orchestrator.report import clean_text, render
from app.orchestrator.settings import BUDGET_KEYS, OrchestratorSettings, Share, WorkerSettings
from app.orchestrator.validate import InvalidResponse, check_envelope, check_research, check_verify
from app.orchestrator.worker_client import CallResult, WorkerClient

MODEL_LABEL = "A2: researcher + verifier"
KEY_OK = re.compile(r"^[a-z0-9][a-z0-9._/-]*$")


# ------------------------------------------------------------------ budget ----

class Ledger:
    """One budget per run (PDF §8). Shares are reserved before a worker is called, so the sum of
    what was handed out never exceeds the total; charges are keyed by request_id, so a request
    is charged once however many attempts or replays it took."""

    def __init__(self, total: dict):
        self.total = dict(total)
        self.reserved: dict[str, dict] = {}
        self.charged: dict[str, dict] = {}       # request_id -> {steps, fetches, tokens}

    def reserve(self, request_id: str, share: Share) -> bool:
        want = share.wire()
        used = {k: sum(r[k] for r in self.reserved.values()) for k in BUDGET_KEYS}
        if any(used[k] + want[k] > self.total[k] for k in BUDGET_KEYS):
            return False
        self.reserved[request_id] = want
        return True

    def charge(self, request_id: str, usage: dict) -> None:
        self.charged[request_id] = {k: int(usage.get(k, 0)) for k in ("steps", "fetches", "tokens")}

    def charge_full(self, request_id: str) -> None:
        r = self.reserved[request_id]
        self.charge(request_id, {"steps": r["max_steps"], "fetches": r["max_fetches"], "tokens": r["max_tokens"]})

    def totals(self) -> dict:
        return {k: sum(c[k] for c in self.charged.values()) for k in ("steps", "fetches", "tokens")}


# ----------------------------------------------------------------- outcome ----

@dataclass
class WorkerOutcome:
    name: str
    status: str = "not_called"   # complete | partial | failed | skipped | not_called
    error: str | None = None
    request_id: str | None = None
    attempts: int = 0
    latency_ms: int = 0
    http_status: int | None = None
    usage: dict = field(default_factory=dict)
    searches: int = 0

    def public(self) -> dict:
        return {"status": self.status, "error": self.error}


def _envelope(run_id: str, w: WorkerSettings, payload: dict) -> dict:
    return {"request_id": str(uuid.uuid4()), "run_id": run_id, "time_budget_ms": w.share.time_budget_ms,
            "budget": w.share.wire(), "payload": payload}


def _settle(call: CallResult, sent: dict, w: WorkerSettings, ledger: Ledger, out: WorkerOutcome):
    """Decide what a call cost and whether its body can be trusted. Returns the validated
    WorkerResponse, or None if the worker failed."""
    out.request_id, out.attempts, out.latency_ms, out.http_status = sent["request_id"], call.attempts, \
        call.latency_ms, call.http_status
    rid = sent["request_id"]
    if not call.answered:
        ledger.charge_full(rid)                 # never answered: we can't know what it spent
        out.status, out.error = "failed", call.error
        return None
    if call.http_status in (400, 401):
        ledger.charge(rid, {})                  # refused before doing any work
        out.status, out.error = "failed", call.error
        return None
    try:
        resp = check_envelope(call.body, sent, w.share)
    except InvalidResponse as e:
        ledger.charge_full(rid)                 # can't trust its usage either
        out.status = "failed"
        out.error = call.error or f"invalid response: {e}"
        return None
    ledger.charge(rid, resp.usage.model_dump())
    out.usage = resp.usage.model_dump()
    out.searches = resp.usage.searches
    if call.http_status != 200:
        out.status, out.error = "failed", call.error or (resp.error.message if resp.error else None)
        return None
    out.status = resp.status
    if resp.error is not None:
        out.error = f"{resp.error.code}: {resp.error.message}"
    return resp


# ----------------------------------------------------------------- memory ----

def load_memory(db: Session, user_id) -> tuple[list[str], dict | None]:
    known = list(db.scalars(select(TrackerDevelopment.key).where(TrackerDevelopment.user_id == user_id)))
    # last run's top K = the most recent run that HAS a ranked list (a run whose researcher failed
    # has none; see DECISIONS.md C2)
    last = db.scalar(
        select(TrackerRun).where(TrackerRun.user_id == user_id, TrackerRun.status != "failed",
                                 TrackerRun.id.in_(select(TrackerRanking.run_id).where(TrackerRanking.rank.is_not(None))))
        .order_by(TrackerRun.finished_at.desc()).limit(1))
    if last is None:
        return known, None
    items = [{"rank": r.rank, "key": d.key, "title": d.title, "summary": r.summary}
             for r, d in db.execute(
                 select(TrackerRanking, TrackerDevelopment)
                 .join(TrackerDevelopment, TrackerRanking.development_id == TrackerDevelopment.id)
                 .where(TrackerRanking.run_id == last.id, TrackerRanking.rank.is_not(None))
                 .order_by(TrackerRanking.rank))]
    return known, {"run_id": str(last.id), "finished_at": last.finished_at, "items": items}


def _safe_key(key: str) -> str:
    k = re.sub(r"[^a-z0-9._/-]", "-", key.lower())[:200].strip("-") or "unknown"
    return k if KEY_OK.match(k) else "x" + k[:199]


# -------------------------------------------------------------------- run ----

Progress = Callable[[str], None]


class Orchestrator:
    def __init__(self, settings: OrchestratorSettings, session_factory, client: WorkerClient, trace):
        self.s, self.session_factory, self.client, self.trace = settings, session_factory, client, trace

    def run(self, user_id, *, run_id: str | None = None, progress: Progress = lambda stage: None) -> str:
        s = self.s
        run_id = run_id or str(uuid.uuid4())
        started_at = datetime.now(timezone.utc)
        t0 = time.monotonic()
        ledger = Ledger(s.total)
        self.trace.log(run_id=run_id, request_id=None, step=0, direction="in", endpoint_or_tool="run:start",
                       status="started", latency_ms=0, attempt=None, tokens=0, topic=s.topic, k=s.k)

        progress("loading memory")
        with self.session_factory() as db:
            known_keys, last_top = load_memory(db, user_id)

        prewarm = None
        if s.prewarm_verifier:
            prewarm = threading.Thread(target=self.client.ping, args=(s.verifier,), kwargs={"run_id": run_id},
                                       daemon=True)
            prewarm.start()

        # ---- researcher
        progress("researching")
        r_out = WorkerOutcome("researcher")
        r_env = _envelope(run_id, s.researcher, {"topic": s.topic, "k": s.k})
        developments = []
        if not ledger.reserve(r_env["request_id"], s.researcher.share):
            r_out.status, r_out.error = "skipped", "allocation exceeds the run budget"
        else:
            call = self.client.call(s.researcher, r_env, step=1)
            resp = _settle(call, r_env, s.researcher, ledger, r_out)
            if resp is not None:
                try:
                    developments = check_research(resp, s.k)
                except InvalidResponse as e:     # discard ALL of it; it never reaches the report
                    ledger.charge_full(r_env["request_id"])
                    r_out.status, r_out.error, developments = "failed", f"invalid response: {e}", []

        # ---- merge near-duplicate developments onto known keys (orchestrator-owned memory)
        keymap = canonical_keys([d.id for d in developments], known_keys)
        merged: dict[str, dict] = {}
        for d in developments:
            key = _safe_key(keymap[d.id])
            claims = [{"claim_id": c.claim_id, "text": clean_text(c.text, 1000), "source_url": c.source_url}
                      for c in d.claims]
            if key in merged:
                merged[key]["claims"] = (merged[key]["claims"] + claims)[:10]
                continue
            merged[key] = {"key": key, "wire_id": d.id, "title": clean_text(d.title, 300) or key,
                           "summary": clean_text(d.summary, 4000), "claims": claims}
        devs = list(merged.values())
        all_claims = [c for d in devs for c in d["claims"]]

        # ---- verifier
        v_out = WorkerOutcome("verifier")
        verdicts = {}
        if not all_claims:
            v_out.status = "skipped"
            v_out.error = "no claims to verify"
        else:
            progress("verifying")
            v_env = _envelope(run_id, s.verifier, {"claims": [{"claim_id": c["claim_id"], "text": c["text"],
                                                               "source_url": c["source_url"]} for c in all_claims]})
            if not ledger.reserve(v_env["request_id"], s.verifier.share):
                v_out.status, v_out.error = "skipped", "not enough run budget left for the verifier"
            else:
                call = self.client.call(s.verifier, v_env, step=2)
                resp = _settle(call, v_env, s.verifier, ledger, v_out)
                if resp is not None:
                    try:
                        verdicts = check_verify(resp, [c["claim_id"] for c in all_claims])
                    except InvalidResponse as e:
                        ledger.charge_full(v_env["request_id"])
                        v_out.status, v_out.error, verdicts = "failed", f"invalid response: {e}", {}
        for c in all_claims:
            v = verdicts.get(c["claim_id"])
            c["verdict"] = v.verdict if v else "unverified"
            c["evidence"] = clean_text(v.evidence, 300) if v and v.verdict == "supported" else None
            c["verifier_request_id"] = v_out.request_id if v else None

        # ---- status and memory
        progress("saving")
        researcher_ok = r_out.status == "complete"
        verifier_ok = v_out.status == "complete" or (v_out.status == "skipped" and not all_claims)
        status = "complete" if researcher_ok and verifier_ok and all(c["verdict"] != "unverified" for c in all_claims) \
            else "partial"
        failed_workers = [w.name for w in (r_out, v_out) if w.status == "failed" or
                          (w.status == "skipped" and w.error and "budget" in w.error)]
        partial_workers = [w.name for w in (r_out, v_out) if w.status == "partial"]
        stale = last_top if (not devs and r_out.status != "complete") else None
        has_previous = last_top is not None
        changes, dropped = classify([d["key"] for d in devs], (last_top or {}).get("items", []), researcher_ok)

        notes = []
        if r_out.status != "complete":
            notes.append(f"Researcher {r_out.status}: {r_out.error or 'no details'}")
        if v_out.status not in ("complete", "skipped") or (v_out.status == "skipped" and all_claims):
            notes.append(f"Verifier {v_out.status}: {v_out.error or 'no details'}. Claims without a valid verdict "
                         f"are shown as UNVERIFIED.")
        counts = {k: sum(1 for c in all_claims if c["verdict"] == k)
                  for k in ("supported", "unsupported", "unreachable", "unverified")}
        charged = ledger.totals()
        finished_at = datetime.now(timezone.utc)
        duration = round(time.monotonic() - t0, 1)
        stats = {
            "kind": "a2", "failed_workers": ",".join(failed_workers) or None,
            "partial_workers": ",".join(partial_workers) or None,
            "researcher_status": r_out.status, "verifier_status": v_out.status,
            "researcher_error": (r_out.error or None) and r_out.error[:300],
            "verifier_error": (v_out.error or None) and v_out.error[:300],
            "researcher_request_id": r_out.request_id, "verifier_request_id": v_out.request_id,
            "researcher_attempts": r_out.attempts, "verifier_attempts": v_out.attempts,
            "researcher_latency_ms": r_out.latency_ms, "verifier_latency_ms": v_out.latency_ms,
            "budget_steps": s.total["max_steps"], "budget_fetches": s.total["max_fetches"],
            "budget_tokens": s.total["max_tokens"],
            "charged_steps": charged["steps"], "charged_fetches": charged["fetches"],
            "charged_tokens": charged["tokens"],
            "researcher_steps": r_out.usage.get("steps"), "researcher_fetches": r_out.usage.get("fetches"),
            "researcher_tokens": r_out.usage.get("tokens"),
            "verifier_steps": v_out.usage.get("steps"), "verifier_fetches": v_out.usage.get("fetches"),
            "verifier_tokens": v_out.usage.get("tokens"),
            "searches": r_out.searches,
            # the keys A1B's page already shows
            "steps": charged["steps"], "fetches": charged["fetches"], "tokens_total": charged["tokens"],
            "duration_s": duration,
            **{f"claims_{k}": v for k, v in counts.items()},
            "stale_from_run_id": stale["run_id"] if stale else None,
        }
        md = render(topic=s.topic, k=s.k, run_id=run_id, started_at=started_at, status=status,
                    workers={"researcher": r_out.public(), "verifier": v_out.public()}, stats=stats,
                    developments=devs, changes=changes if has_previous else None, dropped=dropped, stale=stale,
                    verdict_counts=counts, notes=notes)
        stop_reason = "; ".join(notes)[:500] or None

        save_ms = self._save(user_id, run_id, started_at, finished_at, status, stop_reason, stats, md, devs,
                             changes, dropped)
        self.trace.log(run_id=run_id, request_id=None, step=3, direction="out", endpoint_or_tool="db:save_run",
                       status="ok", latency_ms=save_ms, attempt=1, tokens=0)
        self.trace.log(run_id=run_id, request_id=None, step=3, direction="in", endpoint_or_tool="run:finish",
                       status=status, latency_ms=int((time.monotonic() - t0) * 1000), attempt=None,
                       tokens=charged["tokens"], failed_workers=failed_workers or None, verdicts=counts)
        return run_id

    def _save(self, user_id, run_id, started_at, finished_at, status, stop_reason, stats, md, devs, changes, dropped):
        """All rows of the run in one transaction. Only the orchestrator writes these tables."""
        t = time.monotonic()
        with self.session_factory() as db:
            run = TrackerRun(id=uuid.UUID(run_id), user_id=user_id, started_at=started_at, finished_at=finished_at,
                             status=status, stop_reason=stop_reason, topic=self.s.topic[:200], k=self.s.k,
                             model=MODEL_LABEL, stats=stats, report_md=md[:200_000])
            db.add(run)
            db.flush()
            keys = [d["key"] for d in devs] + [t_["key"] for t_ in dropped]
            existing = {d.key: d for d in db.scalars(select(TrackerDevelopment).where(
                TrackerDevelopment.user_id == user_id, TrackerDevelopment.key.in_(keys)))} if keys else {}
            for rank, d in enumerate(devs, start=1):
                dev = existing.get(d["key"])
                if dev is None:
                    dev = TrackerDevelopment(user_id=user_id, key=d["key"], title=d["title"], summary=d["summary"],
                                             first_seen_run_id=run.id, last_seen_run_id=run.id)
                    db.add(dev)
                    db.flush()
                    existing[d["key"]] = dev
                else:
                    dev.title, dev.summary, dev.last_seen_run_id = d["title"], d["summary"], run.id
                known_urls = set(db.scalars(select(TrackerSource.url).where(TrackerSource.development_id == dev.id)))
                for c in d["claims"]:
                    if c["source_url"] not in known_urls:   # researcher's evidence is not on the wire: NULL
                        db.add(TrackerSource(development_id=dev.id, url=c["source_url"], title=None, evidence=None,
                                             first_run_id=run.id))
                        known_urls.add(c["source_url"])
                db.add(TrackerRanking(run_id=run.id, development_id=dev.id, rank=rank,
                                      change=changes.get(d["key"], "new"), summary=d["summary"] or d["title"]))
                for pos, c in enumerate(d["claims"], start=1):
                    db.add(TrackerClaim(run_id=run.id, user_id=user_id, development_id=dev.id,
                                        development_key=d["key"], claim_id=c["claim_id"][:200], position=pos,
                                        text=c["text"], source_url=c["source_url"][:2048], verdict=c["verdict"],
                                        evidence=c["evidence"],
                                        verifier_request_id=uuid.UUID(c["verifier_request_id"])
                                        if c["verifier_request_id"] else None))
            for item in dropped:
                dev = existing.get(item["key"])
                if dev is not None:
                    db.add(TrackerRanking(run_id=run.id, development_id=dev.id, rank=None, change="dropped",
                                          summary=item["summary"]))
            db.commit()
        return int((time.monotonic() - t) * 1000)


def count_claims(db: Session, run_ids: list) -> dict:
    out: dict = {}
    if run_ids:
        for rid, verdict, n in db.execute(select(TrackerClaim.run_id, TrackerClaim.verdict, func.count())
                                          .where(TrackerClaim.run_id.in_(run_ids))
                                          .group_by(TrackerClaim.run_id, TrackerClaim.verdict)):
            out.setdefault(rid, {})[verdict] = n
    return out
