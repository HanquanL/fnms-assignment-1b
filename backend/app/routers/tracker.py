import uuid
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user
from app.models import TrackerArticle, TrackerDevelopment, TrackerRanking, TrackerRun, TrackerSource, User
from app.tracker_schemas import (
    ArticleOut,
    RankingOut,
    RunCounts,
    RunDetailOut,
    RunIn,
    RunSummaryOut,
    SourceOut,
    StateDevelopment,
    StateOut,
    StateTopK,
)

router = APIRouter(prefix="/api/tracker", tags=["tracker"])


def _not_found(detail: str = "Run not found") -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def get_owned_run(
    run_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TrackerRun:
    """Same rule as A1's /api/users/:id: someone else's run, a missing run,
    and a malformed id all get the identical 404. The ownership filter is in
    the query itself, so another user's run is never loaded."""
    try:
        rid = uuid.UUID(run_id)
    except ValueError:
        raise _not_found()
    run = db.scalar(select(TrackerRun).where(TrackerRun.id == rid, TrackerRun.user_id == current_user.id))
    if run is None:
        raise _not_found()
    return run


# ---------- helpers ----------

def _sources_by_dev(db: Session, dev_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[SourceOut]]:
    out: dict[uuid.UUID, list[SourceOut]] = defaultdict(list)
    for s in db.scalars(
        select(TrackerSource).where(TrackerSource.development_id.in_(dev_ids)).order_by(TrackerSource.id)
    ):
        out[s.development_id].append(SourceOut.model_validate(s))
    return out


def _summaries(db: Session, runs: list[TrackerRun]) -> list[RunSummaryOut]:
    """Run summaries with new/still/dropped and article-status counts (2 queries total, not 2 per run)."""
    ids = [r.id for r in runs]
    counts: dict[uuid.UUID, dict[str, int]] = {rid: {} for rid in ids}
    if ids:
        for rid, change, n in db.execute(
            select(TrackerRanking.run_id, TrackerRanking.change, func.count())
            .where(TrackerRanking.run_id.in_(ids))
            .group_by(TrackerRanking.run_id, TrackerRanking.change)
        ):
            counts[rid][change] = n
        for rid, st, n in db.execute(
            select(TrackerArticle.run_id, TrackerArticle.status, func.count())
            .where(TrackerArticle.run_id.in_(ids))
            .group_by(TrackerArticle.run_id, TrackerArticle.status)
        ):
            counts[rid][st] = n
    return [
        RunSummaryOut.model_validate(r).model_copy(update={"counts": RunCounts(**counts[r.id])})
        for r in runs
    ]


def _detail(db: Session, run: TrackerRun) -> RunDetailOut:
    rows = db.execute(
        select(TrackerRanking, TrackerDevelopment)
        .join(TrackerDevelopment, TrackerRanking.development_id == TrackerDevelopment.id)
        .where(TrackerRanking.run_id == run.id)
    ).all()
    sources = _sources_by_dev(db, [dev.id for _, dev in rows])
    rankings = [
        RankingOut(rank=r.rank, change=r.change, key=d.key, title=d.title, summary=r.summary, sources=sources[d.id])
        for r, d in rows
    ]
    rankings.sort(key=lambda x: (x.rank is None, x.rank or 0, x.title))  # ranked 1..K, then dropped

    articles = db.scalars(
        select(TrackerArticle)
        .where(TrackerArticle.run_id == run.id)
        .order_by(TrackerArticle.fetched_at, TrackerArticle.id)
    ).all()

    summary = _summaries(db, [run])[0]
    return RunDetailOut(
        **summary.model_dump(),
        report_md=run.report_md,
        rankings=rankings,
        articles=[ArticleOut.model_validate(a) for a in articles],
    )


# ---------- tracker-facing ----------

@router.get("/state", response_model=StateOut)
def get_state(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Memory loaded at the start of a run."""
    seen = db.scalars(
        select(TrackerArticle.canonical_url)
        .where(TrackerArticle.user_id == current_user.id, TrackerArticle.status == "fetched")
        .distinct()
    ).all()

    devs = db.scalars(
        select(TrackerDevelopment)
        .where(TrackerDevelopment.user_id == current_user.id)
        .order_by(TrackerDevelopment.created_at)
    ).all()
    sources = _sources_by_dev(db, [d.id for d in devs])

    last_run = db.scalar(
        select(TrackerRun)
        .where(TrackerRun.user_id == current_user.id, TrackerRun.status != "failed")
        .order_by(TrackerRun.finished_at.desc())
        .limit(1)
    )
    top_k: list[StateTopK] = []
    if last_run is not None:
        for r, d in db.execute(
            select(TrackerRanking, TrackerDevelopment)
            .join(TrackerDevelopment, TrackerRanking.development_id == TrackerDevelopment.id)
            .where(TrackerRanking.run_id == last_run.id, TrackerRanking.rank.is_not(None))
            .order_by(TrackerRanking.rank)
        ):
            top_k.append(StateTopK(rank=r.rank, key=d.key, title=d.title, summary=r.summary))

    return StateOut(
        seen_urls=sorted(seen),
        developments=[
            StateDevelopment(key=d.key, title=d.title, summary=d.summary, sources=sources[d.id]) for d in devs
        ],
        last_run_id=last_run.id if last_run else None,
        last_run_finished_at=last_run.finished_at if last_run else None,
        last_top_k=top_k,
    )


@router.post("/runs", response_model=RunSummaryOut, status_code=status.HTTP_201_CREATED)
def create_run(body: RunIn, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Save one finished run in a single transaction: all of it, or none of it."""
    run = TrackerRun(
        user_id=current_user.id,
        **body.model_dump(include={"started_at", "finished_at", "status", "stop_reason",
                                   "topic", "k", "model", "stats", "report_md"}),
    )
    db.add(run)
    db.flush()  # assigns run.id

    for a in body.articles:
        db.add(TrackerArticle(run_id=run.id, user_id=current_user.id, **a.model_dump()))

    # Upsert developments by (user, key) -- the dedup identity
    keys = {d.key for d in body.developments} | {r.key for r in body.rankings}
    existing = {
        d.key: d
        for d in db.scalars(
            select(TrackerDevelopment).where(
                TrackerDevelopment.user_id == current_user.id, TrackerDevelopment.key.in_(keys)
            )
        )
    }
    for d in body.developments:
        dev = existing.get(d.key)
        if dev is None:
            dev = TrackerDevelopment(
                user_id=current_user.id, key=d.key, title=d.title, summary=d.summary,
                first_seen_run_id=run.id, last_seen_run_id=run.id,
            )
            db.add(dev)
            db.flush()
            existing[d.key] = dev
        else:
            dev.title, dev.summary, dev.last_seen_run_id = d.title, d.summary, run.id

        known_urls = set(db.scalars(select(TrackerSource.url).where(TrackerSource.development_id == dev.id)))
        for s in d.sources:
            if s.url not in known_urls:
                db.add(TrackerSource(development_id=dev.id, url=s.url, title=s.title,
                                     evidence=s.evidence, first_run_id=run.id))
                known_urls.add(s.url)

    for r in body.rankings:
        dev = existing.get(r.key)
        if dev is None:
            db.rollback()
            raise HTTPException(status_code=422, detail=f"Ranking refers to an unknown development: {r.key}")
        if r.change != "dropped":
            dev.last_seen_run_id = run.id
        db.add(TrackerRanking(run_id=run.id, development_id=dev.id, rank=r.rank, change=r.change, summary=r.summary))

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Conflicting tracker data")
    db.refresh(run)
    return _summaries(db, [run])[0]


@router.delete("/state", status_code=status.HTTP_204_NO_CONTENT)
def reset_state(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Forget everything for this user. The DB's ON DELETE CASCADE removes
    sources, rankings, and articles along with developments and runs."""
    db.execute(delete(TrackerDevelopment).where(TrackerDevelopment.user_id == current_user.id))
    db.execute(delete(TrackerRun).where(TrackerRun.user_id == current_user.id))
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------- frontend-facing ----------

@router.get("/runs", response_model=list[RunSummaryOut])
def list_runs(
    limit: int = Query(default=50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    runs = db.scalars(
        select(TrackerRun)
        .where(TrackerRun.user_id == current_user.id)
        .order_by(TrackerRun.started_at.desc())
        .limit(limit)
    ).all()
    return _summaries(db, list(runs))


# Declared before /runs/{run_id} so "latest" isn't parsed as a run id
@router.get("/runs/latest", response_model=RunDetailOut)
def latest_run(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    run = db.scalar(
        select(TrackerRun)
        .where(TrackerRun.user_id == current_user.id, TrackerRun.status != "failed")
        .order_by(TrackerRun.started_at.desc())
        .limit(1)
    )
    if run is None:
        raise _not_found("No runs yet")
    return _detail(db, run)


@router.get("/runs/{run_id}", response_model=RunDetailOut)
def get_run(run: TrackerRun = Depends(get_owned_run), db: Session = Depends(get_db)):
    return _detail(db, run)