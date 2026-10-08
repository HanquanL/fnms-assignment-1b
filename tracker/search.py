"""search_web via Tavily's REST API, with every failure classified.

Called through httpx directly (no SDK) so there are no hidden retries:
call_with_retry is the only place that retries, and the trace sees each attempt.
"""
import time
from dataclasses import asdict, dataclass, field
from typing import Mapping

import httpx

from tracker.errors import TerminalError, ToolInputError, TransientError, parse_retry_after
from tracker.extract import clean

TAVILY_URL = "https://api.tavily.com/search"
MAX_QUERY_CHARS = 400


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str
    published_date: str | None
    score: float | None


@dataclass
class SearchResponse:
    query: str
    hits: list[SearchHit] = field(default_factory=list)
    credits: float | None = None
    elapsed_ms: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def _error_message(resp: httpx.Response) -> str:
    try:
        detail = resp.json().get("detail")
        if isinstance(detail, dict):
            detail = detail.get("error") or detail
        return str(detail)[:300]
    except ValueError:
        return resp.text[:300]


def classify_tavily(resp: httpx.Response) -> Exception:
    """Map a non-2xx Tavily response to Transient / Terminal / ToolInput."""
    code, msg = resp.status_code, _error_message(resp)
    if code == 429:
        return TransientError(f"Tavily rate limit (429): {msg}", parse_retry_after(resp.headers.get("retry-after")))
    if code >= 500:
        return TransientError(f"Tavily server error ({code}): {msg}")
    if code in (401, 403):
        return TerminalError(f"Tavily rejected the API key ({code}). Check TAVILY_API_KEY in .env.")
    if code == 432:
        return TerminalError(f"Tavily plan limit reached (432): the monthly credits are used up; "
                             f"retrying won't help until they reset. {msg}")
    if code == 433:
        return TerminalError(f"Tavily pay-as-you-go limit reached (433). {msg}")
    if code in (400, 422):
        return ToolInputError(f"Tavily refused the query ({code}): {msg}")
    return TerminalError(f"Unexpected Tavily response {code}: {msg}")


def tavily_search(query: str, *, api_key: str, cfg: Mapping, timeout: float = 20.0,
                  transport: httpx.BaseTransport | None = None) -> SearchResponse:
    """ONE attempt. Raises TransientError / TerminalError / ToolInputError."""
    query = " ".join(query.split())
    if not query:
        raise ToolInputError("query is empty")
    if len(query) > MAX_QUERY_CHARS:
        raise ToolInputError(f"query is longer than {MAX_QUERY_CHARS} characters")

    payload = {
        "query": query,
        "max_results": int(cfg["max_results"]),
        "search_depth": cfg["search_depth"],
        "topic": cfg.get("topic", "general"),
        "include_usage": True,
    }
    if cfg.get("time_range"):
        payload["time_range"] = cfg["time_range"]

    started = time.monotonic()
    try:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            resp = client.post(TAVILY_URL, json=payload, headers={"Authorization": f"Bearer {api_key}"})
    except httpx.TimeoutException as e:
        raise TransientError(f"Tavily timed out after {timeout:g}s") from e
    except httpx.TransportError as e:  # DNS failure, connection refused/reset: e.g. the network is down
        raise TransientError(f"network error reaching Tavily: {type(e).__name__}") from e

    if resp.status_code != 200:
        raise classify_tavily(resp)
    try:
        data = resp.json()
    except ValueError as e:
        raise TransientError("Tavily returned a non-JSON body") from e

    hits = [
        SearchHit(
            title=clean(str(r.get("title") or ""))[:300],
            url=str(r.get("url") or "")[:2048],
            snippet=clean(str(r.get("content") or ""))[:500],
            published_date=r.get("published_date"),
            score=r.get("score"),
        )
        for r in data.get("results", [])
        if r.get("url")
    ]
    usage = data.get("usage") or {}
    return SearchResponse(query=query, hits=hits, credits=usage.get("credits"),
                          elapsed_ms=int((time.monotonic() - started) * 1000))
