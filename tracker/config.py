"""Load config.yaml once and hand out read-only views of it.

Requirement: web text must not be able to change the agent's instructions,
tools, or budget. The policy is read from disk at startup and frozen; nothing
the model or a web page produces is ever written back into it.
"""
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


@lru_cache(maxsize=1)
def load_config(path: Path = CONFIG_PATH) -> MappingProxyType:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    for key in ("topic", "k", "model", "tools", "limits", "fetch", "search"):
        if key not in raw:
            raise ValueError(f"config.yaml is missing '{key}'")
    if not 3 <= int(raw["k"]) <= 10:
        raise ValueError("config.yaml: k must be between 3 and 10")
    return _freeze(raw)