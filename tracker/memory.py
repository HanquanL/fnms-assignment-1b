"""Memory between runs (requirements 6-7): what "new" means, decided by code.

Identity of a development is its key, "org/model/event". The model is shown the
known keys and asked to reuse them, but it doesn't always copy them exactly
("deepseek/v4.1-flash/release" vs "deepseek/deepseek-v4-1-flash/launch"), so
the code normalizes keys and maps a near-miss onto the known key before
deciding new / still / dropped.

Known blind spot (AGENT.md question 3): identity is per org + model, so two
genuinely different events about the same model (its release, then a license
change) collapse into one development.
"""
import re
from typing import Any

from tracker.agent import AgentResult, Memory
from tracker.urls import canonicalize

ORG_ALIASES = {"alibaba": "qwen", "alibaba-cloud": "qwen", "qwenlm": "qwen", "facebook": "meta",
               "meta-ai": "meta", "mistral-ai": "mistral", "mistralai": "mistral", "google-deepmind": "google",
               "deepmind": "google", "zhipu": "z-ai", "zhipu-ai": "z-ai", "zai": "z-ai", "moonshot-ai": "moonshot",
               "reflection": "reflection-ai", "deepseek-ai": "deepseek", "nvidia-ai": "nvidia"}


def _flat(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def identity(key: str) -> tuple[str, str]:
    """('qwen', 'qwen4') for 'alibaba/Qwen-4/release'. The event part is ignored on purpose."""
    parts = key.lower().split("/")
    org = ORG_ALIASES.get(parts[0], parts[0])
    model = parts[1] if len(parts) > 1 else ""
    flat_model, flat_org = _flat(model), _flat(org)
    if flat_model.startswith(flat_org) and len(flat_model) > len(flat_org):
        flat_model = flat_model[len(flat_org):]  # "deepseek/deepseek-v4" -> "v4"
    return _flat(org), flat_model


def memory_from_state(state: dict) -> Memory:
    return Memory(seen_urls=set(state.get("seen_urls", [])),
                  developments=list(state.get("developments", [])),
                  last_top_k=list(state.get("last_top_k", [])))


def dedupe_against_memory(report: dict, memory: Memory) -> list[tuple[str, str]]:
    """Rename keys that match a known development to the known key; merge
    duplicates inside the report. Returns the (old, new) renames for the trace."""
    known = {identity(d["key"]): d["key"] for d in memory.developments}
    renames: list[tuple[str, str]] = []
    merged: dict[str, dict] = {}
    for dev in report["developments"]:
        original = dev["key"]
        target = known.get(identity(original), original)
        if target not in merged:  # also catch two entries for one development inside this report
            target = next((m for m in merged if identity(m) == identity(target)), target)
        dev["key"] = target
        if target in merged:  # two entries for one development: keep the higher-ranked, add the other's claims
            renames.append((f"{original} (merged into the higher-ranked entry)", target))
            keep = merged[target]
            keep["claims"] = (keep.get("claims", []) + dev.get("claims", []))[:4]
            keep["summary"] = " ".join(c["text"] for c in keep["claims"])
            urls = {s["url"] for s in keep["sources"]}
            keep["sources"] += [s for s in dev["sources"] if s["url"] not in urls]
        else:
            if target != original:
                renames.append((original, target))
            merged[target] = dev
    report["developments"] = sorted(merged.values(), key=lambda d: d["rank"])
    for i, dev in enumerate(report["developments"], start=1):
        dev["rank"] = i
    return renames


def classify(report: dict, memory: Memory, run_complete: bool) -> tuple[dict[str, str], list[dict]]:
    """key -> 'new' | 'still' for this run's list, plus the dropped items.
    Only a complete run can drop things: a run cut short by its budget didn't
    look hard enough to say something fell out of the top K."""
    last = {t["key"]: t for t in memory.last_top_k}
    changes = {d["key"]: ("still" if d["key"] in last else "new") for d in report["developments"]}
    dropped = [t for key, t in last.items() if key not in changes] if run_complete else []
    return changes, sorted(dropped, key=lambda t: t["rank"])


def build_payload(cfg: Any, result: AgentResult, report_md: str, changes: dict[str, str],
                  dropped: list[dict], model_used: str) -> dict:
    """The body for POST /api/tracker/runs (backend/app/tracker_schemas.py:RunIn)."""
    titles = {a["canonical_url"]: a["title"] for a in result.articles if a.get("title")}
    developments, rankings = [], []
    for d in result.report["developments"]:
        developments.append({
            "key": d["key"], "title": d["title"][:300], "summary": d["summary"][:4000],
            "sources": [{"url": s["url"], "title": (titles.get(canonicalize(s["url"])) or None),
                         "evidence": s["evidence"][:2000]} for s in d["sources"]][:20],
        })
        rankings.append({"key": d["key"], "rank": d["rank"], "change": changes.get(d["key"], "new"),
                         "summary": d["summary"][:4000]})
    rankings += [{"key": t["key"], "rank": None, "change": "dropped", "summary": t["summary"][:4000]}
                 for t in dropped]
    stats = {k: v for k, v in result.stats.items() if isinstance(v, (int, float, str))}
    stats["model_used"] = model_used
    return {
        "started_at": result.started_at, "finished_at": result.finished_at,
        "status": result.status, "stop_reason": (result.stop_reason or None) and result.stop_reason[:500],
        "topic": cfg["topic"], "k": int(cfg["k"]), "model": model_used[:100], "stats": stats,
        "report_md": report_md[:200_000], "articles": result.articles[:500],
        "developments": developments, "rankings": rankings,
    }
