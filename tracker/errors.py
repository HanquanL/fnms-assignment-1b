"""Failure classification and retry (requirement 5).

Every external call ends in exactly one of:
  success
  TransientError  -> worth retrying: timeouts, network errors, 5xx, per-minute 429
  TerminalError   -> stop the run now: bad key, quota for the day/month used up,
                     payment required. Retrying these only burns time (and for a
                     daily quota it's a bug: the answer won't change until tomorrow).
  ToolInputError  -> the model asked for something invalid (bad query). Not fatal:
                     the message goes back to the model as the tool's result.
call_with_retry retries TransientError with capped exponential backoff and turns
"still failing after N attempts" into RetriesExhausted, a TerminalError.
"""
import random
import time
from dataclasses import dataclass
from typing import Callable, Mapping, TypeVar

T = TypeVar("T")


class TransientError(Exception):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class TerminalError(Exception):
    """Stop the run. The message must tell a human what to do about it."""


class RetriesExhausted(TerminalError):
    pass


class ToolInputError(Exception):
    """Bad arguments from the model; reported back to it, the run continues."""


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    base_delay: float
    max_delay: float

    @classmethod
    def from_config(cls, retry: Mapping) -> "RetryPolicy":
        return cls(int(retry["max_attempts"]), float(retry["base_delay_seconds"]),
                   float(retry["max_delay_seconds"]))


# (attempt number, the error, seconds we're about to sleep) -> used for the trace log
OnRetry = Callable[[int, TransientError, float], None]


def backoff_delay(policy: RetryPolicy, attempt: int, retry_after: float | None) -> float:
    """1s, 2s, 4s, ... plus jitter, capped; never shorter than what the server asked for."""
    delay = min(policy.max_delay, policy.base_delay * 2 ** (attempt - 1)) + random.uniform(0, policy.base_delay)
    if retry_after is not None:
        delay = max(delay, retry_after)
    return delay


def call_with_retry(fn: Callable[[], T], policy: RetryPolicy, *, what: str,
                    on_retry: OnRetry | None = None, sleep: Callable[[float], None] = time.sleep) -> T:
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return fn()
        except TransientError as e:
            if attempt == policy.max_attempts:
                raise RetriesExhausted(f"{what}: still failing after {attempt} attempts ({e})") from e
            if e.retry_after is not None and e.retry_after > policy.max_delay:
                raise TerminalError(
                    f"{what}: server asked us to wait {e.retry_after:.0f}s, longer than the "
                    f"{policy.max_delay:.0f}s we allow; treating it as a quota, not a blip ({e})"
                ) from e
            delay = backoff_delay(policy, attempt, e.retry_after)
            if on_retry:
                on_retry(attempt, e, delay)
            sleep(delay)
    raise AssertionError("unreachable")


def parse_retry_after(value: str | None) -> float | None:
    """Retry-After in seconds (the HTTP-date form is rare for APIs; ignore it)."""
    try:
        return max(0.0, float(value)) if value is not None else None
    except ValueError:
        return None