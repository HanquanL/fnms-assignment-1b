import uuid
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RunStatus = Literal["complete", "partial", "failed"]
ArticleStatus = Literal["fetched", "skipped", "rejected", "failed"]
Change = Literal["new", "still", "dropped"]

KEY_PATTERN = r"^[a-z0-9][a-z0-9._/-]*$"

def _http_url(v: str) -> str:
    v = v.strip()
    parts = urlsplit(v)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("must be an absolute http(s) URL")
    return v


class ArticleIn(BaseModel):
    # URL must still be recorded so the UI can show the rejection.
    url: str = Field(min_length=1, max_length=2048)
    canonical_url: str = Field(min_length=1, max_length=2048)
    title: str | None = Field(default=None, max_length=500)
    status: ArticleStatus
    reason: str | None = Field(default=None, max_length=500)
    http_status: int | None = Field(default=None, ge=100, le=599)
    fetched_at: datetime

class SourceIn(BaseModel):
    url: str = Field(max_length=2048)
    title: str | None = Field(default=None, max_length=500)
    evidence: str | None = Field(default=None, max_length=2000)

    _check_url = field_validator("url")(_http_url)

class DevelopmentIn(BaseModel):
    key: str = Field(min_length=1, max_length=200, pattern=KEY_PATTERN)
    title: str = Field(min_length=1, max_length=300)
    summary: str = Field(min_length=1, max_length=4000)
    # Provenance is structural: a development can't be saved without a source
    sources: list[SourceIn] = Field(min_length=1, max_length=20)

class RankingIn(BaseModel):
    key: str = Field(min_length=1, max_length=200, pattern=KEY_PATTERN)
    rank: int | None = Field(default=None, ge=1, le=10)
    change: Change
    summary: str = Field(min_length=1, max_length=4000)

    @model_validator(mode="after")
    def rank_matches_change(self):
        if (self.change == "dropped") != (self.rank is None):
            raise ValueError("rank must be null exactly when change is 'dropped'")
        return self

class RunIn(BaseModel):
    started_at: datetime
    finished_at: datetime
    status: RunStatus
    stop_reason: str | None = Field(default=None, max_length=500)
    topic: str = Field(min_length=1, max_length=200)
    k: int = Field(ge=3, le=10)
    model: str = Field(min_length=1, max_length=100)
    stats: dict[str, int | float | str | None] = Field(default_factory=dict, max_length=50)
    report_md: str = Field(max_length=200_000)
    articles: list[ArticleIn] = Field(default_factory=list, max_length=500)
    developments: list[DevelopmentIn] = Field(default_factory=list, max_length=50)
    rankings: list[RankingIn] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def consistent(self):
        if self.finished_at < self.started_at:
            raise ValueError("finished_at is before started_at")
        dev_keys = [d.key for d in self.developments]
        if len(dev_keys) != len(set(dev_keys)):
            raise ValueError("duplicate development keys")
        rank_keys = [r.key for r in self.rankings]
        if len(rank_keys) != len(set(rank_keys)):
            raise ValueError("duplicate keys in rankings")
        ranks = [r.rank for r in self.rankings if r.rank is not None]
        if len(ranks) != len(set(ranks)):
            raise ValueError("duplicate ranks")
        if any(r > self.k for r in ranks):
            raise ValueError("rank greater than k")
        return self

class RunIn(BaseModel):
    started_at: datetime
    finished_at: datetime
    status: RunStatus
    stop_reason: str | None = Field(default=None, max_length=500)
    topic: str = Field(min_length=1, max_length=200)
    k: int = Field(ge=3, le=10)
    model: str = Field(min_length=1, max_length=100)
    stats: dict[str, int | float | str | None] = Field(default_factory=dict, max_length=50)
    report_md: str = Field(max_length=200_000)
    articles: list[ArticleIn] = Field(default_factory=list, max_length=500)
    developments: list[DevelopmentIn] = Field(default_factory=list, max_length=50)
    rankings: list[RankingIn] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def consistent(self):
        if self.finished_at < self.started_at:
            raise ValueError("finished_at is before started_at")
        dev_keys = [d.key for d in self.developments]
        if len(dev_keys) != len(set(dev_keys)):
            raise ValueError("duplicate development keys")
        rank_keys = [r.key for r in self.rankings]
        if len(rank_keys) != len(set(rank_keys)):
            raise ValueError("duplicate keys in rankings")
        ranks = [r.rank for r in self.rankings if r.rank is not None]
        if len(ranks) != len(set(ranks)):
            raise ValueError("duplicate ranks")
        if any(r > self.k for r in ranks):
            raise ValueError("rank greater than k")
        return self

# ---------- Responses (backend -> tracker / frontend) ----------

class SourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    url: str
    title: str | None
    evidence: str | None


class RankingOut(BaseModel):
    rank: int | None
    change: Change
    key: str
    title: str
    summary: str
    sources: list[SourceOut]


class ArticleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    url: str
    canonical_url: str
    title: str | None
    status: ArticleStatus
    reason: str | None
    http_status: int | None
    fetched_at: datetime


class RunCounts(BaseModel):
    new: int = 0
    still: int = 0
    dropped: int = 0
    fetched: int = 0
    skipped: int = 0
    rejected: int = 0
    failed: int = 0


class RunSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    started_at: datetime
    finished_at: datetime
    status: RunStatus
    stop_reason: str | None
    topic: str
    k: int
    model: str
    stats: dict
    counts: RunCounts = RunCounts()


class RunDetailOut(RunSummaryOut):
    report_md: str
    rankings: list[RankingOut]
    articles: list[ArticleOut]


class StateDevelopment(BaseModel):
    key: str
    title: str
    summary: str
    sources: list[SourceOut]


class StateTopK(BaseModel):
    rank: int
    key: str
    title: str
    summary: str


class StateOut(BaseModel):
    """Everything the tracker needs to remember at the start of a run."""
    seen_urls: list[str]
    developments: list[StateDevelopment]
    last_run_id: uuid.UUID | None
    last_run_finished_at: datetime | None
    last_top_k: list[StateTopK]