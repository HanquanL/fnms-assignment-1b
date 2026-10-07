import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String, Uuid, func, JSON, ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

def _user_fk():
    return mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(32), nullable=False)
    email: Mapped[str | None] = mapped_column(String(254), unique=True, nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        # Case-insensitive uniqueness: "NYUgrader" and "nyugrader" can't both exist
        # Uniqueness is guaranteed by the database
        Index("uq_users_username_lower", func.lower(username), unique=True),
    )

class TrackerRun(Base):
    """One execution of the tracker: when, how it ended, what it cost, and the report."""
    __tablename__ = "tracker_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = _user_fk()
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # complete | partial | failed
    stop_reason: Mapped[str | None] = mapped_column(String(500))
    topic: Mapped[str] = mapped_column(String(200), nullable=False)
    k: Mapped[int] = mapped_column(Integer, nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    stats: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    report_md: Mapped[str] = mapped_column(Text, nullable=False)

class TrackerArticle(Base):
    """Every URL a run touched, and what happened to it. 'Already seen' = fetched in any earlier run."""
    __tablename__ = "tracker_articles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tracker_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = _user_fk()
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    canonical_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    title: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # fetched | skipped | rejected | failed
    reason: Mapped[str | None] = mapped_column(String(500))          # why skipped / rejected / failed
    http_status: Mapped[int | None] = mapped_column(Integer)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_tracker_articles_user_canonical", "user_id", "canonical_url"),)

class TrackerDevelopment(Base):
    """A real-world development (e.g. a model release), independent of which articles reported it."""
    __tablename__ = "tracker_developments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = _user_fk()
    key: Mapped[str] = mapped_column(String(200), nullable=False) 
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)    # latest summary
    first_seen_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tracker_runs.id", ondelete="SET NULL")
    )
    last_seen_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tracker_runs.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Dedup is enforced by the database: one row per (user, key)
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_tracker_developments_user_key"),)

class TrackerSource(Base):
    """An article that supports a development, with the quote that backs the claim (provenance)."""
    __tablename__ = "tracker_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    development_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tracker_developments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    title: Mapped[str | None] = mapped_column(String(500))
    evidence: Mapped[str | None] = mapped_column(Text)  # sentence(s) copied from the article
    first_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tracker_runs.id", ondelete="SET NULL")
    )

    __table_args__ = (UniqueConstraint("development_id", "url", name="uq_tracker_sources_dev_url"),)

class TrackerRanking(Base):
    """A run's top-K snapshot. Dropped items are stored too (rank NULL) so 'what changed' is a lookup."""
    __tablename__ = "tracker_rankings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tracker_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    development_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tracker_developments.id", ondelete="CASCADE"), nullable=False
    )
    rank: Mapped[int | None] = mapped_column(Integer)             # 1..K, NULL when dropped
    change: Mapped[str] = mapped_column(String(16), nullable=False)  # new | still | dropped
    summary: Mapped[str] = mapped_column(Text, nullable=False)    # as reported in THIS run

    __table_args__ = (UniqueConstraint("run_id", "development_id", name="uq_tracker_rankings_run_dev"),)