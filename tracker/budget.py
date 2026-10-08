"""Budgets, enforced by the runtime , not by asking the model nicely.

Two kinds of limit:
  run limits  (model steps, tokens, wall clock) -> BudgetExceeded: the loop stops
              and writes a report marked partial from the evidence so far.
  tool limits (searches, fetches) -> the tool refuses and says so; the model can
              still use what it has and call finish.
One model step is held back for that final finish call.
"""
import time
from dataclasses import dataclass, field
from typing import Mapping

from tracker.llm.base import Usage


class BudgetExceeded(Exception):
    pass


@dataclass
class Budget:
    max_steps: int
    max_searches: int
    max_fetches: int
    max_total_tokens: int
    max_wall_seconds: float
    steps: int = 0
    searches: int = 0
    fetches: int = 0
    tokens: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    started: float = field(default_factory=time.monotonic)

    @classmethod
    def from_config(cls, limits: Mapping) -> "Budget":
        return cls(int(limits["max_steps"]), int(limits["max_searches"]), int(limits["max_fetches"]),
                   int(limits["max_total_tokens"]), float(limits["max_wall_seconds"]))

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def check_run(self, reserve_steps: int = 1) -> None:
        """Before a normal model call. Keeps `reserve_steps` back for the forced finish."""
        if self.steps >= self.max_steps - reserve_steps:
            raise BudgetExceeded(f"max_steps ({self.max_steps}) reached")
        self.check_hard()

    def check_hard(self) -> None:
        """Limits that also stop the final finish call."""
        if self.tokens >= self.max_total_tokens:
            raise BudgetExceeded(f"max_total_tokens ({self.max_total_tokens:,}) reached: used {self.tokens:,}")
        if self.elapsed() >= self.max_wall_seconds:
            raise BudgetExceeded(f"max_wall_seconds ({self.max_wall_seconds:g}) reached")

    def add_model_call(self, usage: Usage) -> None:
        self.steps += 1
        self.prompt_tokens += usage.prompt
        self.output_tokens += usage.output
        self.thinking_tokens += usage.thinking
        # total includes thinking; fall back to the sum if a provider omits it
        self.tokens += usage.total or (usage.prompt + usage.output + usage.thinking)

    def can_search(self) -> bool:
        return self.searches < self.max_searches

    def can_fetch(self) -> bool:
        return self.fetches < self.max_fetches

    def remaining(self) -> dict:
        return {
            "model_steps_left": max(0, self.max_steps - 1 - self.steps),
            "searches_left": max(0, self.max_searches - self.searches),
            "fetches_left": max(0, self.max_fetches - self.fetches),
            "tokens_left": max(0, self.max_total_tokens - self.tokens),
            "seconds_left": max(0, int(self.max_wall_seconds - self.elapsed())),
        }

    def stats(self) -> dict:
        return {
            "steps": self.steps, "searches": self.searches, "fetches": self.fetches,
            "tokens_total": self.tokens, "tokens_prompt": self.prompt_tokens,
            "tokens_output": self.output_tokens, "tokens_thinking": self.thinking_tokens,
            "duration_s": round(self.elapsed(), 1),
        }
