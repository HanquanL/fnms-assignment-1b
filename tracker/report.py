"""finish(report): the model's final answer, checked by code before it's accepted.

The model proposes; the code decides. A report is rejected (with reasons the
model can act on) unless every development:
  - has a well-formed key, a title, a short summary, and a unique rank 1..K
  - cites 1-5 http(s) sources
  - and, when we know what was fetched this run, every source is an article we
    actually fetched AND its evidence quote appears in that article's text
    (requirement 10: a claim not in the cited source is a hallucination).
"""
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import urlsplit

from tracker.urls import canonicalize

KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*(/[a-z0-9._-]+){1,3}$")  # org/model[/event]

# JSON schema the model sees for the finish tool's argument
REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "developments": {
            "type": "array",
            "description": "Top developments, best first. Each needs evidence copied from a fetched article.",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Stable id: org/model/event, lowercase, e.g. 'qwen/qwen-4/release'. Reuse the key of a known development if this is the same one."},
                    "title": {"type": "string"},
                    "summary": {"type": "string", "description": "2-4 sentences, only facts stated in the sources."},
                    "rank": {"type": "integer", "description": "1 = most important."},
                    "sources": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "url": {"type": "string", "description": "URL of an article you fetched."},
                                "evidence": {"type": "string", "description": "A sentence copied word for word from that article that supports the summary."},
                            },
                            "required": ["url", "evidence"],
                        },
                    },
                },
                "required": ["key", "title", "summary", "rank", "sources"],
            },
        },
        "notes": {"type": "string", "description": "Optional: gaps, caveats."},
    },
    "required": ["developments"],
}


@dataclass
class FinishResult:
    ok: bool
    report: dict | None = None
    problems: list[str] = field(default_factory=list)


def _norm(text: str) -> str:
    """Compare quotes loosely: unicode-normalized, curly quotes/dashes folded, whitespace collapsed."""
    text = unicodedata.normalize("NFKC", text).lower()
    text = text.translate(str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
                                         "\u2013": "-", "\u2014": "-", "\u00a0": " "}))
    return " ".join(text.split())


def evidence_in_text(evidence: str, text: str) -> bool:
    ev = _norm(evidence).strip(" .\"'")
    return len(ev) >= 20 and ev in _norm(text)


def validate_report(raw: Any, k: int, fetched_texts: Mapping[str, str] | None = None) -> FinishResult:
    """fetched_texts: canonical_url -> article text for every article fetched this run.
    None = schema checks only (e.g. when finish is called from the CLI)."""
    problems: list[str] = []
    if not isinstance(raw, dict) or not isinstance(raw.get("developments"), list):
        return FinishResult(False, problems=["report must be an object with a 'developments' list"])
    devs = raw["developments"]
    if len(devs) > k:
        problems.append(f"at most {k} developments allowed, got {len(devs)}")

    clean_devs, seen_keys, ranks = [], set(), []
    for i, d in enumerate(devs[:k], start=1):
        where = f"development #{i}"
        if not isinstance(d, dict):
            problems.append(f"{where}: must be an object")
            continue
        key = str(d.get("key", "")).strip().lower()
        title = " ".join(str(d.get("title", "")).split())
        summary = " ".join(str(d.get("summary", "")).split())
        rank = d.get("rank")
        if not KEY_RE.match(key) or len(key) > 200:
            problems.append(f"{where}: key '{key}' must look like 'org/model/event' (lowercase, a-z 0-9 . _ -)")
        elif key in seen_keys:
            problems.append(f"{where}: duplicate key '{key}'")
        seen_keys.add(key)
        if not 1 <= len(title) <= 300:
            problems.append(f"{where}: title must be 1-300 characters")
        if not 1 <= len(summary) <= 1200:
            problems.append(f"{where}: summary must be 1-1200 characters")
        if not isinstance(rank, int) or isinstance(rank, bool) or not 1 <= rank <= k:
            problems.append(f"{where}: rank must be an integer 1-{k}")
        else:
            ranks.append(rank)

        sources = d.get("sources")
        if not isinstance(sources, list) or not 1 <= len(sources) <= 5:
            problems.append(f"{where}: needs 1-5 sources")
            sources = []
        clean_sources = []
        for s in sources:
            url = str((s or {}).get("url", "")).strip() if isinstance(s, dict) else ""
            evidence = " ".join(str((s or {}).get("evidence", "")).split()) if isinstance(s, dict) else ""
            if urlsplit(url).scheme not in ("http", "https"):
                problems.append(f"{where}: source '{url[:80]}' is not an http(s) URL")
                continue
            if not 20 <= len(evidence) <= 600:
                problems.append(f"{where}: evidence for {url[:80]} must be a 20-600 character quote")
                continue
            if fetched_texts is not None:
                text = fetched_texts.get(canonicalize(url))
                if text is None:
                    problems.append(f"{where}: {url[:80]} was not fetched this run; only cite articles you fetched")
                    continue
                if not evidence_in_text(evidence, text):
                    problems.append(f"{where}: the evidence quote was not found in {url[:80]}; copy a sentence exactly")
                    continue
            clean_sources.append({"url": url, "evidence": evidence})
        clean_devs.append({"key": key, "title": title, "summary": summary, "rank": rank, "sources": clean_sources})

    if len(ranks) != len(set(ranks)):
        problems.append("ranks must be unique")
    elif ranks and sorted(ranks) != list(range(1, len(ranks) + 1)):
        problems.append(f"ranks must be 1..{len(ranks)} with no gaps")

    if problems:
        return FinishResult(False, problems=problems)
    clean_devs.sort(key=lambda d: d["rank"])
    return FinishResult(True, report={"developments": clean_devs, "notes": str(raw.get("notes") or "")[:2000]})

def salvage_report(raw: Any, k: int, fetched_texts: Mapping[str, str]) -> dict:
    """Last resort after repeated rejections: keep each development's sources that
    verify on their own, drop the rest, and renumber ranks. Nothing unverified survives."""
    kept: list[tuple[int, dict]] = []
    seen: set[str] = set()
    devs = raw.get("developments") if isinstance(raw, dict) else None
    for d in (devs if isinstance(devs, list) else [])[:k]:
        if not isinstance(d, dict) or not isinstance(d.get("sources"), list):
            continue
        good = [s for s in d["sources"][:5]
                if validate_report({"developments": [{**d, "rank": 1, "sources": [s]}]}, k, fetched_texts).ok]
        if not good:
            continue
        res = validate_report({"developments": [{**d, "rank": 1, "sources": good}]}, k, fetched_texts)
        if res.ok and res.report["developments"][0]["key"] not in seen:
            dev = res.report["developments"][0]
            seen.add(dev["key"])
            rank = d.get("rank") if isinstance(d.get("rank"), int) else 99
            kept.append((rank, dev))
    kept.sort(key=lambda x: x[0])
    developments = [{**dev, "rank": i} for i, (_, dev) in enumerate(kept, start=1)]
    return {"developments": developments, "notes": "Salvaged: only developments with verified evidence were kept."}