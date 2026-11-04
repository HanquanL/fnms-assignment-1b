"""Memory between runs, now owned by the orchestrator (PDF: only it reads or writes run history).

Same rules as A1B's tracker/memory.py (re-implemented here, not imported: tracker/ is the
researcher's code). The researcher has no memory, so it can't reuse known keys itself; the
orchestrator maps a near-miss key ("deepseek/deepseek-v4-1-flash/launch") onto the known one
("deepseek/v4.1-flash/release") before deciding new / still / dropped.
"""
import re

ORG_ALIASES = {"alibaba": "qwen", "alibaba-cloud": "qwen", "qwenlm": "qwen", "facebook": "meta",
               "meta-ai": "meta", "mistral-ai": "mistral", "mistralai": "mistral", "google-deepmind": "google",
               "deepmind": "google", "zhipu": "z-ai", "zhipu-ai": "z-ai", "zai": "z-ai", "moonshot-ai": "moonshot",
               "reflection": "reflection-ai", "deepseek-ai": "deepseek", "nvidia-ai": "nvidia"}


def _flat(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def identity(key: str) -> tuple[str, str]:
    """('qwen', '4') for 'alibaba/Qwen-4/release': org alias + model name; the event part is ignored."""
    parts = key.lower().split("/")
    org = ORG_ALIASES.get(parts[0], parts[0])
    model = parts[1] if len(parts) > 1 else ""
    flat_model, flat_org = _flat(model), _flat(org)
    if flat_model.startswith(flat_org) and len(flat_model) > len(flat_org):
        flat_model = flat_model[len(flat_org):]
    return flat_org, flat_model


def canonical_keys(dev_ids: list[str], known_keys: list[str]) -> dict[str, str]:
    """wire development id -> the key to store it under. A known development keeps its known key;
    two developments in one report that are the same thing map to the first (higher-ranked) one."""
    known = {identity(k): k for k in known_keys}
    out: dict[str, str] = {}
    used: dict[tuple[str, str], str] = {}
    for dev_id in dev_ids:
        ident = identity(dev_id)
        target = known.get(ident) or used.get(ident) or dev_id.lower()
        used.setdefault(ident, target)
        out[dev_id] = target
    return out


def classify(keys_in_order: list[str], last_top_k: list[dict], researcher_complete: bool) -> tuple[dict, list]:
    """key -> 'new' | 'still' for this run's list, plus the dropped items of last run's top K.
    Only a COMPLETE research result can drop something: a cut-short run didn't look hard enough
    to say a development fell out of the top K."""
    last = {t["key"]: t for t in last_top_k}
    changes = {k: ("still" if k in last else "new") for k in keys_in_order}
    dropped = [t for key, t in last.items() if key not in changes] if researcher_complete else []
    return changes, sorted(dropped, key=lambda t: t["rank"])
