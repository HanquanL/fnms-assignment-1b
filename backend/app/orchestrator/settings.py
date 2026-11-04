"""Orchestrator policy (config.yaml, read once) and secrets (environment only).

Service tokens and worker URLs never go in backend/.env (that file is committed). They come from
the real environment or the git-ignored repository-root .env:
    RESEARCHER_URL, RESEARCHER_TOKEN, VERIFIER_URL, VERIFIER_TOKEN
The tokens stay server-side: no API response and no trace line contains them.
"""
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]          # repository root
CONFIG_PATH = ROOT / "config.yaml"
BUDGET_KEYS = ("max_steps", "max_fetches", "max_tokens")


def _load_root_dotenv() -> None:
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Share:
    max_steps: int
    max_fetches: int
    max_tokens: int
    time_budget_ms: int

    def wire(self) -> dict:
        return {"max_steps": self.max_steps, "max_fetches": self.max_fetches, "max_tokens": self.max_tokens}


@dataclass(frozen=True)
class RetrySettings:
    max_attempts: int
    base_delay: float
    max_delay: float
    max_retry_after: float


@dataclass(frozen=True)
class WorkerSettings:
    name: str
    url: str
    token: str
    path: str
    share: Share
    cold_start_s: float


@dataclass(frozen=True)
class OrchestratorSettings:
    topic: str
    k: int
    total: dict                      # run_budget
    researcher: WorkerSettings
    verifier: WorkerSettings
    connect_timeout_s: float
    slack_s: float
    retry: RetrySettings
    max_seconds_per_worker: float
    prewarm_verifier: bool
    trace_path: Path

    def response_timeout(self, w: WorkerSettings) -> float:
        """Must exceed the worker's own time budget (PDF §6), plus a cold start before the worker
        even receives the request, plus slack for the network and the worker's last call."""
        return w.share.time_budget_ms / 1000.0 + w.cold_start_s + self.slack_s


def validate_allocation(total: dict, researcher: Share, verifier: Share) -> None:
    for key in BUDGET_KEYS:
        if getattr(researcher, key) + getattr(verifier, key) > int(total[key]):
            raise ValueError(f"config.yaml: allocation.{key} for researcher + verifier exceeds run_budget.{key}")


def from_config(cfg: dict, env: dict | None = None) -> OrchestratorSettings:
    env = os.environ if env is None else env
    o = cfg["orchestrator"]
    total = {k: int(cfg["run_budget"][k]) for k in BUDGET_KEYS}
    shares = {name: Share(**{k: int(v) for k, v in cfg["allocation"][name].items()})
              for name in ("researcher", "verifier")}
    validate_allocation(total, shares["researcher"], shares["verifier"])
    cold = o.get("cold_start_seconds") or {}
    r = o["retry"]
    return OrchestratorSettings(
        topic=str(cfg["topic"]), k=int(cfg["k"]), total=total,
        researcher=WorkerSettings("researcher", (env.get("RESEARCHER_URL") or o["researcher_url"]).rstrip("/"),
                                  env.get("RESEARCHER_TOKEN", "").strip(), "/research", shares["researcher"],
                                  float(cold.get("researcher", 0))),
        verifier=WorkerSettings("verifier", (env.get("VERIFIER_URL") or o["verifier_url"]).rstrip("/"),
                                env.get("VERIFIER_TOKEN", "").strip(), "/verify", shares["verifier"],
                                float(cold.get("verifier", 0))),
        connect_timeout_s=float(o["connect_timeout_seconds"]), slack_s=float(o["slack_seconds"]),
        retry=RetrySettings(int(r["max_attempts"]), float(r["base_delay_seconds"]), float(r["max_delay_seconds"]),
                            float(r["max_retry_after_seconds"])),
        max_seconds_per_worker=float(o["max_seconds_per_worker"]),
        prewarm_verifier=bool(o.get("prewarm_verifier", True)),
        trace_path=Path(env.get("ORCHESTRATOR_TRACE_PATH") or ROOT / "traces" / "orchestrator.jsonl"),
    )


@lru_cache(maxsize=1)
def load_settings() -> OrchestratorSettings:
    _load_root_dotenv()
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return from_config(yaml.safe_load(f))
