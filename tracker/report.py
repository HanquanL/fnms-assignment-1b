"""finish(report): the model's final answer, checked by code before it's accepted.

The model proposes; the code decides. Provenance is checked claim by claim
(requirement 10: a claim that doesn't appear in its cited source counts as a
hallucination). A development's summary is not free text: it is the list of its
claims, and every claim carries the URL it came from and a sentence copied from
that article. The code accepts a claim only if:
  - the URL is an article fetched this run (or a source verified in an earlier run)
  - the evidence sentence really appears in that article's text
  - every number in the claim also appears in that article (numbers are the
    facts models most often blur or invent: "1.05 trillion" when the source says
    "1 trillion"). Checked against the whole article, not just the evidence
    sentence, so a model name like "V4.1" mentioned elsewhere in it still counts.
"""
import difflib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import urlsplit

from tracker.urls import canonicalize

KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*(/[a-z0-9._-]+){1,3}$")  # org/model[/event]
MAX_CLAIMS = 4

# JSON schema the model sees for the finish tool's argument
REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "developments": {
            "type": "array",
            "description": "Top developments, most important first.",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Stable id: org/model/event, lowercase, e.g. "
                            "'qwen/qwen-4/release'. Reuse the key of a known development if this is the same one."},
                    "title": {"type": "string", "description": "Short headline, under 120 characters."},
                    "rank": {"type": "integer", "description": "1 = most important."},
                    "claims": {
                        "type": "array",
                        "description": f"1-{MAX_CLAIMS} factual sentences that together summarize the development. "
                                       "Each claim restates ONLY what its evidence sentence says.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string", "description": "One factual sentence. Any number in it "
                                         "must appear in the cited article."},
                                "url": {"type": "string", "description": "URL of a fetched article that states it."},
                                "evidence": {"type": "string", "description": "A sentence copied word for word "
                                             "from that article."},
                            },
                            "required": ["text", "url", "evidence"],
                            "propertyOrdering": ["text", "url", "evidence"],
                        },
                    },
                },
                "required": ["key", "title", "rank", "claims"],
                "propertyOrdering": ["key", "title", "rank", "claims"],
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


_FOLD = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
                       "−": "-", " ": " "})
_NUMBER_WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
                 "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12", "twenty": "20",
                 "hundred": "100", "thousand": "1000"}


def _norm(text: str) -> str:
    """Compare loosely: unicode-normalized, lowercase, curly quotes/dashes folded, whitespace collapsed."""
    return " ".join(unicodedata.normalize("NFKC", text).lower().translate(_FOLD).split())


def evidence_in_text(evidence: str, text: str) -> bool:
    ev = _norm(evidence).strip(" .\"'")
    return len(ev) >= 20 and ev in _norm(text)


def numbers_in(text: str) -> set[str]:
    """'1,000' -> '1000', '1.05' stays, 'one million' -> {'1'}: digits only, so wording can differ."""
    t = _norm(text)
    t = re.sub(r"\b(" + "|".join(_NUMBER_WORDS) + r")\b", lambda m: _NUMBER_WORDS[m.group(1)], t)
    return {n.replace(",", "").rstrip(".") for n in re.findall(r"\d[\d,]*(?:\.\d+)?", t)}


def closest_sentence(evidence: str, text: str) -> str | None:
    """The article sentence most like the misquoted evidence, so the model can copy it exactly."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if 20 <= len(s.strip()) <= 600]
    best = difflib.get_close_matches(evidence, sentences, n=1, cutoff=0.6)
    return best[0][:300] if best else None


def unsupported_numbers(claim: str, source: str) -> list[str]:
    return sorted(numbers_in(claim) - numbers_in(source))


def _check_claim(c: Any, where: str, texts: Mapping[str, str] | None) -> tuple[dict | None, str | None]:
    if not isinstance(c, dict):
        return None, f"{where}: each claim must be an object with text, url, evidence"
    text = " ".join(str(c.get("text") or "").split())
    url = str(c.get("url") or "").strip()
    evidence = " ".join(str(c.get("evidence") or "").split())
    if not 10 <= len(text) <= 400:
        return None, f"{where}: claim text must be 10-400 characters"
    if urlsplit(url).scheme not in ("http", "https"):
        return None, f"{where}: '{url[:80]}' is not an http(s) URL"
    if not 20 <= len(evidence) <= 600:
        return None, f"{where}: evidence must be a 20-600 character sentence copied from the article"
    source_text = evidence  # CLI mode: no article text, so the evidence is all we can check against
    if texts is not None:
        source_text = texts.get(canonicalize(url))
        if source_text is None:
            return None, f"{where}: {url[:80]} was not fetched; fetch it first or cite a fetched article"
        if not evidence_in_text(evidence, source_text):
            hint = closest_sentence(evidence, source_text)
            return None, (f"{where}: the evidence sentence is not in {url[:80]}; copy it exactly"
                          + (f". Closest sentence in the article: \"{hint}\"" if hint else ""))
    missing = unsupported_numbers(text, source_text)
    if missing:
        return None, (f"{where}: the claim says {', '.join(missing)} but {url[:60]} never does; "
                      f"only state numbers the source states")
    return {"text": text, "url": url, "evidence": evidence}, None


def _sources(claims: list[dict]) -> list[dict]:
    """One source per URL, with all of its evidence sentences (the backend stores sources, not claims)."""
    by_url: dict[str, list[str]] = {}
    for c in claims:
        by_url.setdefault(c["url"], [])
        if c["evidence"] not in by_url[c["url"]]:
            by_url[c["url"]].append(c["evidence"])
    return [{"url": u, "evidence": " ... ".join(evs)[:2000]} for u, evs in by_url.items()]


def _development(d: Any, where: str, k: int, texts: Mapping[str, str] | None,
                 keep_good_claims: bool) -> tuple[dict | None, list[str]]:
    problems: list[str] = []
    if not isinstance(d, dict):
        return None, [f"{where}: must be an object"]
    key = str(d.get("key", "")).strip().lower()
    title = " ".join(str(d.get("title") or "").split())
    rank = d.get("rank")
    if not KEY_RE.match(key) or len(key) > 200:
        problems.append(f"{where}: key '{key}' must look like 'org/model/event' (lowercase, a-z 0-9 . _ -)")
    if not isinstance(rank, int) or isinstance(rank, bool) or not 1 <= rank <= k:
        problems.append(f"{where}: rank must be an integer 1-{k}")
    raw_claims = d.get("claims")
    if not isinstance(raw_claims, list) or not raw_claims:
        problems.append(f"{where}: needs 1-{MAX_CLAIMS} claims")
        raw_claims = []
    claims = []
    for j, c in enumerate(raw_claims[:MAX_CLAIMS], start=1):
        claim, problem = _check_claim(c, f"{where} claim {j}", texts)
        if claim:
            claims.append(claim)
        elif not keep_good_claims:
            problems.append(problem)
    if not claims and not problems:
        problems.append(f"{where}: no claim could be verified")
    if problems:
        return None, problems
    summary = " ".join(c["text"] for c in claims)
    if not title:  # models sometimes drop the title; take the first claim, never invent one
        title = claims[0]["text"]
    title = title if len(title) <= 120 else title[:117].rstrip() + "..."
    return {"key": key, "title": title, "rank": rank, "summary": summary, "claims": claims,
            "sources": _sources(claims)}, []


def validate_report(raw: Any, k: int, fetched_texts: Mapping[str, str] | None = None) -> FinishResult:
    """fetched_texts: canonical_url -> article text (plus known evidence from earlier runs).
    None = structure checks only (e.g. when finish is called from the CLI)."""
    if not isinstance(raw, dict) or not isinstance(raw.get("developments"), list):
        return FinishResult(False, problems=["report must be an object with a 'developments' list"])
    devs = raw["developments"]
    problems: list[str] = []
    if len(devs) > k:
        problems.append(f"at most {k} developments allowed, got {len(devs)}")
    clean, keys, ranks = [], set(), []
    for i, d in enumerate(devs[:k], start=1):
        dev, errs = _development(d, f"development #{i}", k, fetched_texts, keep_good_claims=False)
        problems += errs
        if dev:
            if dev["key"] in keys:
                problems.append(f"development #{i}: duplicate key '{dev['key']}'")
            keys.add(dev["key"])
            ranks.append(dev["rank"])
            clean.append(dev)
    if len(ranks) != len(set(ranks)):
        problems.append("ranks must be unique")
    elif ranks and not problems and sorted(ranks) != list(range(1, len(ranks) + 1)):
        problems.append(f"ranks must be 1..{len(ranks)} with no gaps")
    if problems:
        return FinishResult(False, problems=problems)
    clean.sort(key=lambda d: d["rank"])
    return FinishResult(True, report={"developments": clean, "notes": str(raw.get("notes") or "")[:2000]})


def salvage_report(raw: Any, k: int, fetched_texts: Mapping[str, str]) -> dict:
    """Last resort after repeated rejections: keep only the claims that verify,
    drop developments left with none, and renumber ranks. Nothing unverified survives."""
    kept: list[tuple[int, dict]] = []
    seen: set[str] = set()
    devs = raw.get("developments") if isinstance(raw, dict) else None
    for d in (devs if isinstance(devs, list) else [])[:k]:
        fixed = {**d, "rank": 1} if isinstance(d, dict) else d
        dev, _ = _development(fixed, "development", k, fetched_texts, keep_good_claims=True)
        if dev and dev["key"] not in seen:
            seen.add(dev["key"])
            rank = d.get("rank") if isinstance(d.get("rank"), int) else 99
            kept.append((rank, dev))
    kept.sort(key=lambda x: x[0])
    developments = [{**dev, "rank": i} for i, (_, dev) in enumerate(kept, start=1)]
    return {"developments": developments, "notes": "Salvaged: only claims with verified evidence were kept."}
