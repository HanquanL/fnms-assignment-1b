"""Provider-neutral types for the agent loop.

The loop (tracker/agent.py) only speaks these types. Each provider adapter
(gemini.py today, maybe claude.py later) converts them to its own wire format
and classifies its own errors into Transient / Terminal.
"""
import time
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict  # JSON schema


@dataclass
class ToolCall:
    id: str | None
    name: str
    args: dict


@dataclass
class Usage:
    prompt: int = 0
    output: int = 0
    thinking: int = 0  # billed like output; counted in the budget
    total: int = 0


@dataclass
class UserText:
    text: str


@dataclass
class ModelTurn:
    text: str
    tool_calls: list[ToolCall]
    usage: Usage
    finish_reason: str | None
    raw: Any = None  # the provider's own message, replayed verbatim on the next call


@dataclass
class ToolResult:
    call: ToolCall
    content: dict


@dataclass
class ToolResults:
    results: list[ToolResult] = field(default_factory=list)


HistoryItem = UserText | ModelTurn | ToolResults


class LLMClient(Protocol):
    provider: str
    model: str

    def generate(self, system: str, history: list[HistoryItem], tools: list[ToolSpec]) -> ModelTurn:
        """ONE attempt. Raises TransientError / TerminalError (tracker.errors)."""
        ...


class Pacer:
    """Client-side rate limiting: space calls so we stay under requests_per_minute
    instead of waiting to be told off with a 429."""

    def __init__(self, requests_per_minute: float, sleep=time.sleep, clock=time.monotonic):
        self.interval = 60.0 / requests_per_minute
        self._sleep, self._clock = sleep, clock
        self._last: float | None = None

    def wait(self) -> float:
        waited = 0.0
        if self._last is not None:
            waited = max(0.0, self._last + self.interval - self._clock())
            if waited:
                self._sleep(waited)
        self._last = self._clock()
        return waited